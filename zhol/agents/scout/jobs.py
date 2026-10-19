from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout import sample
from zhol.agents.scout.collect import collect_company
from zhol.agents.scout.errors import CollectionFailed
from zhol.agents.scout.extract import extract_card
from zhol.agents.scout.fields import PURPOSES, RECHECK_FIELDS, field_keys
from zhol.agents.scout.persist import open_task
from zhol.agents.scout.verify import check_snapshot, needs_match_review
from zhol.browser.google import public as google_public
from zhol.browser.twogis import public as twogis_public
from zhol.browser.yandex import public as yandex_public
from zhol.browser.config import load_platforms
from zhol.browser.fixtures import FixtureStore
from zhol.browser.live import LiveCollectionDisabled, live_enabled
from zhol.db.models import CardSnapshot, Company, Job
from zhol.jobs import enqueue, run_one
from zhol.timeutil import now
from zhol.transitions import transition

_CARD = {"2gis": twogis_public.read_card, "yandex": yandex_public.read_card, "google": google_public.read_card}
BROWSER_JOBS = ("scout.collect", "review.snapshot_recheck")
MAIN_JOBS = ("snapshot.verify", "admin.scout_batch")
# Same job type as the main queue. Only browser_2gis payloads are enqueued here.
COLLECTOR_JOBS = ("admin.scout_batch",)


def enqueue_collect(
    session: Session,
    company_id: int,
    purpose: str,
    fixtures_dir: str,
    *,
    collection_key: str | None = None,
) -> tuple[int, bool]:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    dedup = f"scout.collect:{company_id}:{purpose}"
    already = session.scalar(select(Job.id).where(Job.dedup_key == dedup))
    if already is not None:
        return already, True
    payload = {
        "company_id": company_id,
        "purpose": purpose,
        "collection_key": collection_key or f"scout:{company_id}:{purpose}:{uuid4().hex}",
        "fixtures_dir": fixtures_dir,
    }
    return enqueue(session, "scout.collect", payload, queue="browser", dedup_key=dedup), False


def _verify(session: Session, snapshot_ids: list[int], company_id: int) -> int:
    snapshots = [session.get(CardSnapshot, snapshot_id) for snapshot_id in snapshot_ids]
    twogis = next((row for row in snapshots if row is not None and row.platform == "2gis"), None)
    twogis_fields = None if twogis is None else twogis.fields
    verified = 0
    for snapshot in snapshots:
        if snapshot is None:
            continue
        if snapshot.status == "verified":
            verified += 1
            continue
        if snapshot.status != "collected":
            continue
        hold = ((snapshot.field_sources or {}).get("_collection") or {}).get("hold")
        if hold:
            open_task(session, str(hold), "card_snapshot", snapshot.id, {"company_id": company_id, "platform": snapshot.platform})
            continue
        if needs_match_review(snapshot.platform, snapshot.match_status, twogis_fields, snapshot.fields or {}):
            open_task(
                session,
                "match_needs_review",
                "card_snapshot",
                snapshot.id,
                {"company_id": company_id, "platform": snapshot.platform},
            )
            continue
        problems = check_snapshot(snapshot)
        if problems:
            open_task(session, "snapshot_invalid", "card_snapshot", snapshot.id, {"problems": problems})
            continue
        transition(snapshot, "verified", session)
        verified += 1
    return verified


def _fresh_fields(snapshot: CardSnapshot, pages: FixtureStore, cfg: dict[str, Any]) -> dict[str, Any]:
    meta = (snapshot.field_sources or {}).get("_collection") or {}
    if meta.get("kind") != "card" or not meta.get("file"):
        return {key: None for key in field_keys(snapshot.platform)}
    page = pages.read(snapshot.platform, meta["file"], snapshot.url or "", kind="card")
    document = _CARD[snapshot.platform](page.html, cfg["markers"])
    spec = cfg["platforms"][snapshot.platform]
    fields, _sources = extract_card(
        snapshot.platform,
        document,
        url=page.url,
        seen_at=now().isoformat(),
        screenshot=page.screenshot_path,
        raw_path=page.raw_path,
        code_fields=list(spec["code_fields"]),
        model_fields=list(spec["model_fields"]),
        model_reader=None,
    )
    return fields


def recheck_snapshots(session: Session, payload: dict[str, Any]) -> None:
    pages = FixtureStore(payload["fixtures_dir"])
    cfg = load_platforms()
    company_id = payload["company_id"]
    prior = session.scalar(
        select(func.count()).select_from(CardSnapshot).where(
            CardSnapshot.company_id == company_id,
            CardSnapshot.status == "rejected",
        )
    ) or 0
    rejected_now = False
    for snapshot_id in payload["snapshot_ids"]:
        snapshot = session.get(CardSnapshot, snapshot_id)
        if snapshot is None or snapshot.status not in ("collected", "verified"):
            continue
        fresh = _fresh_fields(snapshot, pages, cfg)
        stored = snapshot.fields or {}
        diffs = [key for key in RECHECK_FIELDS if key in stored and stored.get(key) != fresh.get(key)]
        snapshot.recheck_count = (snapshot.recheck_count or 0) + 1
        model_fields = [
            key
            for key, source in (snapshot.field_sources or {}).items()
            if isinstance(source, dict) and source.get("read_by") == "model" and stored.get(key) is not None
        ]
        if model_fields:
            open_task(
                session,
                "scout_fields_unavailable",
                "card_snapshot",
                snapshot.id,
                {"fields": model_fields, "company_id": company_id},
            )
        if len(diffs) > 1:
            snapshot.fail_reason = "recheck:" + ",".join(diffs)
            session.flush()
            transition(snapshot, "rejected", session)
            rejected_now = True
    if not rejected_now:
        return
    if prior >= 1:
        open_task(
            session,
            "scout_rejected_twice",
            "company",
            company_id,
            {"collection_key": payload["collection_key"]},
        )
        return
    enqueue_collect(
        session,
        company_id,
        payload["purpose"],
        payload["fixtures_dir"],
        collection_key=f"{payload['collection_key']}:retry",
    )


def _pages(payload: dict[str, Any]) -> FixtureStore:
    if not payload.get("live"):
        return FixtureStore(payload["fixtures_dir"])
    if not live_enabled():
        raise LiveCollectionDisabled("live collection is disabled")
    platforms = tuple(payload.get("platforms") or ())
    if platforms != ("2gis",):
        raise CollectionFailed("live collection is public 2GIS only")
    from zhol.browser.twogis.capture import LiveStore

    return LiveStore(payload.get("live_dir") or "data/scout-pilot")


def handlers(factory: sessionmaker[Session]) -> dict[str, Any]:
    def collect(payload: dict[str, Any]) -> None:
        pages = _pages(payload)
        cfg = load_platforms()
        platforms = tuple(payload.get("platforms") or ("2gis", "yandex", "google"))
        with factory() as session, session.begin():
            company = session.get(Company, payload["company_id"])
            if company is None:
                raise CollectionFailed("company not found")
            result = collect_company(
                session,
                company=company,
                purpose=payload["purpose"],
                collection_key=payload["collection_key"],
                pages=pages,
                cfg=cfg,
                platforms=platforms,
            )
            snapshot_ids = [row.id for row in result.snapshots]
            failed = result.failed
            reason = result.reason
        if failed:
            raise CollectionFailed(reason or "collection failed")
        with factory() as session, session.begin():
            enqueue(
                session,
                "snapshot.verify",
                {
                    "company_id": payload["company_id"],
                    "purpose": payload["purpose"],
                    "collection_key": payload["collection_key"],
                    "fixtures_dir": payload["fixtures_dir"],
                    "snapshot_ids": snapshot_ids,
                },
                queue="main",
                dedup_key=f"snapshot.verify:{payload['collection_key']}",
            )

    def verify(payload: dict[str, Any]) -> None:
        with factory() as session, session.begin():
            verified = _verify(session, list(payload["snapshot_ids"]), payload["company_id"])
            company = session.get(Company, payload["company_id"])
            if verified and company is not None and sample.selected_for_recheck(company.id_2gis):
                enqueue(
                    session,
                    "review.snapshot_recheck",
                    payload,
                    queue="browser",
                    dedup_key=f"review.snapshot_recheck:{payload['collection_key']}",
                )

    def recheck(payload: dict[str, Any]) -> None:
        with factory() as session, session.begin():
            recheck_snapshots(session, payload)

    def scout_batch(payload: dict[str, Any]) -> None:
        from zhol.api.admin_jobs import handle_scout_batch

        handle_scout_batch(factory, payload)

    return {
        "scout.collect": collect,
        "snapshot.verify": verify,
        "review.snapshot_recheck": recheck,
        "admin.scout_batch": scout_batch,
    }


def drain(factory: sessionmaker[Session], *, notifier: Any, limit: int = 200) -> int:
    registered = handlers(factory)
    ran = 0
    for _ in range(limit):
        browser = run_one(factory, "browser", registered, notifier=notifier, types=BROWSER_JOBS)
        main = run_one(factory, "main", registered, notifier=notifier, types=MAIN_JOBS)
        collector = run_one(factory, "collector", registered, notifier=notifier, types=COLLECTOR_JOBS)
        if browser is None and main is None and collector is None:
            return ran
        ran += (browser is not None) + (main is not None) + (collector is not None)
    raise RuntimeError("job drain did not finish")
