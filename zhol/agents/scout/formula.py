from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.agents.scout.persist import open_task
from zhol.db.models import CardSnapshot
from zhol.transitions import transition

PAIRS = {
    "yandex": ("rating", "ratings_count", "reviews_count", "photos_count"),
    "google": ("rating", "reviews_count"),
}
MIN_COMPANIES = 3


def _delta(left: object, right: object, *, rating: bool) -> float | None:
    if left is None or right is None or isinstance(left, bool) or isinstance(right, bool):
        return None
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    if rating:
        return round(round(float(right), 1) - round(float(left), 1), 1)
    return float(right) - float(left)


def _latest(rows: list[CardSnapshot]) -> dict[tuple[int, str], CardSnapshot]:
    chosen: dict[tuple[int, str], CardSnapshot] = {}
    for row in rows:
        if row.company_id is None or row.status == "failed":
            continue
        key = (row.company_id, row.platform)
        current = chosen.get(key)
        if current is None or row.id > current.id:
            chosen[key] = row
    return chosen


def find_formulas(snapshots: list[CardSnapshot]) -> list[str]:
    latest = _latest(snapshots)
    companies = {company_id for company_id, _platform in latest}
    found: list[str] = []
    for platform, keys in PAIRS.items():
        for key in keys:
            deltas: list[float] = []
            complete = True
            paired = 0
            for company_id in companies:
                origin = latest.get((company_id, "2gis"))
                other = latest.get((company_id, platform))
                if origin is None or other is None:
                    continue
                if origin.match_status != "found" or other.match_status != "found":
                    continue
                paired += 1
                delta = _delta((origin.fields or {}).get(key), (other.fields or {}).get(key), rating=key == "rating")
                if delta is None:
                    complete = False
                    break
                deltas.append(delta)
            if not complete or paired < MIN_COMPANIES or not deltas:
                continue
            if all(item == deltas[0] for item in deltas):
                found.append(f"{platform}.{key} - 2gis.{key} = {deltas[0]}")
    return found


def reject_batch(session: Session, snapshots: list[CardSnapshot], reasons: list[str]) -> int:
    rejected = 0
    for snapshot in snapshots:
        if snapshot.status not in ("collected", "verified"):
            continue
        snapshot.fail_reason = "formula:" + "; ".join(reasons)
        session.flush()
        transition(snapshot, "rejected", session)
        rejected += 1
    if rejected:
        open_task(
            session,
            "formula_detected",
            "batch",
            None,
            {"dependencies": reasons, "snapshots": rejected},
        )
    return rejected


def check_since(session: Session, since: datetime) -> list[str]:
    rows = session.scalars(select(CardSnapshot).where(CardSnapshot.collected_at >= since)).all()
    reasons = find_formulas(list(rows))
    if reasons:
        reject_batch(session, list(rows), reasons)
    return reasons
