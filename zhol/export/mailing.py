"""Excel handoff for Bek. Reads stored snapshots only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.agents.scout.enrich import resolve_contacts
from zhol.db.models import Audit, CardSnapshot, Company, CompanyProfile
from zhol.timeutil import ALMATY

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATEGORIES = ROOT / "config" / "categories.yaml"
READY = frozenset({"collected", "verified"})
BLOCKED = frozenset({"robot_check", "access_blocked"})
FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")

COLUMNS = (
    "Company ID",
    "Company Name",
    "Business Sector",
    "Business Category",
    "Business Subcategory",
    "Services / Activities",
    "Branch Name",
    "City",
    "District",
    "Address",
    "Phone",
    "WhatsApp",
    "Instagram",
    "Website",
    "Email",
    "2GIS URL",
    "2GIS Rating",
    "2GIS Review Count",
    "2GIS Photo Count",
    "2GIS Description",
    "2GIS Opening Hours",
    "Google URL",
    "Google Rating",
    "Google Review Count",
    "Google Photo Count",
    "Google Description",
    "Google Opening Hours",
    "Yandex URL",
    "Yandex Rating",
    "Yandex Review Count",
    "Yandex Photo Count",
    "Yandex Description",
    "Yandex Opening Hours",
    "Overall Completeness Score",
    "Missing Information",
    "Identified Problems",
    "Source",
    "Collection Date",
    "Screenshot Path",
    "Verification Status",
)

_PLATFORM_COLUMNS = {
    "2gis": ("2GIS URL", "2GIS Rating", "2GIS Review Count", "2GIS Photo Count", "2GIS Description", "2GIS Opening Hours"),
    "google": ("Google URL", "Google Rating", "Google Review Count", "Google Photo Count", "Google Description", "Google Opening Hours"),
    "yandex": ("Yandex URL", "Yandex Rating", "Yandex Review Count", "Yandex Photo Count", "Yandex Description", "Yandex Opening Hours"),
}
_COUNT_KEYS = ("reviews_count", "photos_count")
_MISSING_KEYS = (
    ("2gis", "category", "2gis.category"),
    ("2gis", "phones", "2gis.phones"),
    ("2gis", "whatsapp", "2gis.whatsapp"),
    ("2gis", "instagram", "2gis.instagram"),
    ("2gis", "link", "2gis.link"),
    ("2gis", "rating", "2gis.rating"),
    ("2gis", "reviews_count", "2gis.reviews_count"),
    ("2gis", "photos_count", "2gis.photos_count"),
    ("yandex", "category", "yandex.category"),
    ("yandex", "rating", "yandex.rating"),
    ("yandex", "reviews_count", "yandex.reviews_count"),
    ("yandex", "photos_count", "yandex.photos_count"),
    ("google", "category", "google.category"),
    ("google", "rating", "google.rating"),
    ("google", "reviews_count", "google.reviews_count"),
    ("google", "website", "google.website"),
)


@dataclass(frozen=True)
class ClassTable:
    exact: dict[str, tuple[str, str, str]]
    ambiguous: frozenset[str]


def load_categories(path: str | Path | None = None) -> ClassTable:
    file = Path(path) if path is not None else DEFAULT_CATEGORIES
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{file} must be a mapping")
    ambiguous = frozenset(_key(item) for item in data.get("ambiguous") or [])
    exact: dict[str, tuple[str, str, str]] = {}
    for entry in data.get("entries") or []:
        triple = (
            _text(entry.get("sector")) or "",
            _text(entry.get("category")) or "",
            _text(entry.get("subcategory")) or "",
        )
        if not triple[0] or not triple[1]:
            raise ValueError(f"{file} entry needs sector and category")
        for label in entry.get("labels") or []:
            key = _key(label)
            if not key or key in ambiguous or key in exact:
                raise ValueError(f"{file} label {label!r} is blank, ambiguous, or repeated")
            exact[key] = triple
    return ClassTable(exact, ambiguous)


def classify(labels: list[str], table: ClassTable) -> tuple[str | None, str | None, str | None]:
    cleaned = [_text(label) for label in labels]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        return None, None, None
    hits: list[tuple[str, str, str]] = []
    for label in cleaned:
        key = _key(label)
        if key in table.ambiguous or key not in table.exact:
            return None, None, None
        hits.append(table.exact[key])
    if len(set(hits)) != 1:
        return None, None, None
    sector, category, subcategory = hits[0]
    return sector, category, subcategory or None


def excel_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = str(value).strip()
    if text == "":
        return None
    if text[0] in FORMULA_PREFIX:
        return "'" + text
    return text


def services_cell(items: list[Any] | None) -> str | None:
    names: list[str] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("needs_confirmation") is True:
            continue
        name = _text(item.get("name"))
        if name and name not in names:
            names.append(name)
    return "; ".join(names) or None


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["Company ID"])
        current = chosen.get(key)
        if current is None or (row.get("Collection Date") or "") > (current.get("Collection Date") or ""):
            chosen[key] = row
    return [chosen[key] for key in sorted(chosen)]


def build_row(
    *,
    company_id: str,
    name: str,
    city: str | None,
    niche: str | None,
    source: str | None,
    whatsapp: str | None,
    instagram: str | None,
    snapshots: Mapping[str, Any],
    services: list[Any] | None,
    score: int | None,
    discrepancies: Any,
    categories: ClassTable,
    coverage: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {column: None for column in COLUMNS}
    primary = snapshots.get("2gis")
    row["Company ID"] = company_id
    row["Company Name"] = _text(name)
    labels = _category_labels(snapshots, niche)
    sector, category, subcategory = classify(labels, categories)
    row["Business Sector"] = sector
    row["Business Category"] = category
    row["Business Subcategory"] = subcategory
    row["Services / Activities"] = services_cell(services)
    row["Branch Name"] = _field(primary, "name")
    row["City"] = _text(city)
    row["District"] = _first_field(snapshots, "district")
    row["Address"] = _field(primary, "address")
    contacts, conflicts = resolve_contacts(
        _contact_snapshots(snapshots, coverage),
        company_whatsapp=whatsapp,
        company_instagram=instagram,
    )
    row["Phone"] = contacts.get("phone")
    row["WhatsApp"] = contacts.get("whatsapp")
    row["Instagram"] = contacts.get("instagram")
    row["Website"] = contacts.get("website")
    row["Email"] = contacts.get("email")
    for platform, columns in _PLATFORM_COLUMNS.items():
        state = None if coverage is None else coverage.get(platform)
        snap = None if state in ("not_checked", "blocked", "ambiguous", "other_address", "not_found") else snapshots.get(platform)
        if state == "not_found":
            stored = snapshots.get(platform)
            row[columns[2]] = _explicit_zero(_field(stored, "reviews_count"))
            row[columns[3]] = _explicit_zero(_field(stored, "photos_count"))
            continue
        if snap is None:
            continue
        row[columns[0]] = _attr(snap, "url")
        row[columns[1]] = _number(_field(snap, "rating"), whole=False)
        row[columns[2]] = _number(_field(snap, "reviews_count"), whole=True)
        row[columns[3]] = _number(_field(snap, "photos_count"), whole=True)
        row[columns[4]] = _text(_field(snap, "description"))
        row[columns[5]] = _text(_field(snap, "hours"))
    row["Overall Completeness Score"] = score if isinstance(score, int) else None
    row["Missing Information"] = _missing(snapshots if coverage is None else _contact_snapshots(snapshots, coverage))
    row["Identified Problems"] = _join(_problems(snapshots, discrepancies, include_status=coverage is None), *_coverage_notes(coverage), *conflicts)
    row["Source"] = _text(source)
    row["Collection Date"] = _when(primary)
    shown = snapshots if coverage is None else _contact_snapshots(snapshots, coverage)
    row["Screenshot Path"] = _join(*[_attr(shown.get(name), "screenshot_path") for name in ("2gis", "yandex", "google")])
    if coverage is None or coverage.get("2gis") == "found":
        row["Verification Status"] = _attr(primary, "status")
    else:
        row["Verification Status"] = coverage.get("2gis")
    return row


def write_xlsx(rows: list[dict[str, Any]], path: str | Path) -> int:
    ready = dedupe_rows(rows)
    book = Workbook()
    sheet = book.active
    sheet.title = "mailing"
    sheet.append(list(COLUMNS))
    for row in ready:
        sheet.append([excel_value(row.get(column)) for column in COLUMNS])
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".partial")
    book.save(temporary)
    os.replace(temporary, dest)
    return len(ready)


def load_rows(session: Session, categories: ClassTable | None = None) -> list[dict[str, Any]]:
    table = categories or load_categories()
    rows: list[dict[str, Any]] = []
    companies = session.scalars(select(Company).order_by(Company.id_2gis, Company.id)).all()
    for company in companies:
        if company.dnc:
            continue
        snapshots = _latest(session, company.id)
        primary = snapshots.get("2gis")
        if primary is None or primary.status not in READY or _blocked(primary):
            continue
        audit = session.scalar(select(Audit).where(Audit.company_id == company.id).order_by(Audit.id.desc()))
        profile = session.scalar(
            select(CompanyProfile).where(CompanyProfile.company_id == company.id).order_by(CompanyProfile.version.desc())
        )
        data = profile.data if profile is not None else {}
        rows.append(
            build_row(
                company_id=company.id_2gis or f"company:{company.id}",
                name=company.name,
                city=company.city,
                niche=company.niche,
                source=company.source,
                whatsapp=company.whatsapp,
                instagram=company.instagram,
                snapshots=snapshots,
                services=list(data.get("services") or []),
                score=None if audit is None else audit.score_total,
                discrepancies=[] if audit is None else audit.discrepancies,
                categories=table,
            )
        )
    return dedupe_rows(rows)


def _latest(session: Session, company_id: int) -> dict[str, CardSnapshot]:
    found: dict[str, CardSnapshot] = {}
    rows = session.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company_id)).all()
    for row in rows:
        if row.status not in READY:
            continue
        current = found.get(row.platform)
        if current is None or (row.collected_at, row.id) > (current.collected_at, current.id):
            found[row.platform] = row
    twogis = [row for row in rows if row.platform == "2gis"]
    if twogis:
        newest = max(twogis, key=lambda row: (row.collected_at, row.id))
        if _blocked(newest):
            found.pop("2gis", None)
    return found


def _blocked(snapshot: CardSnapshot) -> bool:
    reason = (snapshot.fail_reason or "").strip().lower()
    if reason in BLOCKED:
        return True
    access = (snapshot.field_sources or {}).get("_access")
    if not isinstance(access, dict):
        return False
    return access.get("access") in BLOCKED or access.get("outcome") in BLOCKED


def _category_labels(snapshots: Mapping[str, Any], niche: str | None) -> list[str]:
    labels = []
    for platform in ("2gis", "yandex", "google"):
        label = _text(_field(snapshots.get(platform), "category"))
        if label:
            labels.append(label)
    if labels:
        return labels
    niche_text = _text(niche)
    return [niche_text] if niche_text else []


def _missing(snapshots: Mapping[str, Any]) -> str | None:
    found = []
    for platform, key, label in _MISSING_KEYS:
        fields = _mapping(_attr(snapshots.get(platform), "fields"))
        if key in fields and fields[key] is None:
            found.append(label)
    return "; ".join(found) or None


def load_batch_rows(session: Session, batch_id: int, categories: ClassTable | None = None) -> list[dict[str, Any]]:
    from zhol.db.models import CollectionBatchItem

    table = categories or load_categories()
    items = session.scalars(
        select(CollectionBatchItem)
        .where(CollectionBatchItem.batch_id == batch_id, CollectionBatchItem.company_id.is_not(None))
        .order_by(CollectionBatchItem.id)
    ).all()
    rows: list[dict[str, Any]] = []
    for item in items:
        if item.status not in ("completed", "failed", "blocked"):
            continue
        company = session.get(Company, item.company_id)
        if company is None or company.dnc:
            continue
        snapshots = _latest(session, company.id)
        audit = session.scalar(select(Audit).where(Audit.company_id == company.id).order_by(Audit.id.desc()))
        profile = session.scalar(
            select(CompanyProfile).where(CompanyProfile.company_id == company.id).order_by(CompanyProfile.version.desc())
        )
        data = profile.data if profile is not None else {}
        coverage = (item.detail or {}).get("platforms") or {}
        rows.append(
            build_row(
                company_id=company.id_2gis or f"company:{company.id}",
                name=company.name,
                city=company.city,
                niche=company.niche,
                source=company.source,
                whatsapp=company.whatsapp,
                instagram=company.instagram,
                snapshots=snapshots,
                services=list(data.get("services") or []),
                score=None if audit is None else audit.score_total,
                discrepancies=[] if audit is None else audit.discrepancies,
                categories=table,
                coverage=coverage,
            )
        )
    return dedupe_rows(rows)


def _contact_snapshots(snapshots: Mapping[str, Any], coverage: Mapping[str, str] | None) -> dict[str, Any]:
    if coverage is None:
        return dict(snapshots)
    kept = {}
    for name, snapshot in snapshots.items():
        if coverage.get(name) != "found":
            continue
        if _hold(snapshot) in ("ambiguous_match", "match_needs_review", "missing_external_id", "platform_id_conflict"):
            continue
        if _attr(snapshot, "match_status") not in (None, "found"):
            continue
        kept[name] = snapshot
    return kept


def _hold(snapshot: Any) -> str | None:
    sources = _mapping(_attr(snapshot, "field_sources"))
    marker = sources.get("_collection")
    if not isinstance(marker, Mapping):
        return None
    hold = marker.get("hold")
    return str(hold) if hold else None


def _explicit_zero(value: Any) -> int | None:
    if value in (0, "0", 0.0):
        return 0
    return None


def _coverage_notes(coverage: Mapping[str, str] | None) -> list[str]:
    if not coverage:
        return []
    return [f"{name}: {coverage[name]}" for name in ("2gis", "yandex", "google") if coverage.get(name) not in (None, "found")]


def _problems(snapshots: Mapping[str, Any], discrepancies: Any, *, include_status: bool = True) -> str | None:
    found: list[str] = []
    items = discrepancies if isinstance(discrepancies, list) else []
    for item in items:
        if isinstance(item, dict):
            text = _text(item.get("legacy_text") or item.get("text"))
        else:
            text = _text(item)
        if text and text not in found:
            found.append(text)
    if include_status:
        for platform in ("2gis", "yandex", "google"):
            status = _attr(snapshots.get(platform), "match_status")
            if status and status != "found":
                found.append(f"{platform}: {status}")
    return "; ".join(found) or None


def _phones(value: Any) -> str | None:
    if isinstance(value, list):
        return _join(*[_text(item) for item in value])
    return _text(value)


def _join(*values: Any) -> str | None:
    found: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in found:
            found.append(text)
    return "; ".join(found) or None


def _first_field(snapshots: Mapping[str, Any], key: str) -> str | None:
    for platform in ("2gis", "yandex", "google"):
        text = _text(_field(snapshots.get(platform), key))
        if text:
            return text
    return None


def _field(snapshot: Any, key: str) -> Any:
    fields = _mapping(_attr(snapshot, "fields"))
    if key not in fields:
        return None
    return fields[key]


def _number(value: Any, *, whole: bool) -> int | float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    if whole:
        return int(value)
    return float(value)


def _when(snapshot: Any) -> str | None:
    moment = _attr(snapshot, "collected_at")
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ALMATY)
    return moment.astimezone(ALMATY).date().isoformat()


def _attr(snapshot: Any, name: str) -> Any:
    if snapshot is None:
        return None
    if isinstance(snapshot, Mapping):
        return snapshot.get(name)
    return getattr(snapshot, name, None)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (int, float, bool)):
        return None
    text = " ".join(str(value).split())
    return text or None


def _key(value: Any) -> str:
    text = _text(value)
    return "" if text is None else text.casefold()
