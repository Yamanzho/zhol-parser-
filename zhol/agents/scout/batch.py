"""Resumable fixture batch. One company is committed before the next starts."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.catalog import LIVE_DISCOVERY, PLATFORMS, BatchError, load_catalog, select_companies
from zhol.agents.scout.collect import Identity, collect_company, collect_matched
from zhol.browser.config import load_platforms
from zhol.browser.fixtures import FixtureStore
from zhol.browser.live import live_enabled
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company
from zhol.export.mailing import load_batch_rows, write_xlsx
from zhol.timeutil import now

MAX_ATTEMPTS = 2
SOURCE = "fixture_catalog"
BLOCKED_MARKERS = ("accessrestricted", "robot_check", "access_blocked", "consent_wall")


@dataclass(frozen=True)
class BatchCounts:
    batch_id: int
    completed: int
    failed: int
    blocked: int
    skipped: int
    duplicate: int
    pending: int
    rows: int
    excel: str


def live_discovery_report() -> str:
    lines = ["Live discovery is unsupported. No pages were opened."]
    for platform in PLATFORMS:
        lines.append(f"- {platform}: {LIVE_DISCOVERY[platform]}")
    return "\n".join(lines)


def run_batch(
    factory: sessionmaker[Session],
    *,
    city: str | None,
    category: str | None,
    platforms: tuple[str, ...],
    limit: int | None,
    out: Path,
    source_mode: str,
    catalog: Path,
    fixtures_dir: Path | None = None,
    resume: int | None = None,
    pages: FixtureStore | None = None,
    stop_after: int | None = None,
    on_open: Callable[[int], None] | None = None,
) -> BatchCounts:
    if source_mode not in ("fixture", "live"):
        raise BatchError(f"unknown source mode {source_mode}")
    if source_mode == "live":
        if not live_enabled():
            raise BatchError("live discovery requires ZHOL_SCOUT_LIVE=1")
        raise BatchError(live_discovery_report())
    chosen = _platforms(platforms)
    if limit is not None and limit < 1:
        raise BatchError("limit must be a positive number or omitted")
    if resume is None and (not city or not category):
        raise BatchError("city and category are required")
    batch_id = resume or _open_batch(
        factory,
        city=city or "",
        category=category or "",
        platforms=chosen,
        limit=limit,
        out=out,
        catalog=catalog,
        fixtures_dir=fixtures_dir,
    )
    store = pages or FixtureStore(fixtures_dir)
    cfg = load_platforms()
    processed = 0
    already_completed = _count(factory, batch_id, "completed") if resume is not None else 0
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            raise BatchError(f"batch {batch_id} was not found")
        if batch.source_mode != "fixture":
            raise BatchError(f"batch {batch_id} is not a fixture run")
        out = Path(batch.out_path)
    if on_open is not None:
        on_open(batch_id)
    while stop_after is None or processed < stop_after:
        item_id = _claim(factory, batch_id)
        if item_id is None:
            break
        try:
            _process_item(factory, item_id, store, cfg)
        except BatchError:
            raise
        except Exception as exc:
            _mark_failed(factory, item_id, exc)
        _export(factory, batch_id, out)
        processed += 1
    if _pending(factory, batch_id):
        _finish(factory, batch_id, "interrupted")
    else:
        _finish(factory, batch_id, "completed")
    _export(factory, batch_id, out)
    return _counts(factory, batch_id, out, duplicate=already_completed)


def _platforms(values: tuple[str, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        name = value.strip().lower()
        if not name:
            continue
        if name not in PLATFORMS:
            raise BatchError(f"unsupported platform {value}")
        if name not in found:
            found.append(name)
    if not found:
        raise BatchError("platforms is empty")
    return tuple(found)


def _open_batch(
    factory: sessionmaker[Session],
    *,
    city: str,
    category: str,
    platforms: tuple[str, ...],
    limit: int | None,
    out: Path,
    catalog: Path,
    fixtures_dir: Path | None,
) -> int:
    rows = select_companies(load_catalog(catalog), city=city, category=category)
    if limit is not None:
        rows = rows[:limit]
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city=city,
            category=category,
            platforms=list(platforms),
            item_limit=limit,
            source_mode="fixture",
            status="running",
            out_path=str(out),
            catalog_path=str(catalog),
            fixtures_dir=None if fixtures_dir is None else str(fixtures_dir),
        )
        session.add(batch)
        session.flush()
        for row in rows:
            session.add(
                CollectionBatchItem(
                    batch_id=batch.id,
                    id_2gis=row.id_2gis,
                    status="pending",
                    detail={"name": row.name, "city": row.city, "category": row.category, "platforms": {}},
                )
            )
        return batch.id


def _claim(factory: sessionmaker[Session], batch_id: int) -> int | None:
    with factory() as session, session.begin():
        items = session.scalars(
            select(CollectionBatchItem)
            .where(CollectionBatchItem.batch_id == batch_id)
            .order_by(CollectionBatchItem.id)
        ).all()
        for item in items:
            if item.status == "pending" or (item.status == "in_progress" and item.attempts < MAX_ATTEMPTS):
                item.status = "in_progress"
                item.attempts = (item.attempts or 0) + 1
                item.updated_at = now()
                return item.id
            if item.status == "in_progress":
                item.status = "failed"
                item.detail = {**(item.detail or {}), "reason": "interrupted twice"}
                item.updated_at = now()
        return None


def _process_item(factory: sessionmaker[Session], item_id: int, pages: FixtureStore, cfg: dict[str, Any]) -> None:
    with factory() as session, session.begin():
        item = session.get(CollectionBatchItem, item_id)
        batch = session.get(CollectionBatch, item.batch_id)
        if item is None or batch is None:
            return
        detail = dict(item.detail or {})
        if str(item.id_2gis).startswith("trace:"):
            company = session.get(Company, item.company_id) if item.company_id else None
            if company is None:
                item.status = "blocked"
                item.detail = {**detail, "reason": "identity_not_stored"}
            else:
                item.status = "completed"
            item.updated_at = now()
            return
        company = session.scalar(select(Company).where(Company.id_2gis == item.id_2gis))
        if company is None:
            company = Company(
                id_2gis=item.id_2gis,
                name=str(detail.get("name") or item.id_2gis),
                city=str(detail.get("city") or batch.city),
                niche=str(detail.get("category") or batch.category),
                source=SOURCE,
            )
            session.add(company)
            session.flush()
        item.company_id = company.id
        platforms = tuple(batch.platforms or ())
        states, item_status = _collect(session, company, platforms, f"batch:{batch.id}:{item.id_2gis}", pages, cfg)
        detail["platforms"] = states
        item.detail = detail
        item.status = item_status
        item.updated_at = now()


def _collect(
    session: Session,
    company: Company,
    platforms: tuple[str, ...],
    collection_key: str,
    pages: FixtureStore,
    cfg: dict[str, Any],
) -> tuple[dict[str, str], str]:
    states = {platform: "not_checked" for platform in PLATFORMS}
    if "2gis" in platforms:
        result = collect_company(
            session,
            company=company,
            purpose="prospect",
            collection_key=collection_key,
            pages=pages,
            cfg=cfg,
            platforms=platforms,
        )
        for snapshot in result.snapshots:
            states[snapshot.platform] = _state(snapshot)
        if states.get("2gis") == "blocked":
            return states, "blocked"
        if states.get("2gis") != "found":
            return states, "failed"
        if any(states[name] == "blocked" for name in platforms):
            return states, "completed"
        return states, "completed"
    identity = _ready_identity(session, company.id)
    if identity is None:
        return states, "failed"
    outcomes = collect_matched(
        session,
        company=company,
        identity=identity,
        purpose="prospect",
        collection_key=collection_key,
        pages=pages,
        cfg=cfg,
        platforms=platforms,
        persist_misses=True,
    )
    for outcome in outcomes:
        if outcome.snapshot is not None:
            states[outcome.platform] = _state(outcome.snapshot)
        elif outcome.verdict == "ambiguous":
            states[outcome.platform] = "ambiguous"
        elif outcome.hold:
            states[outcome.platform] = "blocked" if "access" in (outcome.hold or "") else "not_found"
        else:
            states[outcome.platform] = "not_found"
    if any(states[name] == "blocked" for name in platforms) and all(states[name] in ("blocked", "not_checked") for name in platforms):
        return states, "blocked"
    return states, "completed"


def _ready_identity(session: Session, company_id: int) -> Identity | None:
    rows = session.scalars(
        select(CardSnapshot).where(
            CardSnapshot.company_id == company_id,
            CardSnapshot.platform == "2gis",
            CardSnapshot.status.in_(("collected", "verified")),
        )
    ).all()
    if not rows:
        return None
    latest = max(rows, key=lambda row: (row.collected_at, row.id))
    fields = latest.fields or {}
    phones = list(fields.get("phones") or [])
    return Identity(fields.get("name"), fields.get("address"), phones)


def _state(snapshot: CardSnapshot) -> str:
    reason = (snapshot.fail_reason or "").casefold()
    if any(marker in reason for marker in BLOCKED_MARKERS):
        return "blocked"
    hold = ((snapshot.field_sources or {}).get("_collection") or {}).get("hold")
    if hold == "ambiguous_match" or snapshot.match_status == "not_found" and hold:
        return "ambiguous"
    if snapshot.match_status == "other_address":
        return "other_address"
    if snapshot.match_status == "not_found" or snapshot.status == "failed":
        return "not_found"
    if snapshot.match_status == "found" and snapshot.status in ("collected", "verified"):
        return "found"
    return "failed"


def _export(factory: sessionmaker[Session], batch_id: int, out: Path) -> int:
    with factory() as session:
        rows = load_batch_rows(session, batch_id)
    return write_xlsx(rows, out)


def _pending(factory: sessionmaker[Session], batch_id: int) -> int:
    with factory() as session:
        items = session.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
    return sum(1 for item in items if item.status in ("pending", "in_progress"))


def _finish(factory: sessionmaker[Session], batch_id: int, status: str) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            return
        batch.status = status
        batch.finished_at = now()


def _count(factory: sessionmaker[Session], batch_id: int, status: str) -> int:
    with factory() as session:
        items = session.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
    return sum(1 for item in items if item.status == status)


def _mark_failed(factory: sessionmaker[Session], item_id: int, exc: BaseException) -> None:
    with factory() as session, session.begin():
        item = session.get(CollectionBatchItem, item_id)
        if item is None or item.status == "completed":
            return
        item.status = "failed"
        item.detail = {**(item.detail or {}), "reason": f"{type(exc).__name__}: {exc}"}
        item.updated_at = now()


def progress_counts(session: Session, batch_id: int) -> dict[str, int]:
    """Count stored batch items. This does not open a page or resume a batch."""
    counts = {name: 0 for name in ("completed", "failed", "blocked", "pending", "in_progress")}
    rows = session.execute(
        select(CollectionBatchItem.status, func.count())
        .where(CollectionBatchItem.batch_id == batch_id)
        .group_by(CollectionBatchItem.status)
    ).all()
    total = 0
    for status, count in rows:
        total += int(count)
        if status in counts:
            counts[str(status)] = int(count)
    counts["total"] = total
    return counts


def _counts(factory: sessionmaker[Session], batch_id: int, out: Path, *, duplicate: int) -> BatchCounts:
    with factory() as session:
        items = session.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
        rows = load_batch_rows(session, batch_id)
    by_status = {name: 0 for name in ("completed", "failed", "blocked", "pending", "in_progress")}
    for item in items:
        by_status[item.status] = by_status.get(item.status, 0) + 1
    return BatchCounts(
        batch_id=batch_id,
        completed=by_status["completed"],
        failed=by_status["failed"],
        blocked=by_status["blocked"],
        skipped=by_status["pending"] + by_status["in_progress"],
        duplicate=duplicate,
        pending=by_status["pending"] + by_status["in_progress"],
        rows=len(rows),
        excel=str(out),
    )
