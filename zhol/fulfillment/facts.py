"""Approved company facts. An unapproved value is not a listing value."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PUBLIC_SERVICE_KEYS = ("name", "category", "price", "price_from", "price_to", "duration_min")
CRITICAL_FACTS = ("name", "address", "phones")


@dataclass(frozen=True)
class FactValue:
    status: str
    value: Any
    source_ref: str | None


def approved_fact(profile: Mapping[str, Any], path: str) -> FactValue:
    root, _, child = path.partition(".")
    row = profile.get(root)
    if path == "services" or root == "services":
        return _services(row)
    if child:
        return _child(row, child)
    return _wrapped(row)


def critical_missing(profile: Mapping[str, Any], company_name: str) -> tuple[str, ...]:
    missing: list[str] = []
    for key in CRITICAL_FACTS:
        if key == "name":
            name = approved_fact(profile, "name")
            if name.status == "unapproved" or (name.status == "absent" and not company_name.strip()):
                missing.append("name")
            continue
        fact = approved_fact(profile, key)
        if fact.status != "approved" or _blank(fact.value):
            missing.append(key)
    return tuple(missing)


def public_services(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        rows.append({key: item[key] for key in PUBLIC_SERVICE_KEYS if key in item and item[key] is not None})
    rows.sort(key=lambda item: str(item.get("name") or ""))
    return rows


def _services(row: Any) -> FactValue:
    if not isinstance(row, list) or not row:
        return FactValue("absent", None, None)
    if any(not isinstance(item, Mapping) for item in row):
        return FactValue("unapproved", None, None)
    if any(bool(item.get("needs_confirmation")) for item in row):
        source = row[0].get("source_ref")
        return FactValue("unapproved", None, source if isinstance(source, str) else None)
    published = public_services(row)
    if not published:
        return FactValue("absent", None, None)
    source = row[0].get("source_ref")
    return FactValue("approved", published, source if isinstance(source, str) else "field:services")


def _child(row: Any, child: str) -> FactValue:
    if not isinstance(row, Mapping):
        return FactValue("absent", None, None)
    source = row.get("source_ref") if isinstance(row.get("source_ref"), str) else None
    if row.get("needs_confirmation"):
        return FactValue("unapproved", None, source)
    value = row.get("value")
    if not isinstance(value, Mapping) or child not in value or _blank(value.get(child)):
        return FactValue("absent", None, source)
    return FactValue("approved", value.get(child), source)


def _wrapped(row: Any) -> FactValue:
    if not isinstance(row, Mapping) or "value" not in row:
        return FactValue("absent", None, None)
    source = row.get("source_ref") if isinstance(row.get("source_ref"), str) else None
    if row.get("needs_confirmation"):
        return FactValue("unapproved", None, source)
    if _blank(row.get("value")):
        return FactValue("absent", None, source)
    return FactValue("approved", row.get("value"), source)


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False
