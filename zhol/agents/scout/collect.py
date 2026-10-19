from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from zhol.agents.scout.errors import CollectionFailed, InvalidField
from zhol.agents.scout.extract import ModelReader, empty_observation, extract_card
from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.match import Decision, Identity, candidates_from_document, choose, match_status
from zhol.agents.scout.persist import find_reusable, insert_snapshot, open_task, owner_company_id, remember_platform_id
from zhol.browser.google import public as google_public
from zhol.browser.twogis import public as twogis_public
from zhol.browser.yandex import public as yandex_public
from zhol.browser.evidence import usable_screenshot
from zhol.browser.fixtures import FixtureStore, LoadedPage
from zhol.browser.page import AccessRestricted, PageNotLoaded, UnexpectedPage
from zhol.db.models import CardSnapshot, Company
from zhol.timeutil import now

_CARD = {"2gis": twogis_public.read_card, "yandex": yandex_public.read_card, "google": google_public.read_card}
_SEARCH = {"yandex": yandex_public.read_search, "google": google_public.read_search}


@dataclass
class CollectResult:
    snapshots: list[CardSnapshot]
    failed: bool
    reason: str | None


@dataclass
class MatchOutcome:
    platform: str
    verdict: str
    snapshot: CardSnapshot | None
    hold: str | None


def _identity(snapshot: CardSnapshot) -> Identity:
    fields = snapshot.fields or {}
    phones = fields.get("phones") or []
    return Identity(fields.get("name"), fields.get("address"), list(phones))


def _search_url(cfg: dict[str, Any], platform: str, identity: Identity) -> str:
    query = " ".join(part for part in (identity.name, identity.address) if part)
    return cfg["platforms"][platform]["search_url"].format(query=quote_plus(query))


def _nulls(platform: str) -> dict[str, Any]:
    return {key: None for key in field_keys(platform)}


def _screenshot_of(page: LoadedPage | None, pages: FixtureStore, exc: BaseException) -> str | None:
    found = getattr(exc, "screenshot", None)
    if isinstance(found, str):
        kept = usable_screenshot(found)
        if kept:
            return kept
    if page is not None:
        kept = usable_screenshot(page.screenshot_path)
        if kept:
            return kept
    return usable_screenshot(getattr(pages, "screenshot", None))


def _fail(
    session: Session,
    *,
    company_id: int,
    platform: str,
    purpose: str,
    collection_key: str,
    url: str | None,
    screenshot: str | None,
    page: LoadedPage | None,
    reason: str,
    status: str,
) -> CardSnapshot:
    fields = _nulls(platform)
    sources = {
        key: {
            "url": url,
            "seen_at": now().isoformat(),
            "screenshot": screenshot,
            "reason": reason,
            "read_by": "code",
        }
        for key in fields
    }
    return insert_snapshot(
        session,
        company_id=company_id,
        platform=platform,
        purpose=purpose,
        match_status=status,
        url=url,
        fields=fields,
        field_sources=sources,
        screenshot_path=screenshot,
        collection_key=collection_key,
        page_file=None if page is None else page.file_name,
        kind=None if page is None else page.kind,
        raw_path=None if page is None else page.raw_path,
        fail_reason=reason,
    )


def _save(
    session: Session,
    *,
    company: Company,
    platform: str,
    purpose: str,
    collection_key: str,
    match: str,
    url: str | None,
    fields: dict[str, Any],
    sources: dict[str, Any],
    page: LoadedPage,
    external_id: str | None,
    confident: bool,
    hold: str | None,
) -> CardSnapshot:
    if confident and not hold:
        remember_platform_id(session, company.id, platform, external_id, url)
    return insert_snapshot(
        session,
        company_id=company.id,
        platform=platform,
        purpose=purpose,
        match_status=match,
        url=url,
        fields=fields,
        field_sources=sources,
        screenshot_path=page.screenshot_path,
        collection_key=collection_key,
        page_file=page.file_name,
        kind=page.kind,
        raw_path=page.raw_path,
        hold=hold,
    )


def _read_saved_card(
    platform: str,
    page: LoadedPage,
    cfg: dict[str, Any],
    seen_at: str,
    model_reader: ModelReader | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = page.document if page.document is not None else _CARD[platform](page.html, cfg["markers"])
    spec = cfg["platforms"][platform]
    return extract_card(
        platform,
        document,
        url=page.url,
        seen_at=seen_at,
        screenshot=page.screenshot_path,
        raw_path=page.raw_path,
        code_fields=list(spec["code_fields"]),
        model_fields=list(spec["model_fields"]),
        model_reader=model_reader,
    )


def collect_company(
    session: Session,
    *,
    company: Company,
    purpose: str,
    collection_key: str,
    pages: FixtureStore,
    cfg: dict[str, Any],
    model_reader: ModelReader | None = None,
    platforms: tuple[str, ...] = ("2gis", "yandex", "google"),
) -> CollectResult:
    if "2gis" not in platforms:
        raise CollectionFailed("2gis is required")
    twogis = _collect_2gis(session, company, purpose, collection_key, pages, cfg, model_reader)
    if twogis.status == "failed":
        return CollectResult([twogis], True, twogis.fail_reason)
    snapshots = [twogis]
    reasons: list[str] = []
    identity = _identity(twogis)
    for platform in ("yandex", "google"):
        if platform not in platforms:
            continue
        snapshot = _collect_search(session, company, platform, identity, purpose, collection_key, pages, cfg, model_reader)
        snapshots.append(snapshot)
        if snapshot.status == "failed":
            reasons.append(snapshot.fail_reason or platform)
    return CollectResult(snapshots, bool(reasons), "; ".join(reasons) if reasons else None)


def _collect_2gis(
    session: Session,
    company: Company,
    purpose: str,
    collection_key: str,
    pages: FixtureStore,
    cfg: dict[str, Any],
    model_reader: ModelReader | None,
) -> CardSnapshot:
    existing = find_reusable(session, company.id, "2gis", collection_key)
    if existing is not None:
        return existing
    url = cfg["platforms"]["2gis"]["card_url"].format(id=company.id_2gis)
    page: LoadedPage | None = None
    try:
        page = pages.card("2gis", company.id_2gis, url)
        fields, sources = _read_saved_card("2gis", page, cfg, now().isoformat(), model_reader)
    except (PageNotLoaded, UnexpectedPage, AccessRestricted, InvalidField) as exc:
        reason = exc.__class__.__name__ + ": " + str(exc)
        return _fail(
            session,
            company_id=company.id,
            platform="2gis",
            purpose=purpose,
            collection_key=collection_key,
            url=getattr(exc, "url", None) or url,
            screenshot=_screenshot_of(page, pages, exc),
            page=page,
            reason=reason,
            status="not_found",
        )
    return _save(
        session,
        company=company,
        platform="2gis",
        purpose=purpose,
        collection_key=collection_key,
        match="found",
        url=url,
        fields=fields,
        sources=sources,
        page=page,
        external_id=company.id_2gis,
        confident=True,
        hold=None,
    )


def _collect_search(
    session: Session,
    company: Company,
    platform: str,
    identity: Identity,
    purpose: str,
    collection_key: str,
    pages: FixtureStore,
    cfg: dict[str, Any],
    model_reader: ModelReader | None,
) -> CardSnapshot:
    existing = find_reusable(session, company.id, platform, collection_key)
    if existing is not None:
        return existing
    url = _search_url(cfg, platform, identity)
    page: LoadedPage | None = None
    try:
        page = pages.search(platform, company.id_2gis, url)
        document = _SEARCH[platform](page.html, cfg["markers"])
        decision = choose(identity, candidates_from_document(document))
        return _from_decision(
            session,
            company,
            platform,
            purpose,
            collection_key,
            pages,
            cfg,
            model_reader,
            page,
            url,
            decision,
        )
    except (PageNotLoaded, UnexpectedPage, AccessRestricted, InvalidField) as exc:
        reason = exc.__class__.__name__ + ": " + str(exc)
        return _fail(
            session,
            company_id=company.id,
            platform=platform,
            purpose=purpose,
            collection_key=collection_key,
            url=getattr(exc, "url", None) or url,
            screenshot=_screenshot_of(page, pages, exc),
            page=page,
            reason=reason,
            status="not_found",
        )


def _from_decision(
    session: Session,
    company: Company,
    platform: str,
    purpose: str,
    collection_key: str,
    pages: FixtureStore,
    cfg: dict[str, Any],
    model_reader: ModelReader | None,
    search_page: LoadedPage,
    search_url: str,
    decision: Decision,
) -> CardSnapshot:
    seen_at = now().isoformat()
    if decision.verdict in ("not_match", "ambiguous") or decision.candidate is None:
        fields, sources = empty_observation(
            platform,
            url=search_url,
            seen_at=seen_at,
            screenshot=search_page.screenshot_path,
            raw_path=search_page.raw_path,
            reason="not_found" if decision.verdict == "not_match" else "ambiguous",
        )
        return _save(
            session,
            company=company,
            platform=platform,
            purpose=purpose,
            collection_key=collection_key,
            match=match_status(decision.verdict),
            url=search_url,
            fields=fields,
            sources=sources,
            page=search_page,
            external_id=None,
            confident=False,
            hold="ambiguous_match" if decision.verdict == "ambiguous" else None,
        )
    candidate = decision.candidate
    external_id = candidate.external_id
    if decision.verdict == "same" and external_id and owner_company_id(session, platform, external_id, company.id):
        fields, sources = empty_observation(
            platform,
            url=search_url,
            seen_at=seen_at,
            screenshot=search_page.screenshot_path,
            raw_path=search_page.raw_path,
            reason="platform_id_conflict",
        )
        return _save(
            session,
            company=company,
            platform=platform,
            purpose=purpose,
            collection_key=collection_key,
            match="found",
            url=candidate.url or search_url,
            fields=fields,
            sources=sources,
            page=search_page,
            external_id=None,
            confident=False,
            hold="platform_id_conflict",
        )
    card_url = candidate.url or search_url
    card = pages.card(platform, external_id or "", card_url)
    if not external_id and card.document is not None:
        external_id = card.document.external_id
    fields, sources = _read_saved_card(platform, card, cfg, seen_at, model_reader)
    if not external_id:
        saved = _save(
            session,
            company=company,
            platform=platform,
            purpose=purpose,
            collection_key=collection_key,
            match=match_status(decision.verdict),
            url=card.url or card_url,
            fields=fields,
            sources=sources,
            page=card,
            external_id=None,
            confident=False,
            hold="missing_external_id",
        )
        open_task(
            session,
            "missing_external_id",
            "card_snapshot",
            saved.id,
            {"company_id": company.id, "platform": platform},
        )
        return saved
    return _save(
        session,
        company=company,
        platform=platform,
        purpose=purpose,
        collection_key=collection_key,
        match=match_status(decision.verdict),
        url=card_url,
        fields=fields,
        sources=sources,
        page=card,
        external_id=external_id,
        confident=decision.verdict == "same" and decision.phone_match,
        hold=None,
    )


def collect_matched(
    session: Session,
    *,
    company: Company,
    identity: Identity,
    purpose: str,
    collection_key: str,
    pages: FixtureStore,
    cfg: dict[str, Any],
    platforms: tuple[str, ...] = ("yandex", "google"),
    persist_misses: bool = False,
) -> list[MatchOutcome]:
    """Match Yandex and Google to an existing 2GIS card. A miss does not replace a stored snapshot."""
    outcomes: list[MatchOutcome] = []
    for platform in platforms:
        if platform not in _SEARCH:
            raise CollectionFailed(platform)
        existing = find_reusable(session, company.id, platform, collection_key)
        if existing is not None:
            hold = ((existing.field_sources or {}).get("_collection") or {}).get("hold")
            outcomes.append(MatchOutcome(platform, "reused", existing, hold))
            continue
        url = _search_url(cfg, platform, identity)
        page: LoadedPage | None = None
        try:
            page = pages.search(platform, company.id_2gis, url)
            document = page.document if page.document is not None else _SEARCH[platform](page.html, cfg["markers"])
            decision = choose(identity, candidates_from_document(document))
        except (PageNotLoaded, UnexpectedPage, AccessRestricted, InvalidField) as exc:
            if not persist_misses:
                outcomes.append(MatchOutcome(platform, "failed", None, getattr(exc, "access", None)))
                continue
            reason = exc.__class__.__name__ + ": " + str(exc)
            snapshot = _fail(
                session,
                company_id=company.id,
                platform=platform,
                purpose=purpose,
                collection_key=collection_key,
                url=getattr(exc, "url", None) or url,
                screenshot=_screenshot_of(page, pages, exc),
                page=page,
                reason=reason,
                status="not_found",
            )
            outcomes.append(MatchOutcome(platform, "failed", snapshot, None))
            continue
        if decision.verdict != "same" and not persist_misses:
            hold = "ambiguous_match" if decision.verdict == "ambiguous" else None
            if hold:
                open_task(
                    session,
                    hold,
                    "company",
                    company.id,
                    {"platform": platform, "collection_key": collection_key, "url": url},
                )
            outcomes.append(MatchOutcome(platform, decision.verdict, None, hold))
            continue
        snapshot = _from_decision(
            session,
            company,
            platform,
            purpose,
            collection_key,
            pages,
            cfg,
            None,
            page,
            url,
            decision,
        )
        hold = ((snapshot.field_sources or {}).get("_collection") or {}).get("hold")
        if decision.verdict == "same" and not decision.phone_match and not hold:
            open_task(
                session,
                "match_needs_review",
                "card_snapshot",
                snapshot.id,
                {"company_id": company.id, "platform": platform},
            )
            hold = "match_needs_review"
        outcomes.append(MatchOutcome(platform, decision.verdict, snapshot, hold))
    return outcomes
