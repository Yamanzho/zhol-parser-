from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SEVERITY_RANK = {"minor": 0, "major": 1, "critical": 2}
BLOCKING = ("critical", "major")

_FILE_REF = re.compile(r"file:([\w-]+)")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAIN_ITEM = re.compile(r"^\s*(\d+)\.\s*\[главный\]", re.M)


def max_severity(values: Iterable[str]) -> str:
    return max(values, key=SEVERITY_RANK.__getitem__)


def main_items(checklist: str) -> set[int]:
    return {int(n) for n in _MAIN_ITEM.findall(checklist)}


def evidence_ok(evidence: str | None, materials: Mapping[str, Any]) -> bool:
    if not evidence or not evidence.strip():
        return False
    files = {str(f.get("id")) for f in materials.get("files") or [] if isinstance(f, Mapping)}
    if any(ref in files for ref in _FILE_REF.findall(evidence)):
        return True
    return any(token in materials for token in _IDENTIFIER.findall(evidence))


def tally(
    groups: list[dict[str, Any]],
    issues: Mapping[str, dict[str, Any]],
    answers: list[dict[str, Any]],
    participants: Mapping[str, dict[str, Any]],
    author_family: str | None,
    materials: Mapping[str, Any],
) -> list[dict[str, Any]]:
    def foreign(label: str) -> bool:
        return participants[label]["family"] != author_family

    result = []
    for group in groups:
        ids = set(group["ids"])
        supporters = {issues[i]["label"] for i in ids}
        severities = [issues[i]["severity"] for i in ids]
        disagree: set[str] = set()
        for answer in answers:
            if answer["ref"] not in ids:
                continue
            if answer["stance"] in ("agree", "refine"):
                supporters.add(answer["label"])
                if answer["stance"] == "refine" and answer.get("severity"):
                    severities.append(answer["severity"])
            elif answer["stance"] == "disagree" and evidence_ok(answer.get("evidence"), materials):
                disagree.add(answer["label"])
        weight = sum(participants[label]["weight"] for label in supporters)
        dispute = sum(participants[label]["weight"] for label in disagree if foreign(label))
        has_foreign = any(foreign(label) for label in supporters)
        severity = max_severity(severities)
        if dispute >= 1:
            status = "disputed"
        elif weight >= 1.5 and has_foreign:
            status = "accepted"
        elif severity == "critical" and has_foreign:
            status = "accepted"
        else:
            status = "disputed"
        result.append(
            {
                **group,
                "ids": sorted(ids),
                "severity": severity,
                "weight": weight,
                "dispute": dispute,
                "supporters": sorted(supporters),
                "disagree": sorted(disagree),
                "status": status,
            }
        )
    return number_accepted(result)


def number_accepted(groups: list[dict[str, Any]], start: int = 1) -> list[dict[str, Any]]:
    accepted = sorted(
        (g for g in groups if g["status"] == "accepted" and "n" not in g),
        key=lambda g: (-SEVERITY_RANK[g["severity"]], g["ids"][0] if g.get("ids") else ""),
    )
    for n, group in enumerate(accepted, start):
        group["n"] = n
    return groups


def quorum(responded: Iterable[dict[str, Any]], level: str, author_family: str | None, expected: int) -> bool:
    responded = list(responded)
    has_foreign = any(p["family"] != author_family for p in responded)
    if level == "single":
        return len(responded) == expected and all(p["family"] != author_family for p in responded)
    return len(responded) >= 2 and has_foreign


def verdict(*, has_quorum: bool, groups: list[dict[str, Any]], unverified_main: bool = False) -> tuple[str, str | None]:
    if not has_quorum:
        return "human", "no_quorum"
    if unverified_main:
        return "human", "unverified_main"
    if any(g["status"] == "disputed" and g["severity"] == "critical" for g in groups):
        return "human", "disputed_critical"
    if any(g["status"] == "accepted" and g["severity"] in BLOCKING for g in groups):
        return "return", "accepted_issues"
    return "pass", None


def recheck(
    open_groups: list[dict[str, Any]],
    answers: Mapping[str, Mapping[int, bool]],
    new_issues: list[dict[str, Any]],
    participants: Mapping[str, dict[str, Any]],
    author_family: str | None,
    level: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    def foreign(label: str) -> bool:
        return participants[label]["family"] != author_family

    not_fixed = []
    for group in open_groups:
        against = [label for label, by_n in answers.items() if by_n.get(group["n"]) is False]
        if level == "single":
            failed = bool(against)
        else:
            weight = sum(participants[label]["weight"] for label in against if foreign(label))
            failed = weight >= 1 or (group["severity"] == "critical" and any(foreign(label) for label in against))
        if failed:
            not_fixed.append(group)
    new_accepted = [i for i in new_issues if level == "single" or foreign(i["label"])]
    new_disputed = [i for i in new_issues if i not in new_accepted]
    return not_fixed, new_accepted, new_disputed
