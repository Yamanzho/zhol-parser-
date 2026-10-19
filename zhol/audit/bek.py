"""Import a Bek audit workbook and store listing improvement plans.

The import does not open a cabinet, submit a form, or mark an edit complete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from zhol.agents.scout.normalize import addresses_compatible, names_compatible, phones_overlap
from zhol.audit.plan import classify, findings_for, platform_status
from zhol.audit.workbook import AuditRow, WorkbookError, read_workbook
from zhol.db.models import (
    Audit,
    CardSnapshot,
    ChangeItem,
    ChangePackage,
    ClientFile,
    Company,
    CompanyPlatformId,
    CompanyProfile,
    HumanTask,
    ListingImprovementPlan,
    Order,
)
from zhol.etalon import VERIFIED_LAYERS, YANDEX_ORG, EtalonError, _value, collected_at
from zhol.scoring import derive
from zhol.scoring.adapter import COLUMNS, STATUS_COLUMNS, UNSEEN_KEYS
from zhol.timeutil import now

SOURCE = "bek_audit"
COLLECTED_BY = "bek_audit"
PROMPT = "bek-audit-import-1"
PLATFORMS = ("2gis", "yandex", "google")
EXTRA_COLUMNS = {
    "2гис_описание": ("2gis", "description"),
    "2гис_часы": ("2gis", "hours"),
    "2гис_рубрика": ("2gis", "category"),
    "яндекс_описание": ("yandex", "description"),
    "яндекс_часы": ("yandex", "hours"),
    "яндекс_рубрика": ("yandex", "category"),
    "яндекс_сайт": ("yandex", "website"),
    "google_описание": ("google", "description"),
    "google_часы": ("google", "hours"),
    "google_рубрика": ("google", "category"),
    "google_фото": ("google", "photos_count"),
}


class BekAuditError(ValueError):
    pass


@dataclass
class ImportStats:
    sheet: str = ""
    sheets: list[str] = field(default_factory=list)
    companies: int = 0
    snapshots: int = 0
    audits: int = 0
    plans: int = 0
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    skipped_newer: list[str] = field(default_factory=list)
    tasks_by_type: dict[str, int] = field(default_factory=dict)


@dataclass
class _Identity:
    company_id: int
    id_2gis: str
    name: str
    address: str | None
    phones: list[str]


def import_bek_audit(session: Session, path: str | Path) -> ImportStats:
    try:
        book = read_workbook(path)
    except WorkbookError as exc:
        raise BekAuditError(str(exc)) from exc
    stats = ImportStats(sheet=book.sheet, sheets=list(book.sheets))
    known = _identities(session)
    seen_ids: set[str] = set()
    used: set[int] = set()
    imported: list[_Identity] = []
    for row in book.rows:
        outcome = _import_row(session, row, book, known, seen_ids, used, stats)
        if outcome is not None:
            imported.append(outcome)
            known.append(outcome)
    _flag_pairs(session, imported, stats)
    return stats


def company_plan(session: Session, company_id: int) -> dict[str, Any]:
    row = session.scalar(
        select(ListingImprovementPlan)
        .where(ListingImprovementPlan.company_id == company_id)
        .order_by(ListingImprovementPlan.id.desc())
    )
    if row is None:
        raise BekAuditError(f"company {company_id} has no Bek audit plan")
    plan = _refresh(session, row.plan, company_id)
    row.plan = plan
    flag_modified(row, "plan")
    return plan


def read_company_plan(session: Session, company_id: int) -> dict[str, Any] | None:
    """Return the stored plan for display. This does not write the refreshed copy back."""
    row = session.scalar(
        select(ListingImprovementPlan)
        .where(ListingImprovementPlan.company_id == company_id)
        .order_by(ListingImprovementPlan.id.desc())
    )
    if row is None or not isinstance(row.plan, dict):
        return None
    try:
        stored = json.loads(json.dumps(row.plan))
        shown = _refresh(session, json.loads(json.dumps(stored)), company_id)
        display = "stored_plan_refreshed_for_display"
    except Exception:
        stored = dict(row.plan)
        shown = stored
        display = "stored_plan"
    shown["listing_modified"] = False
    return {
        "id": row.id,
        "company_id": company_id,
        "audit_id": row.audit_id,
        "source_key": row.source_key,
        "source": SOURCE,
        "created_at": None if row.created_at is None else row.created_at.isoformat(),
        "display": display,
        "listing_modified": False,
        "plan": shown,
    }


def task_totals(plans: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for plan in plans:
        for task in plan.get("automatic_preparation") or []:
            totals[task["type"]] = totals.get(task["type"], 0) + 1
        for task in plan.get("manual_work") or []:
            totals[task["type"]] = totals.get(task["type"], 0) + 1
    return totals


def _import_row(session, row: AuditRow, book, known, seen_ids, used, stats: ImportStats) -> _Identity | None:
    if row.skip_reason:
        _remember(session, stats, row.skip_kind or "unmatched", row, row.skip_reason, None)
        return None
    layer = row.text("слой")
    # The etalon file uses the layer to drop formula rows. A later section of the original workbook
    # names its own sector; that label is not one of those three sampled layers.
    if layer and layer not in VERIFIED_LAYERS and row.layout != "section":
        _remember(session, stats, "unmatched", row, "unverified_layer", None)
        return None
    try:
        seen = collected_at({"дата_просмотра": row.text("дата_просмотра") or ""})
    except (EtalonError, ValueError):
        _remember(session, stats, "unmatched", row, "missing_source_date", None)
        return None
    id_2gis, id_problem = _company_id(row)
    if id_problem:
        _remember(session, stats, "ambiguous", row, id_problem, None)
        return None
    if id_2gis and id_2gis in seen_ids:
        _remember(session, stats, "ambiguous", row, "duplicate id_2gis", None)
        return None
    company = _match_company(session, row, id_2gis, known, stats)
    if company is None:
        return None
    if company.id in used:
        _remember(session, stats, "ambiguous", row, "row matches a company already taken from this workbook", company.id)
        return None
    if id_2gis:
        seen_ids.add(id_2gis)
    used.add(company.id)
    cards, snapshots = _store_snapshots(session, company, row, seen, stats)
    audit = _store_audit(session, company, row, book.sheet, seen, snapshots, stats)
    _store_plan(session, company, row, book, seen, cards, audit, stats)
    address = (cards.get("2gis") or {}).get("fields", {}).get("address")
    phones = _phones(row)
    return _Identity(company.id, company.id_2gis, company.name, address if isinstance(address, str) else None, phones)


def _company_id(row: AuditRow) -> tuple[str | None, str | None]:
    raw = (row.text("id_2gis") or "").strip()
    from_url = None
    url = row.text("url_2gis") or ""
    match = re.search(r"/firm/(\d+)", url)
    if match:
        from_url = match.group(1)
    if raw and from_url and raw != from_url:
        return None, "id_2gis does not match the 2GIS URL"
    chosen = raw or from_url
    if chosen and not chosen.isdigit():
        return None, "id_2gis is not numeric"
    return chosen, None


def _match_company(session, row: AuditRow, id_2gis: str | None, known: list[_Identity], stats: ImportStats) -> Company | None:
    if id_2gis:
        company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
        if company is not None:
            name = (row.text("название") or "").strip()
            if name and not names_compatible(name, company.name):
                _remember(session, stats, "ambiguous", row, "id_2gis belongs to a company with a different name", company.id)
                return None
            niche = (row.text("тип") or "").strip()
            if niche and not company.niche:
                company.niche = niche
            return company
        name = (row.text("название") or "").strip()
        if not name:
            _remember(session, stats, "unmatched", row, "missing company name", None)
            return None
        company = Company(
            id_2gis=id_2gis,
            name=name,
            city=(row.text("город") or "").strip() or "Алматы",
            niche=(row.text("тип") or "").strip() or None,
            whatsapp=(row.text("номер_whatsapp") or "").strip() or None,
            instagram=(row.text("аккаунт_instagram") or "").strip() or None,
            source=SOURCE,
        )
        session.add(company)
        session.flush()
        stats.companies += 1
        _platform_ids(session, company.id, id_2gis, row, stats)
        return company
    hits, problem = _name_hits(row, known)
    if problem:
        _remember(session, stats, "ambiguous", row, problem, None)
        return None
    if len(hits) == 1:
        return session.get(Company, hits[0].company_id)
    _remember(session, stats, "unmatched", row, "no reliable company id", None)
    return None


def _name_hits(row: AuditRow, known: list[_Identity]) -> tuple[list[_Identity], str | None]:
    name = row.text("название")
    address = row.text("адрес_2гис")
    phones = _phones(row)
    same = [item for item in known if names_compatible(name, item.name) and addresses_compatible(address, item.address)]
    if len(same) > 1:
        return same, "name and address match more than one company"
    phone_hits = [item for item in known if phones_overlap(phones, item.phones)]
    conflicts = [
        item for item in phone_hits if item.address and address and not addresses_compatible(address, item.address)
    ]
    if conflicts:
        return conflicts, "phone matches a company at a different address"
    if len(same) == 1:
        return same, None
    if len(phone_hits) > 1:
        return phone_hits, "phone matches more than one company"
    if len(phone_hits) == 1 and (not address or not phone_hits[0].address or addresses_compatible(address, phone_hits[0].address)):
        return phone_hits, None
    return [], None


def _store_snapshots(session, company: Company, row: AuditRow, seen: datetime, stats: ImportStats):
    urls = {
        "2gis": (row.text("url_2gis") or "").strip() or None,
        "yandex": (row.text("url_яндекс") or "").strip() or None,
        "google": (row.text("url_google") or "").strip() or None,
    }
    cards: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, CardSnapshot] = {}
    for platform in PLATFORMS:
        built = _card(platform, row, urls[platform], seen)
        if built is None:
            continue
        newer = _newer_verified(session, company.id, platform, seen)
        if newer is not None:
            stats.skipped_newer.append(f"{company.id_2gis} {platform}")
            cards[platform] = {
                "match_status": newer.match_status,
                "url": newer.url,
                "fields": {},
                "sources": {},
                "snapshot_id": newer.id,
                "skipped_newer": True,
            }
            continue
        existing = session.scalar(
            select(CardSnapshot).where(
                CardSnapshot.company_id == company.id,
                CardSnapshot.platform == platform,
                CardSnapshot.collected_by == COLLECTED_BY,
                CardSnapshot.collected_at == seen,
            )
        )
        if existing is None:
            existing = CardSnapshot(
                company_id=company.id,
                platform=platform,
                purpose="prospect",
                collected_at=seen,
                collected_by=COLLECTED_BY,
                status="collected",
                match_status=built["match_status"],
                url=built["url"],
                fields=built["fields"],
                field_sources=built["sources"],
            )
            session.add(existing)
            session.flush()
            stats.snapshots += 1
        snapshots[platform] = existing
        cards[platform] = {
            "match_status": existing.match_status,
            "url": existing.url,
            "fields": existing.fields,
            "sources": existing.field_sources,
            "snapshot_id": existing.id,
            "skipped_newer": False,
        }
    _platform_ids(session, company.id, company.id_2gis, row, stats)
    return cards, snapshots


def _store_audit(session, company, row: AuditRow, sheet: str, seen: datetime, snapshots: dict[str, CardSnapshot], stats: ImportStats):
    if not snapshots:
        return None
    ids = sorted(item.id for item in snapshots.values())
    existing = session.scalar(
        select(Audit).where(
            Audit.company_id == company.id,
            Audit.snapshot_ids == ids,
            Audit.prompt_version == PROMPT,
        )
    )
    if existing is not None:
        return existing
    score_total = _score(row)
    if score_total is None:
        return None
    texts = _notes(row)
    twogis = snapshots.get("2gis")
    audit = Audit(
        company_id=company.id,
        snapshot_ids=ids,
        score_total=score_total,
        score_groups=_groups(row),
        derived=derive(twogis) if twogis is not None else {},
        discrepancies=[{"legacy_text": texts[0]}] if texts else [],
        gaps=[],
        observations={
            "source": SOURCE,
            "sheet": sheet,
            "collected_on": seen.date().isoformat(),
            "texts": _saved_observations(row),
            **({"category": row.text("тип"), "layer": row.text("слой")} if row.layout == "section" else {}),
        },
        status="draft",
        prompt_version=PROMPT,
        model=None,
        created_at=now(),
    )
    session.add(audit)
    session.flush()
    stats.audits += 1
    return audit


def _store_plan(session, company: Company, row: AuditRow, book, seen: datetime, cards, audit, stats: ImportStats) -> None:
    source_key = f"bek:{company.id_2gis}:{seen.date().isoformat()}"
    existing = session.scalar(
        select(ListingImprovementPlan).where(
            ListingImprovementPlan.company_id == company.id,
            ListingImprovementPlan.source_key == source_key,
        )
    )
    if existing is not None:
        return
    notes = _notes(row)
    evidence = {
        "sheet": book.sheet,
        "source_date": seen.date().isoformat(),
        "observations": notes,
        "platforms": cards,
    }
    # «Не найдена» in a shifted section is a search result, not proof the listing does not exist.
    if row.text("google_проверка") == "не найдена":
        evidence["google_listing"] = "needs_verification"
    findings = findings_for(evidence)
    plan = _plan_document(session, company, book, seen, cards, findings, audit, _saved_observations(row))
    if row.layout == "section":
        plan["category"] = row.text("тип")
        plan["layer"] = row.text("слой")
    session.add(
        ListingImprovementPlan(
            company_id=company.id,
            audit_id=None if audit is None else audit.id,
            source_key=source_key,
            plan=plan,
            created_at=now(),
        )
    )
    session.flush()
    stats.plans += 1
    for task_type, count in task_totals([plan]).items():
        stats.tasks_by_type[task_type] = stats.tasks_by_type.get(task_type, 0) + count


def _plan_document(session, company: Company, book, seen: datetime, cards, findings, audit, notes: list[str]) -> dict[str, Any]:
    tasks = classify(findings, _readiness(session, company.id), company.id)
    address = None
    branch_count = None
    twogis = cards.get("2gis") or {}
    fields = twogis.get("fields") or {}
    if isinstance(fields.get("address"), str):
        address = fields["address"]
    if isinstance(fields.get("branches"), int):
        branch_count = fields["branches"]
    platforms = {}
    for platform in PLATFORMS:
        card = cards.get(platform)
        if card is None:
            continue
        own = [task for task in tasks if task["platform"] == platform]
        platforms[platform] = {
            "match_status": card.get("match_status"),
            "url": card.get("url"),
            "snapshot_id": card.get("snapshot_id"),
            "status": platform_status(own, skipped=bool(card.get("skipped_newer"))),
            "tasks": own,
        }
    return {
        "company_id": company.id,
        "id_2gis": company.id_2gis,
        "name": company.name,
        "branch": {
            "company_id": company.id,
            "id_2gis": company.id_2gis,
            "name": company.name,
            "address": address,
            "branch_count": branch_count,
        },
        "score_total": None if audit is None else audit.score_total,
        "score_groups": {} if audit is None else audit.score_groups,
        "source_date": seen.date().isoformat(),
        "source_sheet": book.sheet,
        "source_file": Path(book.path).name,
        "observations": notes,
        "findings": findings,
        "platforms": platforms,
        "automatic_preparation": [task for task in tasks if task["lane"] == "automatic_preparation"],
        "manual_work": [task for task in tasks if task["lane"] == "manual"],
        "listing_modified": False,
    }


def _refresh(session, plan: dict[str, Any], company_id: int) -> dict[str, Any]:
    tasks = classify(plan.get("findings") or [], _readiness(session, company_id), company_id)
    updated = dict(plan)
    platforms = {}
    for platform, card in (plan.get("platforms") or {}).items():
        own = [task for task in tasks if task["platform"] == platform]
        platforms[platform] = {
            **{key: card.get(key) for key in ("match_status", "url", "snapshot_id")},
            "status": platform_status(own, skipped=card.get("status") == "skipped_newer_verified"),
            "tasks": own,
        }
    updated["platforms"] = platforms
    updated["automatic_preparation"] = [task for task in tasks if task["lane"] == "automatic_preparation"]
    updated["manual_work"] = [task for task in tasks if task["lane"] == "manual"]
    updated["listing_modified"] = False
    updated["company_id"] = company_id
    return updated


def _readiness(session: Session, company_id: int) -> dict[str, Any]:
    profile_row = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == company_id).order_by(CompanyProfile.version.desc())
    )
    access = {
        row.platform: row.access_status
        for row in session.scalars(select(CompanyPlatformId).where(CompanyPlatformId.company_id == company_id))
    }
    photos = session.scalar(
        select(func.count()).select_from(ClientFile).where(ClientFile.company_id == company_id, ClientFile.kind == "photo_real")
    )
    consents: set[tuple[str, str]] = set()
    order_ids = list(session.scalars(select(Order.id).where(Order.company_id == company_id)))
    if order_ids:
        items = session.scalars(
            select(ChangeItem)
            .join(ChangePackage, ChangeItem.package_id == ChangePackage.id)
            .where(ChangePackage.order_id.in_(order_ids), ChangeItem.nap_consent_ref.is_not(None))
        )
        consents = {(item.platform, item.field) for item in items}
    return {
        "profile": {} if profile_row is None else profile_row.data,
        "access": access,
        "photo_files": int(photos or 0),
        "nap_consents": consents,
    }


def _card(platform: str, row: AuditRow, url: str | None, seen: datetime) -> dict[str, Any] | None:
    columns = [column for item_platform, _key, column in COLUMNS if item_platform == platform]
    extras = [column for column, (item_platform, _key) in EXTRA_COLUMNS.items() if item_platform == platform]
    status_column = None if platform == "2gis" else STATUS_COLUMNS[platform][0]
    touched = [column for column in [*columns, *extras] if column in row.values or column in row.formulas]
    if status_column and (status_column in row.values or status_column in row.formulas):
        touched.append(status_column)
    if platform != "2gis" and not touched:
        return None
    fields: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    for item_platform, key, column in COLUMNS:
        if item_platform != platform:
            continue
        fields[key], sources[key] = _read(platform, key, column, row, url, seen)
    for key in UNSEEN_KEYS[platform]:
        fields.setdefault(key, None)
        sources.setdefault(key, _source(url, seen, None, False))
    for column, (item_platform, key) in EXTRA_COLUMNS.items():
        if item_platform != platform:
            continue
        if column in row.values or column in row.formulas:
            fields[key], sources[key] = _read(platform, key, column, row, url, seen)
    match = "found" if platform == "2gis" else _status(platform, row, url, seen, sources)
    if match is None:
        return None
    return {"match_status": match, "url": url, "fields": fields, "sources": sources}


def _read(platform: str, key: str, column: str, row: AuditRow, url: str | None, seen: datetime):
    if column in row.formulas:
        value, source = None, _source(url, seen, column, True)
    elif column not in row.values:
        value, source = None, _source(url, seen, None, False)
    else:
        try:
            value = _value(platform, key, row.values[column], column)
        except EtalonError:
            if key in EXTRA_COLUMNS.values() or key in {"description", "hours", "category", "website", "photos_count"}:
                raw = row.values[column].strip()
                value = int(raw) if key == "photos_count" and raw.isdigit() else raw or None
            else:
                raise
        source = _source(url, seen, column, False)
    link = row.hyperlinks.get(column)
    if link:
        source = {**source, "hyperlink": link}
    return value, source


def _status(platform: str, row: AuditRow, url: str | None, seen: datetime, sources: dict[str, Any]) -> str | None:
    column, mapping = STATUS_COLUMNS[platform]
    if column in row.formulas:
        sources["match_status"] = _source(url, seen, column, True)
        return None
    if column not in row.values:
        return None
    raw = row.values[column].strip()
    if raw not in mapping:
        raise EtalonError(f"{column}: unknown value {raw!r}")
    sources["match_status"] = _source(url, seen, column, False)
    return mapping[raw]


def _source(url: str | None, seen: datetime, column: str | None, formula: bool) -> dict[str, Any]:
    return {
        "url": url,
        "seen_at": seen.isoformat(),
        "screenshot": None,
        "read_by": "manual",
        "source": SOURCE,
        "csv_column": column,
        "formula": formula,
    }


def _newer_verified(session, company_id: int, platform: str, seen: datetime) -> CardSnapshot | None:
    """Skip the import when this platform already has a newer collected or verified snapshot.

    The existing row is left as it was. Fields from another platform are not copied in.
    """
    return session.scalar(
        select(CardSnapshot)
        .where(
            CardSnapshot.company_id == company_id,
            CardSnapshot.platform == platform,
            CardSnapshot.status.in_(("collected", "verified")),
            CardSnapshot.collected_at > seen,
        )
        .order_by(CardSnapshot.collected_at.desc(), CardSnapshot.id.desc())
    )


def _platform_ids(session, company_id: int, id_2gis: str, row: AuditRow, stats: ImportStats) -> None:
    wanted: dict[str, tuple[str | None, str | None]] = {"2gis": (id_2gis, (row.text("url_2gis") or "").strip() or None)}
    yandex_url = (row.text("url_яндекс") or "").strip()
    if yandex_url:
        match = YANDEX_ORG.search(yandex_url)
        wanted["yandex"] = (match.group(1) if match else None, yandex_url)
    for platform, (external_id, url) in wanted.items():
        current = session.get(CompanyPlatformId, (company_id, platform))
        if current is not None:
            continue
        if external_id:
            owner = session.scalar(
                select(CompanyPlatformId).where(
                    CompanyPlatformId.platform == platform,
                    CompanyPlatformId.external_id == external_id,
                    CompanyPlatformId.company_id != company_id,
                )
            )
            if owner is not None:
                _open(session, "bek_audit_ambiguous", f"platform-id:{platform}:{external_id}", {
                    "row_key": f"platform-id:{platform}:{external_id}",
                    "platform": platform,
                    "external_id": external_id,
                    "company_id": company_id,
                    "reason": "external id already belongs to another company",
                }, company_id)
                stats.ambiguous.append({"row_key": f"platform-id:{platform}:{external_id}", "reason": "external id conflict"})
                external_id = None
        session.add(
            CompanyPlatformId(
                company_id=company_id,
                platform=platform,
                external_id=external_id,
                public_url=url,
            )
        )


def _flag_pairs(session, imported: list[_Identity], stats: ImportStats) -> None:
    for index, left in enumerate(imported):
        for right in imported[index + 1 :]:
            if left.id_2gis == right.id_2gis:
                continue
            if not names_compatible(left.name, right.name) or not addresses_compatible(left.address, right.address):
                continue
            pair = tuple(sorted((left.id_2gis, right.id_2gis)))
            _open(
                session,
                "bek_audit_ambiguous",
                f"pair:{pair[0]}:{pair[1]}",
                {
                    "row_key": f"pair:{pair[0]}:{pair[1]}",
                    "id_2gis": list(pair),
                    "name": left.name,
                    "address": left.address,
                    "reason": "same name and address on two company ids",
                },
                left.company_id,
            )
            stats.ambiguous.append({"row_key": f"pair:{pair[0]}:{pair[1]}", "reason": "same name and address"})


def _remember(session, stats: ImportStats, kind: str, row: AuditRow, reason: str, company_id: int | None) -> None:
    record = {"row": row.excel_row, "name": row.text("название"), "reason": reason, "row_key": f"{kind}:{row.excel_row}:{reason}"}
    if kind == "ambiguous":
        stats.ambiguous.append(record)
        _open(session, "bek_audit_ambiguous", record["row_key"], record, company_id)
    else:
        stats.unmatched.append(record)
        _open(session, "bek_audit_unmatched", record["row_key"], record, company_id)


def _open(session, reason: str, row_key: str, details: dict[str, Any], company_id: int | None) -> None:
    rows = session.scalars(select(HumanTask).where(HumanTask.reason == reason, HumanTask.status == "open")).all()
    if any((item.details or {}).get("row_key") == row_key for item in rows):
        return
    session.add(HumanTask(reason=reason, object_type="company", object_id=company_id, details=details, status="open", created_at=now()))
    session.flush()


def _identities(session: Session) -> list[_Identity]:
    found: list[_Identity] = []
    for company in session.scalars(select(Company)):
        snapshot = session.scalar(
            select(CardSnapshot)
            .where(CardSnapshot.company_id == company.id, CardSnapshot.platform == "2gis")
            .order_by(CardSnapshot.collected_at.desc())
        )
        fields = {} if snapshot is None else snapshot.fields
        address = fields.get("address") if isinstance(fields.get("address"), str) else None
        phones: list[str] = []
        if company.whatsapp:
            phones.append(company.whatsapp)
        raw_phones = fields.get("phones")
        if isinstance(raw_phones, list):
            phones.extend(str(item) for item in raw_phones)
        found.append(_Identity(company.id, company.id_2gis, company.name, address, phones))
    return found


def _phones(row: AuditRow) -> list[str]:
    raw = row.text("контактные_телефоны") or ""
    phones = [part.strip() for part in raw.split(",") if part.strip()]
    whatsapp = (row.text("номер_whatsapp") or "").strip()
    if whatsapp:
        phones.append(whatsapp)
    return phones


def _notes(row: AuditRow) -> list[str]:
    """Cross-platform notes only. A sales sentence is not itself a reconcile task."""
    texts: list[str] = []
    for column in ("расхождения", "не_хватает"):
        text = row.text(column)
        if text:
            texts.append(text)
    texts.extend(text for _header, text in row.notes if text not in texts)
    return texts


def _saved_observations(row: AuditRow) -> list[str]:
    texts: list[str] = []
    gap = row.text("главные_пробелы")
    if gap:
        texts.append(gap)
    for text in _notes(row):
        if text not in texts:
            texts.append(text)
    return texts


def _score(row: AuditRow) -> int | None:
    if "балл_итого" in row.formulas or "балл_итого" not in row.values:
        return None
    raw = row.values["балл_итого"].strip()
    if not raw:
        return None
    return int(raw)


def _groups(row: AuditRow) -> dict[str, int]:
    groups = {}
    for column, key in (("балл_отзывы", "reviews"), ("балл_цены", "prices"), ("балл_фото", "photos"), ("балл_контакты", "contacts")):
        if column in row.formulas or column not in row.values or not row.values[column].strip():
            continue
        groups[key] = int(row.values[column])
    return groups
