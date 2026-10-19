from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from zhol.agents.scout.fields import field_keys
from zhol.browser.evidence import screenshot_state, usable_screenshot
from zhol.db.models import CardSnapshot, CompanyPlatformId, CompanyProfile, HumanTask
from zhol.timeutil import now
from zhol.transitions import transition

COLLECTED_BY = "scout"


def open_task(
    session: Session,
    reason: str,
    object_type: str | None,
    object_id: int | None,
    details: dict[str, Any],
) -> HumanTask:
    query = select(HumanTask).where(HumanTask.reason == reason, HumanTask.object_type == object_type, HumanTask.status == "open")
    if object_id is None:
        query = query.where(HumanTask.object_id.is_(None))
    else:
        query = query.where(HumanTask.object_id == object_id)
    if object_type is None:
        query = query.where(HumanTask.object_type.is_(None))
    existing = session.scalar(query)
    if existing is not None:
        return existing
    task = HumanTask(reason=reason, object_type=object_type, object_id=object_id, details=details)
    session.add(task)
    session.flush()
    return task


def remember_services(
    session: Session,
    company_id: int,
    names: list[str],
    *,
    source_ref: str,
    forbidden: list[str] | None = None,
) -> str:
    """Store priced menu names on the company profile. A company name is not a service."""
    blocked = {" ".join(item.casefold().split()) for item in (forbidden or []) if item and item.strip()}
    cleaned: list[str] = []
    for name in names:
        text = " ".join(str(name).split())
        if not text or text.casefold() in blocked or text in cleaned:
            continue
        cleaned.append(text)
    if not cleaned:
        return "услуги не показаны"
    latest = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == company_id).order_by(CompanyProfile.version.desc())
    )
    if latest is not None:
        current = [
            item.get("name")
            for item in (latest.data or {}).get("services") or []
            if isinstance(item, dict) and item.get("name")
        ]
        if current == cleaned:
            return f"профиль {latest.id} без новой версии"
    version = 1 if latest is None else int(latest.version) + 1
    data = {} if latest is None else dict(latest.data or {})
    data["services"] = [{"name": name, "needs_confirmation": False, "source_ref": source_ref} for name in cleaned]
    row = CompanyProfile(company_id=company_id, version=version, data=data)
    session.add(row)
    session.flush()
    return f"профиль {row.id} версия {version}"


def store_listed_services(
    session: Session,
    company_id: int,
    names: list[str],
    *,
    source_ref: str,
    forbidden: list[str] | None = None,
) -> str:
    """Store an explicit service list. A different saved list is a conflict, not a replacement."""
    blocked = {" ".join(item.casefold().split()) for item in (forbidden or []) if item and item.strip()}
    cleaned: list[str] = []
    for name in names:
        text = " ".join(str(name).split())
        if not text or text.casefold() in blocked or text in cleaned:
            continue
        cleaned.append(text)
    if not cleaned:
        return "услуги не показаны"
    latest = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == company_id).order_by(CompanyProfile.version.desc())
    )
    stored = (latest.data or {}) if latest is not None else {}
    current = [
        str(item.get("name"))
        for item in (stored.get("services") or [])
        if isinstance(item, dict) and item.get("name") and item.get("needs_confirmation") is not True
    ]
    if current and current != cleaned:
        open_task(
            session,
            "service_conflict",
            "company",
            company_id,
            {"source_ref": source_ref, "stored": current, "shown": cleaned},
        )
        return "конфликт услуг, список не заменён"
    return remember_services(session, company_id, cleaned, source_ref=source_ref, forbidden=forbidden)


def save_verified_services(
    session: Session,
    id_2gis: str,
    names: list[str],
    *,
    forbidden: list[str] | None = None,
) -> str:
    """Attach services to an existing company. Never inserts a company row."""
    from zhol.db.models import Company

    company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
    if company is None:
        return "компании нет в базе, услуги не записаны"
    return remember_services(
        session,
        company.id,
        names,
        source_ref="field:2gis.prices",
        forbidden=forbidden,
    )


def find_reusable(session: Session, company_id: int, platform: str, collection_key: str) -> CardSnapshot | None:
    rows = session.scalars(
        select(CardSnapshot).where(
            CardSnapshot.company_id == company_id,
            CardSnapshot.platform == platform,
            CardSnapshot.collected_by == COLLECTED_BY,
        )
    ).all()
    for row in rows:
        meta = (row.field_sources or {}).get("_collection") or {}
        if meta.get("key") == collection_key and row.status in ("collected", "verified"):
            return row
    return None


def owner_company_id(session: Session, platform: str, external_id: str, company_id: int) -> int | None:
    owner = session.scalar(
        select(CompanyPlatformId).where(
            CompanyPlatformId.platform == platform,
            CompanyPlatformId.external_id == external_id,
            CompanyPlatformId.company_id != company_id,
        )
    )
    return None if owner is None else owner.company_id


def remember_platform_id(
    session: Session,
    company_id: int,
    platform: str,
    external_id: str | None,
    public_url: str | None,
) -> str | None:
    if not external_id:
        return None
    other = owner_company_id(session, platform, external_id, company_id)
    if other is not None:
        open_task(
            session,
            "platform_id_conflict",
            "company",
            company_id,
            {"platform": platform, "external_id": external_id, "owner_company_id": other},
        )
        return "platform_id_conflict"
    row = session.get(CompanyPlatformId, (company_id, platform))
    if row is None:
        session.add(
            CompanyPlatformId(
                company_id=company_id,
                platform=platform,
                external_id=external_id,
                public_url=public_url,
            )
        )
        return None
    if row.access_status != "none":
        return None
    if row.external_id is None:
        row.external_id = external_id
    elif row.external_id != external_id:
        open_task(
            session,
            "platform_id_conflict",
            "company",
            company_id,
            {"platform": platform, "external_id": external_id, "current_external_id": row.external_id},
        )
        return "platform_id_conflict"
    if row.public_url is None and public_url:
        row.public_url = public_url
    return None


def _marker(collection_key: str, page_file: str | None, kind: str | None, raw_path: str | None, hold: str | None) -> dict[str, Any]:
    return {"key": collection_key, "file": page_file, "kind": kind, "raw_path": raw_path, "hold": hold}


def remember_search_query(snapshot: CardSnapshot, query: str | None) -> None:
    """Keep the search phrase on this snapshot, apart from the business category.

    The phrase is not written into fields.category and does not replace an older snapshot.
    """
    text = " ".join(str(query or "").split())
    if not text:
        return
    _remember_marker(snapshot, search_query=text)


def remember_collection_context(snapshot: CardSnapshot, *, city: str | None, region: str | None) -> None:
    """Record the city selected for this search. It does not overwrite the card city."""
    selected = " ".join(str(city or "").split())
    area = " ".join(str(region or "").split())
    if not selected and not area:
        return
    _remember_marker(
        snapshot,
        collection_city=selected or None,
        collection_region=area or None,
        city_provenance="selected",
    )


def _remember_marker(snapshot: CardSnapshot, **values: Any) -> None:
    sources = dict(snapshot.field_sources or {})
    marker = dict(sources.get("_collection") or {})
    for key, value in values.items():
        if value is not None:
            marker[key] = value
    sources["_collection"] = marker
    snapshot.field_sources = sources
    flag_modified(snapshot, "field_sources")


def insert_snapshot(
    session: Session,
    *,
    company_id: int,
    platform: str,
    purpose: str,
    match_status: str,
    url: str | None,
    fields: dict[str, Any],
    field_sources: dict[str, Any],
    screenshot_path: str | None,
    collection_key: str,
    page_file: str | None,
    kind: str | None,
    raw_path: str | None,
    hold: str | None = None,
    fail_reason: str | None = None,
) -> CardSnapshot:
    kept = usable_screenshot(screenshot_path)
    shot_state = "present" if kept else screenshot_state(screenshot_path)
    sources: dict[str, Any] = {}
    for key, value in field_sources.items():
        if not isinstance(value, dict):
            sources[key] = value
            continue
        copied = dict(value)
        if "screenshot" in copied:
            raw = copied.get("screenshot")
            copied["screenshot"] = usable_screenshot(raw if isinstance(raw, str) else None)
            copied["screenshot_status"] = "present" if copied["screenshot"] else screenshot_state(raw if isinstance(raw, str) else None)
        sources[key] = copied
    marker = _marker(collection_key, page_file, kind, raw_path, hold)
    marker["screenshot_status"] = shot_state
    sources["_collection"] = marker
    for key in field_keys(platform):
        sources.setdefault(
            key,
            {
                "url": url,
                "seen_at": now().isoformat(),
                "screenshot": kept,
                "screenshot_status": shot_state,
                "reason": "not_shown",
            },
        )
    snapshot = CardSnapshot(
        company_id=company_id,
        platform=platform,
        match_status=match_status,
        url=url,
        fields=fields,
        field_sources=sources,
        screenshot_path=kept,
        purpose=purpose,
        collected_at=now(),
        collected_by=COLLECTED_BY,
        status="collected",
        fail_reason=fail_reason,
    )
    session.add(snapshot)
    session.flush()
    if fail_reason:
        transition(snapshot, "failed", session)
    return snapshot
