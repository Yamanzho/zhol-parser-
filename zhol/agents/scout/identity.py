"""Company identity across 2GIS, Yandex, and Google.

A shared normalized phone is required before two companies are the same.
Name and address alone stay unresolved. A shared phone with a different
address stays unresolved. Similar names are not scored.
"""

from __future__ import annotations

from dataclasses import dataclass

from zhol.agents.scout.match import Candidate, Identity, verdict_for
from zhol.agents.scout.normalize import addresses_compatible, addresses_conflict, names_compatible
from zhol.browser.discovery.types import Branch, PlatformEvidence

PLATFORMS = ("2gis", "yandex", "google")


@dataclass(frozen=True)
class Ref:
    platform: str
    external_id: str


@dataclass(frozen=True)
class Profile:
    name: str | None
    address: str | None
    phones: tuple[str, ...]
    refs: tuple[Ref, ...]


@dataclass(frozen=True)
class Relation:
    kind: str
    phone_confirmed: bool


def relate(left: Profile, right: Profile) -> Relation:
    """same, ambiguous, other_address, or distinct.

    `phone_confirmed` is true only when the same verdict shares a normalized phone.
    """
    shared = _ref_set(left) & _ref_set(right)
    if shared:
        return Relation("same", True)
    verdict, phone = verdict_for(
        Identity(left.name, left.address, list(left.phones)),
        Candidate("candidate", None, None, right.name, right.address, list(right.phones)),
    )
    compatible = addresses_compatible(left.address, right.address)
    conflict = addresses_conflict(left.address, right.address)
    names = names_compatible(left.name, right.name)
    if _conflicting_ids(left, right):
        # Different cards on one platform. The same place stays unresolved.
        # A different house, or an address that is not stored yet, is another company.
        if names and compatible:
            return Relation("ambiguous", False)
        if verdict == "phone_branch":
            return Relation("ambiguous", False)
        if names and conflict:
            return Relation("other_address", False)
        return Relation("distinct", False)
    if verdict == "phone_branch":
        return Relation("ambiguous", False)
    if verdict == "same":
        if not compatible or not phone:
            # Name plus address, or a phone that is not shared, does not merge two companies.
            return Relation("ambiguous", False)
        if left.name and right.name and not names:
            return Relation("ambiguous", False)
        return Relation("same", True)
    if names and not compatible and not conflict:
        return Relation("ambiguous", False)
    if verdict == "other_address" or (names and conflict):
        return Relation("other_address", False)
    return Relation("distinct", False)


def origin_platform(branch: Branch) -> str | None:
    if branch.firm_id:
        return "2gis"
    for name in PLATFORMS:
        evidence = branch.platforms.get(name)
        if evidence is not None and evidence.external_id:
            return name
    return None


def item_key(branch: Branch) -> str:
    """Batch item key. 2GIS keeps the firm id. Other origins use trace:platform:id."""
    if branch.firm_id:
        return branch.firm_id
    origin = origin_platform(branch)
    evidence = None if origin is None else branch.platforms.get(origin)
    if evidence is None or not evidence.external_id:
        return ""
    return f"trace:{origin}:{evidence.external_id}"


def profile_from_branch(branch: Branch) -> Profile:
    return Profile(branch.name, branch.address, branch.phones, _refs(branch, None))


def profile_from_hit(hit: Branch, evidence: PlatformEvidence) -> Profile:
    return Profile(hit.name, hit.address, hit.phones, _refs(hit, evidence))


def public_company_id(id_2gis: str | None, company_id: int) -> str:
    """Stable export id. Existing 2GIS ids stay as they are."""
    if id_2gis:
        return id_2gis
    return f"company:{company_id}"


def _refs(branch: Branch, evidence: PlatformEvidence | None) -> tuple[Ref, ...]:
    found: list[Ref] = []
    if branch.firm_id:
        found.append(Ref("2gis", branch.firm_id))
    for name, item in branch.platforms.items():
        _add_ref(found, name, item.external_id)
    if evidence is not None:
        _add_ref(found, evidence.platform, evidence.external_id)
    return tuple(found)


def _add_ref(found: list[Ref], platform: str, external_id: str | None) -> None:
    if not external_id:
        return
    if any(item.platform == platform and item.external_id == external_id for item in found):
        return
    found.append(Ref(platform, external_id))


def _ref_set(profile: Profile) -> set[tuple[str, str]]:
    return {(item.platform, item.external_id) for item in profile.refs}


def _conflicting_ids(left: Profile, right: Profile) -> bool:
    left_ids = {item.platform: item.external_id for item in left.refs}
    right_ids = {item.platform: item.external_id for item in right.refs}
    return any(platform in right_ids and left_ids[platform] != right_ids[platform] for platform in left_ids)
