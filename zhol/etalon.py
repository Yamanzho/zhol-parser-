from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.db.models import Audit, CardSnapshot, Company, CompanyPlatformId
from zhol.scoring import derive, score, score_groups, to_score_row
from zhol.scoring.adapter import COLUMNS, ENUMS, STATUS_COLUMNS, UNSEEN_KEYS
from zhol.timeutil import ALMATY

SOURCE = "etalon_2026_09_30"
COLLECTED_BY = "manual_import"
DEFAULT_CSV = Path("source") / "etalon-50.csv"
FORBIDDEN_FILES = {"base-contacts.csv"}
VERIFIED_LAYERS = {"верх (реклама)", "середина", "окраины"}
EXPECTED_COLUMNS = ("балл_отзывы", "балл_цены", "балл_фото", "балл_контакты", "балл_итого")
REQUIRED_COLUMNS = {
    "id_2gis", "название", "тип", "слой", "url_2gis", "url_яндекс", "дата_просмотра", "расхождения",
    *(column for _, _, column in COLUMNS), *(column for column, _ in STATUS_COLUMNS.values()), *EXPECTED_COLUMNS,
}
INT_KEYS = {"ratings_count", "reviews_count", "photos_count", "reviews_shown", "replies_count", "branches", "news_count"}
FLOAT_KEYS = {"rating"}
BOOL_KEYS = {"is_ad", "has_phone", "has_whatsapp", "has_instagram", "online_booking", "owner_maintains", "owner_claimed"}
YANDEX_ORG = re.compile(r"/org/[^/]+/(\d+)")


class EtalonError(ValueError):
    pass


@dataclass
class ImportStats:
    companies: int = 0
    snapshots: int = 0
    audits: int = 0
    skipped: list[str] = field(default_factory=list)


def read_rows(path: str | Path = DEFAULT_CSV) -> list[dict[str, str]]:
    path = Path(path)
    if path.name in FORBIDDEN_FILES:
        raise EtalonError(f"{path.name} is not a reference dataset and must not be imported")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise EtalonError(f"{path.name}: missing columns {sorted(missing)}")
        rows = list(reader)
    bad = [r["id_2gis"] for r in rows if r["слой"] not in VERIFIED_LAYERS]
    if bad:
        raise EtalonError(f"rows outside the manually collected layers: {bad}")
    return rows


def _value(platform: str, key: str, raw: str, column: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return None
    enum = ENUMS.get((platform, key))
    if enum is not None:
        if raw not in enum:
            raise EtalonError(f"{column}: unknown value {raw!r}")
        return enum[raw]
    if key in BOOL_KEYS:
        if raw not in ("0", "1"):
            raise EtalonError(f"{column}: expected 0 or 1, got {raw!r}")
        return raw == "1"
    if key in INT_KEYS:
        return int(raw)
    if key in FLOAT_KEYS:
        return float(raw.replace(",", "."))
    if key == "last_review_date":
        return date.fromisoformat(raw).isoformat()
    if key == "phones":
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def collected_at(row: dict[str, str]) -> datetime:
    return datetime.combine(date.fromisoformat(row["дата_просмотра"]), time(0, 0), tzinfo=ALMATY)


def snapshot_values(row: dict[str, str]) -> dict[str, dict[str, Any]]:
    seen = collected_at(row)
    urls = {"2gis": row["url_2gis"].strip() or None, "yandex": row["url_яндекс"].strip() or None, "google": None}
    result: dict[str, dict[str, Any]] = {}
    for platform in ("2gis", "yandex", "google"):
        fields: dict[str, Any] = {}
        sources: dict[str, Any] = {}
        for p, key, column in COLUMNS:
            if p != platform:
                continue
            fields[key] = _value(platform, key, row[column], column)
            sources[key] = _source(urls[platform], seen, column)
        for key in UNSEEN_KEYS[platform]:
            fields[key] = None
            sources[key] = _source(urls[platform], seen, None)
        if platform == "2gis":
            status = "found"
        else:
            column, mapping = STATUS_COLUMNS[platform]
            if row[column] not in mapping:
                raise EtalonError(f"{column}: unknown value {row[column]!r}")
            status = mapping[row[column]]
            sources["match_status"] = _source(urls[platform], seen, column)
        result[platform] = {"match_status": status, "url": urls[platform], "fields": fields, "field_sources": sources}
    return result


def _source(url: str | None, seen: datetime, column: str | None) -> dict[str, Any]:
    return {"url": url, "seen_at": seen.isoformat(), "screenshot": None, "read_by": "manual", "source": SOURCE, "csv_column": column}


def legacy_discrepancies(row: dict[str, str]) -> list[dict[str, str]]:
    text = row["расхождения"].strip()
    return [{"legacy_text": text}] if text else []


def expected_score(row: dict[str, str]) -> dict[str, int]:
    return {column: int(row[column]) for column in EXPECTED_COLUMNS}


def _platform_ids(session: Session, company_id: int, row: dict[str, str]) -> None:
    wanted = {"2gis": (row["id_2gis"].strip(), row["url_2gis"].strip() or None)}
    yandex_url = row["url_яндекс"].strip()
    if yandex_url:
        match = YANDEX_ORG.search(yandex_url)
        wanted["yandex"] = (match.group(1) if match else None, yandex_url)
    for platform, (external_id, url) in wanted.items():
        if session.get(CompanyPlatformId, (company_id, platform)) is None:
            session.add(CompanyPlatformId(company_id=company_id, platform=platform, external_id=external_id, public_url=url))


def import_etalon(session: Session, path: str | Path = DEFAULT_CSV) -> ImportStats:
    stats = ImportStats()
    for row in read_rows(path):
        company = session.scalar(select(Company).where(Company.id_2gis == row["id_2gis"]))
        if company is None:
            company = Company(
                id_2gis=row["id_2gis"],
                name=row["название"],
                niche=row["тип"].strip() or None,
                whatsapp=row["номер_whatsapp"].strip() or None,
                instagram=row["аккаунт_instagram"].strip() or None,
                source=SOURCE,
            )
            session.add(company)
            session.flush()
            stats.companies += 1
        elif company.source != SOURCE:
            stats.skipped.append(f"{row['id_2gis']}: company exists with source {company.source}")
        _platform_ids(session, company.id, row)

        seen = collected_at(row)
        snapshots: dict[str, CardSnapshot] = {}
        for platform, values in snapshot_values(row).items():
            snapshot = session.scalar(
                select(CardSnapshot).where(
                    CardSnapshot.company_id == company.id,
                    CardSnapshot.platform == platform,
                    CardSnapshot.collected_by == COLLECTED_BY,
                    CardSnapshot.collected_at == seen,
                )
            )
            if snapshot is None:
                snapshot = CardSnapshot(
                    company_id=company.id,
                    platform=platform,
                    purpose="prospect",
                    collected_at=seen,
                    collected_by=COLLECTED_BY,
                    status="verified",
                    **values,
                )
                session.add(snapshot)
                stats.snapshots += 1
            snapshots[platform] = snapshot
        session.flush()

        ids = sorted(s.id for s in snapshots.values())
        if session.scalar(select(Audit.id).where(Audit.company_id == company.id, Audit.snapshot_ids == ids)) is None:
            derived = derive(snapshots["2gis"])
            discrepancies = legacy_discrepancies(row)
            result = score(to_score_row(snapshots, derived, discrepancies))
            session.add(
                Audit(
                    company_id=company.id,
                    snapshot_ids=ids,
                    score_total=result["итого"],
                    score_groups=score_groups(result),
                    derived=derived,
                    discrepancies=discrepancies,
                    gaps=[],
                )
            )
            stats.audits += 1
    session.flush()
    return stats


@dataclass
class CheckResult:
    id_2gis: str
    name: str
    expected: dict[str, int]
    actual: dict[str, int]

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def score_check(session: Session, path: str | Path = DEFAULT_CSV) -> list[CheckResult]:
    results = []
    for row in read_rows(path):
        company = session.scalar(select(Company).where(Company.id_2gis == row["id_2gis"]))
        if company is None:
            raise EtalonError(f"{row['id_2gis']} is not imported")
        seen = collected_at(row)
        snapshots = {
            s.platform: s
            for s in session.scalars(
                select(CardSnapshot).where(
                    CardSnapshot.company_id == company.id,
                    CardSnapshot.collected_by == COLLECTED_BY,
                    CardSnapshot.collected_at == seen,
                )
            )
        }
        ids = sorted(s.id for s in snapshots.values())
        audit = session.scalar(select(Audit).where(Audit.company_id == company.id, Audit.snapshot_ids == ids))
        if audit is None:
            raise EtalonError(f"{row['id_2gis']} has no imported audit")
        result = score(to_score_row(snapshots, audit.derived, audit.discrepancies))
        actual = {column: result[key] for column, key in zip(EXPECTED_COLUMNS, ("отзывы", "цены", "фото", "контакты", "итого"))}
        results.append(CheckResult(row["id_2gis"], row["название"], expected_score(row), actual))
    return results
