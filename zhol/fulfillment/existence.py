"""Decide whether a new listing may be prepared.

A stored id or url, a found card, or an unverified audit blocks creation.
Absence is usable only when a fresh verified search and a person both say the card is missing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from zhol.timeutil import parse_now

FRESH = timedelta(days=14)
RELIABLE_PURPOSES = frozenset({"order_fresh", "report", "subscription_start"})
AUDIT_COLLECTORS = frozenset({"bek_audit", "etalon", "manual"})
STATUSES = frozenset({"existing_listing", "missing_confirmed", "uncertain", "ownership_required"})


@dataclass(frozen=True)
class SnapshotEvidence:
    match_status: str
    url: str | None
    collected_at: datetime
    status: str
    purpose: str
    collected_by: str


@dataclass(frozen=True)
class AbsenceConfirmation:
    ref: str
    confirmed_at: datetime


@dataclass(frozen=True)
class ExistenceVerdict:
    status: str
    reason: str


def classify_existence(
    *,
    external_id: str | None,
    public_url: str | None,
    snapshots: Sequence[SnapshotEvidence],
    confirmation: AbsenceConfirmation | None,
    now: datetime,
) -> ExistenceVerdict:
    """Classify stored records. The open create form is checked separately."""
    if _text(external_id) or _text(public_url):
        return ExistenceVerdict("existing_listing", "known platform id or listing url")
    if any(item.match_status == "found" or _text(item.url) for item in snapshots):
        return ExistenceVerdict("existing_listing", "a stored listing was found")
    if any(item.match_status == "other_address" for item in snapshots):
        return ExistenceVerdict("uncertain", "the stored address does not match")
    latest = snapshots[0] if snapshots else None
    if latest is not None and latest.match_status == "not_found" and _audit(latest):
        return ExistenceVerdict("uncertain", "audit not_found is unverified")
    if not _reliable_absence(latest, now):
        if latest is not None and latest.match_status == "not_found":
            return ExistenceVerdict("uncertain", "listing absence is not fresh")
        return ExistenceVerdict("uncertain", "listing absence is not confirmed by a fresh search")
    if confirmation is None:
        return ExistenceVerdict("uncertain", "missing human confirmation")
    age = now - confirmation.confirmed_at
    if age < timedelta(0) or age > FRESH or not confirmation.ref.strip():
        return ExistenceVerdict("uncertain", "human confirmation is stale")
    return ExistenceVerdict("missing_confirmed", "absence confirmed")


def page_signal(
    *,
    external_id: str | None,
    listing_state: str,
    absence_phrases: Sequence[str],
    found_phrases: Sequence[str],
    ownership_phrases: Sequence[str],
) -> ExistenceVerdict | None:
    """Read the open form. None means the page agrees the listing is absent."""
    if _text(external_id):
        return ExistenceVerdict("existing_listing", "the open page already has a listing id")
    if _contains(listing_state, ownership_phrases):
        return ExistenceVerdict("ownership_required", "the page asks for owner verification")
    if _contains(listing_state, absence_phrases):
        return None
    if _contains(listing_state, found_phrases):
        return ExistenceVerdict("existing_listing", "the page shows an existing listing")
    return ExistenceVerdict("uncertain", "the page does not confirm that the listing is absent")


def confirmation_from_checklist(checklist: Mapping[str, object], platform: str) -> AbsenceConfirmation | None:
    platforms = checklist.get("platforms")
    if not isinstance(platforms, Mapping):
        return None
    row = platforms.get(platform)
    if not isinstance(row, Mapping):
        return None
    raw = row.get("absence_confirmation")
    if not isinstance(raw, Mapping):
        return None
    ref = raw.get("ref")
    stamp = raw.get("at")
    if not isinstance(ref, str) or not ref.strip() or not isinstance(stamp, str):
        return None
    try:
        confirmed_at = parse_now(stamp)
    except ValueError:
        return None
    return AbsenceConfirmation(ref.strip(), confirmed_at)


def _reliable_absence(snapshot: SnapshotEvidence | None, now: datetime) -> bool:
    if snapshot is None or snapshot.match_status != "not_found" or _text(snapshot.url):
        return False
    if snapshot.status != "verified" or snapshot.purpose not in RELIABLE_PURPOSES or _audit(snapshot):
        return False
    age = now - snapshot.collected_at
    return timedelta(0) <= age <= FRESH


def _audit(snapshot: SnapshotEvidence) -> bool:
    return snapshot.purpose == "prospect" or snapshot.collected_by in AUDIT_COLLECTORS


def _contains(text: str, phrases: Sequence[str]) -> bool:
    folded = " ".join(text.split()).casefold()
    return any(phrase.casefold() in folded for phrase in phrases if phrase.strip())


def _text(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())
