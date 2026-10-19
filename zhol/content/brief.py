"""Structured content briefs from approved client facts.

Unapproved values stay on the brief as gaps. They are not given to the model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zhol.fulfillment.facts import approved_fact

KAZAKH_LETTERS = frozenset("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
FACT_PATHS = (
    "address",
    "category",
    "description",
    "entrance_hint",
    "hours",
    "phones",
    "messengers",
    "website",
    "socials",
    "payment_methods",
    "services",
    "features",
)
GAP_PATHS = frozenset({"address", "hours", "phones", "services", "category", "description", "website"})


@dataclass(frozen=True)
class OrderFacts:
    language: str
    approved: tuple[dict[str, Any], ...]
    missing: tuple[str, ...]
    unverified: tuple[dict[str, Any], ...]
    platforms: tuple[str, ...]
    client_description: bool
    reviews_authorized: bool
    company_name: str

    def approved_map(self) -> dict[str, dict[str, Any]]:
        return {str(item["path"]): item for item in self.approved}

    def model_payload(self) -> dict[str, Any]:
        return {"language": self.language, "facts": [dict(item) for item in self.approved]}

    def gap_payload(self) -> dict[str, Any]:
        return {
            "missing": list(self.missing),
            "unverified": [str(item["path"]) for item in self.unverified],
        }


def choose_language(checklist: Mapping[str, Any], profile: Mapping[str, Any], messages: Sequence[str]) -> str:
    requested = checklist.get("content_language")
    if requested in {"ru", "kk"}:
        return str(requested)
    blob = json.dumps({"profile": profile, "messages": list(messages)}, ensure_ascii=False)
    if any(char in KAZAKH_LETTERS for char in blob):
        return "kk"
    return "ru"


def collect_facts(
    *,
    company_name: str,
    city: str | None,
    profile: Mapping[str, Any],
    messages: Sequence[str],
    checklist: Mapping[str, Any],
) -> OrderFacts:
    approved: list[dict[str, Any]] = [
        {"path": "name", "source_ref": "field:company.name", "value": company_name.strip()},
    ]
    if isinstance(city, str) and city.strip():
        approved.append({"path": "city", "source_ref": "field:company.city", "value": city.strip()})
    missing: list[str] = []
    unverified: list[dict[str, Any]] = []
    for path in FACT_PATHS:
        fact = approved_fact(profile, path)
        if fact.status == "approved":
            approved.append(
                {
                    "path": path,
                    "source_ref": fact.source_ref or f"field:{path}",
                    "value": fact.value,
                }
            )
            continue
        if fact.status == "unapproved":
            unverified.append(
                {
                    "path": path,
                    "source_ref": fact.source_ref,
                    "value": _stored_value(profile, path),
                }
            )
            continue
        if path in GAP_PATHS:
            missing.append(path)
    approved.sort(key=lambda item: str(item["path"]))
    unverified.sort(key=lambda item: str(item["path"]))
    description = approved_fact(profile, "description")
    platforms = _platforms(checklist)
    return OrderFacts(
        language=choose_language(checklist, profile, messages),
        approved=tuple(approved),
        missing=tuple(sorted(set(missing))),
        unverified=tuple(unverified),
        platforms=platforms,
        client_description=description.status == "approved" and not _blank(description.value),
        reviews_authorized=checklist.get("review_replies") == "authorized",
        company_name=company_name.strip(),
    )


def _platforms(checklist: Mapping[str, Any]) -> tuple[str, ...]:
    raw = checklist.get("platforms")
    if not isinstance(raw, Mapping):
        return ()
    return tuple(sorted(str(name) for name in raw))


def _stored_value(profile: Mapping[str, Any], path: str) -> Any:
    row = profile.get(path)
    if isinstance(row, Mapping) and "value" in row:
        return row.get("value")
    return row


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False
