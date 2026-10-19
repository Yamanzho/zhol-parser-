from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.db.models import Company, CompanyPlatformId

SOURCE = "base_550_contacts"
COLUMNS = ("id_2gis", "название", "тип", "номер_whatsapp", "аккаунт_instagram", "etalon")


class ContactsError(ValueError):
    pass


@dataclass
class ContactStats:
    companies: int = 0
    skipped: int = 0


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def import_contacts(session: Session, path: str | Path) -> ContactStats:
    stats = ContactStats()
    file = Path(path)
    with file.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ContactsError(f"{file.name}: missing columns {sorted(missing)}")
        rows = list(reader)
    for row in rows:
        id_2gis = (row.get("id_2gis") or "").strip()
        name = (row.get("название") or "").strip()
        if not id_2gis or not name or (row.get("etalon") or "").strip() == "1":
            stats.skipped += 1
            continue
        if session.scalar(select(Company.id).where(Company.id_2gis == id_2gis)) is not None:
            stats.skipped += 1
            continue
        company = Company(
            id_2gis=id_2gis,
            name=name,
            niche=_blank(row.get("тип")),
            whatsapp=_blank(row.get("номер_whatsapp")),
            instagram=_blank(row.get("аккаунт_instagram")),
            source=SOURCE,
        )
        session.add(company)
        session.flush()
        session.add(
            CompanyPlatformId(
                company_id=company.id,
                platform="2gis",
                external_id=id_2gis,
                public_url=_blank(row.get("url_2gis")),
            )
        )
        stats.companies += 1
    session.flush()
    return stats
