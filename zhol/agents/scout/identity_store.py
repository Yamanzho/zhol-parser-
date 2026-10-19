"""Store a discovered branch as a company, platform id, and first snapshot.

A 2GIS firm still uses the card collector for its snapshot. A Yandex-only or
Google-only company is stored here, because that walk has no 2GIS card.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.identity import (
    Profile,
    Ref,
    item_key,
    origin_platform,
    profile_from_branch,
    relate,
)
from zhol.agents.scout.persist import find_reusable, insert_snapshot, open_task, remember_platform_id
from zhol.browser.discovery.types import Branch, PlatformEvidence
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, CompanyPlatformId
from zhol.etalon import SOURCE as ETALON_SOURCE

COMPANY_SOURCE = {"fixture": "saved_pages", "manual": "manual_browser"}


@dataclass(frozen=True)
class Stored:
    company: Company
    profile: Profile


@dataclass(frozen=True)
class Outcome:
    action: str
    company: Company | None
    reason: str
    phone_confirmed: bool = False


def save_discovered_company(
    factory: sessionmaker[Session],
    *,
    batch_id: int,
    branch: Branch,
    source_mode: str,
) -> str:
    """Insert one identity. A concurrent insert of the same external id attaches to the winner."""
    for attempt in range(2):
        try:
            with factory() as session, session.begin():
                batch = session.get(CollectionBatch, batch_id)
                if batch is None:
                    return "missing"
                return save_branch(session, batch, branch, source_mode)
        except IntegrityError:
            if attempt == 1:
                raise
    return "conflict"


def save_branch(session: Session, batch: CollectionBatch, branch: Branch, source_mode: str) -> str:
    key = item_key(branch)
    if not key:
        return "skipped"
    known = session.scalar(
        select(CollectionBatchItem.id).where(
            CollectionBatchItem.batch_id == batch.id,
            CollectionBatchItem.id_2gis == key,
        )
    )
    if known is not None:
        return "duplicate"
    outcome = decide(session, branch)
    if outcome.action == "review":
        _blocked(session, batch, key, branch, outcome.reason)
        if outcome.company is not None and outcome.company.source != ETALON_SOURCE:
            _review_snapshot(session, batch, outcome.company, branch)
        return "review"
    company = outcome.company
    created = company is None
    if company is not None and company.source == ETALON_SOURCE:
        _note_skip(batch, branch.firm_id or key, "reference")
        return "skipped"
    if created:
        company = Company(
            id_2gis=branch.firm_id,
            name=branch.name or branch.firm_id or key,
            city=branch.city or batch.city,
            niche=branch.category,
            whatsapp=branch.whatsapp,
            instagram=branch.instagram,
            source=COMPANY_SOURCE[source_mode],
        )
        session.add(company)
        session.flush()
    assert company is not None
    if branch.firm_id and company.id_2gis is None:
        company.id_2gis = branch.firm_id
    elif branch.firm_id and company.id_2gis != branch.firm_id:
        open_task(
            session,
            "platform_id_conflict",
            "company",
            company.id,
            {"platform": "2gis", "external_id": branch.firm_id, "current_external_id": company.id_2gis},
        )
    _store_ids(session, company, branch, created=created, phone_confirmed=outcome.phone_confirmed, by_platform_id=outcome.reason == "platform_id")
    if not branch.firm_id:
        _store_snapshots(session, batch, company, branch, created=created, phone_confirmed=outcome.phone_confirmed)
    session.add(
        CollectionBatchItem(
            batch_id=batch.id,
            id_2gis=key,
            company_id=company.id,
            status="pending" if branch.firm_id else "completed",
            detail={
                "name": branch.name,
                "city": branch.city or batch.city,
                "category": branch.category,
                "origin_platform": origin_platform(branch),
                "discovery": branch.as_progress(),
                "platforms": {name: "found" for name in branch.platforms},
            },
        )
    )
    return "created" if created else "attached"


def decide(session: Session, branch: Branch) -> Outcome:
    owners = _owners(session, profile_from_branch(branch))
    if len(owners) > 1:
        return Outcome("review", None, "platform_id_conflict")
    if len(owners) == 1:
        return Outcome("attach", owners[0], "platform_id", phone_confirmed=True)
    incoming = profile_from_branch(branch)
    matches: list[tuple[Company, bool]] = []
    ambiguous: list[Company] = []
    for stored in _stored(session, branch.city, origin_platform(branch)):
        relation = relate(incoming, stored.profile)
        if relation.kind == "same":
            matches.append((stored.company, relation.phone_confirmed))
        elif relation.kind == "ambiguous":
            ambiguous.append(stored.company)
    if len(matches) == 1 and not ambiguous:
        company, phone = matches[0]
        return Outcome("attach", company, "identity", phone_confirmed=phone)
    if matches or ambiguous:
        company = ambiguous[0] if ambiguous else matches[0][0]
        return Outcome("review", company, "ambiguous")
    return Outcome("create", None, "new")


def _owners(session: Session, profile: Profile) -> list[Company]:
    found: dict[int, Company] = {}
    for ref in profile.refs:
        company = _owner(session, ref)
        if company is not None:
            found[company.id] = company
    return list(found.values())


def _owner(session: Session, ref: Ref) -> Company | None:
    if ref.platform == "2gis":
        return session.scalar(select(Company).where(Company.id_2gis == ref.external_id))
    row = session.scalar(
        select(CompanyPlatformId).where(
            CompanyPlatformId.platform == ref.platform,
            CompanyPlatformId.external_id == ref.external_id,
        )
    )
    if row is None:
        return None
    return session.get(Company, row.company_id)


def _stored(session: Session, city: str | None, platform: str | None) -> list[Stored]:
    if not city:
        return []
    companies = session.scalars(select(Company).where(Company.city == city)).all()
    if not companies:
        return []
    ids = [company.id for company in companies]
    snapshots = session.scalars(select(CardSnapshot).where(CardSnapshot.company_id.in_(ids))).all()
    platforms = session.scalars(select(CompanyPlatformId).where(CompanyPlatformId.company_id.in_(ids))).all()
    by_company: dict[int, list[CardSnapshot]] = {}
    for row in snapshots:
        if row.company_id is not None:
            by_company.setdefault(row.company_id, []).append(row)
    refs: dict[int, list[Ref]] = {}
    for row in platforms:
        if row.external_id:
            refs.setdefault(row.company_id, []).append(Ref(row.platform, row.external_id))
    stored: list[Stored] = []
    for company in companies:
        company_refs = list(refs.get(company.id, []))
        if company.id_2gis and not any(item.platform == "2gis" and item.external_id == company.id_2gis for item in company_refs):
            company_refs.append(Ref("2gis", company.id_2gis))
        rows = by_company.get(company.id, [])
        own = [row for row in rows if platform is not None and row.platform == platform]
        stored.append(
            Stored(
                company,
                Profile(company.name, _newest_text(own, "address"), _newest_phones(own), tuple(company_refs)),
            )
        )
    return stored


def _newest_text(rows: list[CardSnapshot], key: str) -> str | None:
    chosen: CardSnapshot | None = None
    value: str | None = None
    for row in rows:
        text = (row.fields or {}).get(key)
        if not isinstance(text, str) or not text.strip():
            continue
        if chosen is None or (row.collected_at, row.id) > (chosen.collected_at, chosen.id):
            chosen = row
            value = text.strip()
    return value


def _newest_phones(rows: list[CardSnapshot]) -> tuple[str, ...]:
    chosen: CardSnapshot | None = None
    phones: tuple[str, ...] = ()
    for row in rows:
        raw = (row.fields or {}).get("phones")
        if not isinstance(raw, list) or not raw:
            continue
        if chosen is None or (row.collected_at, row.id) > (chosen.collected_at, chosen.id):
            chosen = row
            phones = tuple(str(item) for item in raw if str(item).strip())
    return phones


def _store_ids(
    session: Session,
    company: Company,
    branch: Branch,
    *,
    created: bool,
    phone_confirmed: bool,
    by_platform_id: bool,
) -> None:
    origin = origin_platform(branch)
    for platform, evidence in branch.platforms.items():
        if not evidence.external_id:
            continue
        if not _write_id(created, by_platform_id, phone_confirmed, platform == origin, evidence.phone_confirmed):
            continue
        remember_platform_id(session, company.id, platform, evidence.external_id, evidence.url)


def _write_id(created: bool, by_platform_id: bool, db_phone: bool, is_origin: bool, evidence_phone: bool) -> bool:
    if created:
        return is_origin or evidence_phone
    if by_platform_id:
        return is_origin or evidence_phone
    return db_phone and (is_origin or evidence_phone)


def _store_snapshots(
    session: Session,
    batch: CollectionBatch,
    company: Company,
    branch: Branch,
    *,
    created: bool,
    phone_confirmed: bool,
) -> None:
    origin = origin_platform(branch)
    for platform, evidence in branch.platforms.items():
        write_id = _write_id(created, False, phone_confirmed, platform == origin, evidence.phone_confirmed)
        hold = None if write_id or (created and platform == origin) else "match_needs_review"
        _snapshot(session, batch, company, branch, evidence, hold=hold, match_status="found")
        if hold == "match_needs_review":
            open_task(
                session,
                "match_needs_review",
                "company",
                company.id,
                {"platform": platform, "external_id": evidence.external_id, "batch_id": batch.id},
            )
    for alternative in branch.alternatives:
        if not isinstance(alternative, dict):
            continue
        platform = str(alternative.get("platform") or "")
        if platform not in {"2gis", "yandex", "google"}:
            continue
        evidence = PlatformEvidence(
            platform,
            alternative.get("url"),
            alternative.get("external_id"),
            alternative.get("screenshot_path"),
            str(alternative.get("seen_at") or ""),
            str(alternative.get("source_path") or ""),
            False,
        )
        shadow = Branch(
            name=alternative.get("name"),
            category=branch.category,
            subcategory=None,
            sector=None,
            address=alternative.get("address"),
            city=branch.city,
            phones=tuple(str(item) for item in alternative.get("phones") or []),
            phone_display=tuple(str(item) for item in alternative.get("phone_display") or []),
        )
        _snapshot(session, batch, company, shadow, evidence, hold="ambiguous_match", match_status="not_found")
        open_task(
            session,
            "ambiguous_match",
            "company",
            company.id,
            {"platform": platform, "external_id": evidence.external_id, "batch_id": batch.id},
        )


def _review_snapshot(session: Session, batch: CollectionBatch, company: Company, branch: Branch) -> None:
    origin = origin_platform(branch)
    evidence = None if origin is None else branch.platforms.get(origin)
    if evidence is None:
        return
    _snapshot(session, batch, company, branch, evidence, hold="ambiguous_match", match_status="not_found")
    open_task(
        session,
        "ambiguous_match",
        "company",
        company.id,
        {"platform": evidence.platform, "external_id": evidence.external_id, "batch_id": batch.id},
    )


def _snapshot(
    session: Session,
    batch: CollectionBatch,
    company: Company,
    branch: Branch,
    evidence: PlatformEvidence,
    *,
    hold: str | None,
    match_status: str,
) -> CardSnapshot | None:
    if not evidence.external_id:
        return None
    collection_key = f"discovery:{batch.id}:{evidence.platform}:{evidence.external_id}:{hold or 'found'}"
    existing = find_reusable(session, company.id, evidence.platform, collection_key)
    if existing is not None:
        return existing
    allowed = set(field_keys(evidence.platform))
    raw = {
        "name": branch.name,
        "address": branch.address,
        "phones": list(branch.phones) if branch.phones else None,
        "category": branch.category,
        "city": branch.city,
        "whatsapp": branch.whatsapp,
        "instagram": branch.instagram,
        "website": branch.website,
        "email": branch.email,
        "rating": branch.rating,
        "reviews_count": branch.review_count,
    }
    fields = {key: value for key, value in raw.items() if key in allowed and value not in (None, "", [])}
    sources = {}
    for key in fields:
        source = {
            "url": evidence.url,
            "seen_at": evidence.seen_at,
            "screenshot": evidence.screenshot_path,
            "source_path": evidence.source_path,
            "read_by": "code",
        }
        if key == "phones" and branch.phone_display:
            source["display"] = list(branch.phone_display)
        if hold == "ambiguous_match":
            source["reason"] = "ambiguous"
        sources[key] = source
    return insert_snapshot(
        session,
        company_id=company.id,
        platform=evidence.platform,
        purpose="prospect",
        match_status=match_status,
        url=evidence.url,
        fields=fields,
        field_sources=sources,
        screenshot_path=evidence.screenshot_path,
        collection_key=collection_key,
        page_file=None,
        kind="discovery",
        raw_path=evidence.source_path,
        hold=hold,
    )


def _blocked(session: Session, batch: CollectionBatch, key: str, branch: Branch, reason: str) -> None:
    origin = origin_platform(branch)
    evidence = None if origin is None else branch.platforms.get(origin)
    session.add(
        CollectionBatchItem(
            batch_id=batch.id,
            id_2gis=key,
            company_id=None,
            status="blocked",
            detail={
                "item_key": key,
                "platform": None if evidence is None else evidence.platform,
                "external_id": None if evidence is None else evidence.external_id,
                "url": None if evidence is None else evidence.url,
                "name": branch.name,
                "address": branch.address,
                "phones": list(branch.phones),
                "reason": reason,
                "screenshot_path": None if evidence is None else evidence.screenshot_path,
                "seen_at": None if evidence is None else evidence.seen_at,
                "source_path": None if evidence is None else evidence.source_path,
            },
        )
    )


def _note_skip(batch: CollectionBatch, firm_id: str, reason: str) -> None:
    progress = dict(batch.progress or {})
    skipped = list(progress.get("skipped") or [])
    skipped.append({"firm_id": firm_id, "reason": reason})
    progress["skipped"] = skipped
    batch.progress = progress
