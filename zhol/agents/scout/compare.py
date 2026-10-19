from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.agents.scout.fields import field_keys
from zhol.db.models import CardSnapshot, Company

MANUAL = "manual_import"
SCOUT = "scout"


@dataclass(frozen=True)
class CompareRow:
    id_2gis: str
    platform: str
    field: str
    old: Any
    new: Any
    match: bool
    screenshot: str | None


def _latest(session: Session, company_id: int, platform: str, collected_by: str) -> CardSnapshot | None:
    return session.scalar(
        select(CardSnapshot)
        .where(
            CardSnapshot.company_id == company_id,
            CardSnapshot.platform == platform,
            CardSnapshot.collected_by == collected_by,
        )
        .order_by(CardSnapshot.id.desc())
        .limit(1)
    )


def _value(snapshot: CardSnapshot | None, field: str) -> Any:
    if snapshot is None:
        return None
    if field == "match_status":
        return snapshot.match_status
    return (snapshot.fields or {}).get(field)


def compare_companies(session: Session) -> list[CompareRow]:
    rows: list[CompareRow] = []
    companies = session.scalars(select(Company).order_by(Company.id)).all()
    for company in companies:
        manual = {platform: _latest(session, company.id, platform, MANUAL) for platform in ("2gis", "yandex", "google")}
        if all(item is None for item in manual.values()):
            continue
        for platform in ("2gis", "yandex", "google"):
            fresh = _latest(session, company.id, platform, SCOUT)
            for field in ("match_status", *field_keys(platform)):
                old = _value(manual[platform], field)
                new = _value(fresh, field)
                rows.append(
                    CompareRow(
                        id_2gis=company.id_2gis,
                        platform=platform,
                        field=field,
                        old=old,
                        new=new,
                        match=old == new,
                        screenshot=None if fresh is None else fresh.screenshot_path,
                    )
                )
    return rows
