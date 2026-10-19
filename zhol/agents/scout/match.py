from __future__ import annotations

from dataclasses import dataclass

from zhol.agents.scout.normalize import addresses_compatible, addresses_conflict, names_compatible, phone_parts, phones_overlap
from zhol.browser.page import PageDocument

VERDICT_STATUS = {"same": "found", "other_address": "other_address", "not_match": "not_found"}


@dataclass(frozen=True)
class Identity:
    name: str | None
    address: str | None
    phones: list[str]


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    external_id: str | None
    url: str | None
    name: str | None
    address: str | None
    phones: list[str]


@dataclass(frozen=True)
class Decision:
    verdict: str
    candidate: Candidate | None
    phone_match: bool


def candidates_from_document(document: PageDocument) -> list[Candidate]:
    found = []
    for raw in document.candidates:
        text = (raw.get("fields") or {}).get("phones") or ""
        phones = []
        for part in text.replace(";", ",").split(","):
            number, _shown = phone_parts(part)
            if number and number not in phones:
                phones.append(number)
        found.append(
            Candidate(
                candidate_id=str(raw.get("candidate_id") or ""),
                external_id=raw.get("external_id"),
                url=raw.get("url"),
                name=(raw.get("fields") or {}).get("name"),
                address=(raw.get("fields") or {}).get("address"),
                phones=phones,
            )
        )
    return found


def verdict_for(identity: Identity, candidate: Candidate) -> tuple[str, bool]:
    phone = phones_overlap(identity.phones, candidate.phones)
    name = names_compatible(identity.name, candidate.name)
    address = addresses_compatible(identity.address, candidate.address)
    conflict = addresses_conflict(identity.address, candidate.address)
    # A shared switchboard number plus a different street and house is a branch, not proof of the same card.
    if phone and conflict:
        return "phone_branch", phone
    if phone or (name and address):
        return "same", phone
    if name and conflict:
        return "other_address", False
    return "not_match", False


def choose(identity: Identity, candidates: list[Candidate]) -> Decision:
    marked = [(candidate, *verdict_for(identity, candidate)) for candidate in candidates]
    same = [item for item in marked if item[1] == "same"]
    if len(same) > 1:
        return Decision("ambiguous", None, False)
    branches = [item for item in marked if item[1] == "phone_branch"]
    if branches and not same:
        return Decision("ambiguous", None, False)
    if len(same) == 1:
        candidate, _verdict, phone = same[0]
        return Decision("same", candidate, phone)
    other = [item for item in marked if item[1] == "other_address"]
    if len(other) == 1:
        return Decision("other_address", other[0][0], False)
    if len(other) > 1:
        return Decision("ambiguous", None, False)
    return Decision("not_match", None, False)


def match_status(verdict: str) -> str:
    """Column value. Ambiguous stays `not_found` because the snapshot check has no other status.

    The company page reads hold `ambiguous_match` and does not call that a confirmed miss.
    """
    if verdict == "ambiguous":
        return "not_found"
    return VERDICT_STATUS[verdict]
