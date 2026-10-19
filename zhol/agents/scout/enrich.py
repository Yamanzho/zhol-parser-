"""Common Excel contacts. Platform snapshots are not rewritten."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from zhol.agents.scout.normalize import normalize_phone

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULES = ROOT / "config" / "contacts.yaml"
FIELDS = ("phone", "whatsapp", "instagram", "website", "email")
_USE = frozenset({None, "found"})


def load_contact_rules(path: str | Path | None = None) -> dict[str, list[dict[str, str]]]:
    file = Path(path) if path is not None else DEFAULT_RULES
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    priority = data.get("priority") if isinstance(data, dict) else None
    if not isinstance(priority, dict):
        raise ValueError(f"{file} needs a priority mapping")
    rules: dict[str, list[dict[str, str]]] = {}
    for name in FIELDS:
        sources = priority.get(name)
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"{file} priority.{name} needs a source list")
        cleaned = []
        for source in sources:
            if not isinstance(source, dict) or not source.get("key"):
                raise ValueError(f"{file} priority.{name} has a source without a key")
            if source.get("source") == "company":
                cleaned.append({"source": "company", "key": str(source["key"])})
            elif source.get("platform"):
                cleaned.append({"platform": str(source["platform"]), "key": str(source["key"])})
            else:
                raise ValueError(f"{file} priority.{name} source needs platform or source: company")
        rules[name] = cleaned
    return rules


def _attr(snapshot: Any, name: str) -> Any:
    if snapshot is None:
        return None
    if isinstance(snapshot, Mapping):
        return snapshot.get(name)
    return getattr(snapshot, name, None)


def _usable(snapshot: Any) -> bool:
    return _attr(snapshot, "match_status") in _USE


def _raw(snapshot: Any, key: str) -> Any:
    if not _usable(snapshot):
        return None
    if key == "phones":
        sources = _attr(snapshot, "field_sources")
        if isinstance(sources, Mapping):
            phone = sources.get("phones")
            if isinstance(phone, Mapping) and phone.get("display"):
                return list(phone["display"])
    fields = _attr(snapshot, "fields")
    if not isinstance(fields, Mapping) or key not in fields:
        return None
    return fields.get(key)


def _display(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, list):
        parts = []
        for item in value:
            text = " ".join(str(item).split())
            if text and text not in parts:
                parts.append(text)
        return "; ".join(parts) or None
    text = " ".join(str(value).split())
    return text or None


def _instagram(value: str) -> str:
    text = value.strip().split("?")[0].rstrip("/")
    if "instagram.com/" in text.lower():
        text = text.split("/")[-1]
    return text[1:].casefold() if text.startswith("@") else text.casefold()


def _website(value: str) -> str:
    text = value.strip().rstrip("/").casefold()
    if text.startswith("http://"):
        text = text[len("http://") :]
    elif text.startswith("https://"):
        text = text[len("https://") :]
    if text.startswith("www."):
        text = text[4:]
    return text


def _phone_key(value: str) -> str:
    numbers = []
    for part in re.split(r"[,;]", value):
        number = normalize_phone(part)
        if number and number not in numbers:
            numbers.append(number)
    return "|".join(sorted(numbers)) if numbers else " ".join(value.casefold().split())


def _same(field: str, left: str, right: str) -> bool:
    if field in ("phone", "whatsapp"):
        return _phone_key(left) == _phone_key(right)
    if field == "instagram":
        return _instagram(left) == _instagram(right)
    if field == "website":
        return _website(left) == _website(right)
    return left.casefold() == right.casefold()


def _label(source: dict[str, str]) -> str:
    if source.get("source") == "company":
        return "company"
    return str(source.get("platform"))


def resolve_contacts(
    snapshots: Mapping[str, Any],
    *,
    company_whatsapp: str | None = None,
    company_instagram: str | None = None,
    rules: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[dict[str, str | None], list[str]]:
    """Fill common cells by config order. Conflicts stay visible and do not overwrite."""
    table = rules or load_contact_rules()
    company = {"whatsapp": company_whatsapp, "instagram": company_instagram}
    chosen: dict[str, str | None] = {}
    conflicts: list[str] = []
    for field, sources in table.items():
        winner: str | None = None
        winner_label = ""
        seen: list[str] = []
        for source in sources:
            if source.get("source") == "company":
                raw = company.get(source["key"])
            else:
                raw = _raw(snapshots.get(str(source.get("platform"))), source["key"])
            text = _display(raw)
            if text is None:
                continue
            label = _label(source)
            if winner is None:
                winner = text
                winner_label = label
                seen.append(f"{label} {text}")
                continue
            if _same(field, winner, text):
                continue
            seen.append(f"{label} {text}")
        chosen[field] = winner
        if len(seen) > 1:
            conflicts.append(f"contact {field}: " + "; ".join(seen) + f"; kept {winner_label}")
    return chosen, conflicts
