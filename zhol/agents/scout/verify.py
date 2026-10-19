from __future__ import annotations

from datetime import date
from typing import Any

from zhol.agents.scout.fields import BOOL_KEYS, DATE_KEYS, FLOAT_KEYS, INT_KEYS, LIST_KEYS, enum_values, field_keys
from zhol.agents.scout.normalize import phones_overlap
from zhol.browser.evidence import screenshot_state


def _problems_for_value(platform: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if key in BOOL_KEYS:
        if not isinstance(value, bool):
            return [f"{key}: expected boolean"]
        return []
    if key in INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return [f"{key}: expected a non-negative integer"]
        return []
    if key in FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 5:
            return [f"{key}: expected a rating from 0 to 5"]
        return []
    if key in DATE_KEYS:
        if not isinstance(value, str):
            return [f"{key}: expected a date"]
        try:
            date.fromisoformat(value)
        except ValueError:
            return [f"{key}: expected a date"]
        return []
    if key in LIST_KEYS:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return [f"{key}: expected a list of strings"]
        return []
    allowed = enum_values(platform, key)
    if not isinstance(value, str):
        return [f"{key}: expected a string"]
    if allowed is not None and value not in allowed:
        return [f"{key}: unexpected value"]
    return []


def check_snapshot(snapshot: Any) -> list[str]:
    problems: list[str] = []
    expected = set(field_keys(snapshot.platform))
    fields = snapshot.fields or {}
    sources = snapshot.field_sources or {}
    extra = set(fields) - expected
    missing = expected - set(fields)
    if extra:
        problems.append(f"unexpected fields: {sorted(extra)}")
    if missing:
        problems.append(f"missing fields: {sorted(missing)}")
    # Verification needs a readable screenshot. This is decided before the verified status is written.
    shot_state = screenshot_state(getattr(snapshot, "screenshot_path", None))
    if shot_state != "present":
        problems.append("screenshot is unreadable" if shot_state == "unreadable" else "screenshot is missing")
    for key in expected & set(fields):
        problems.extend(_problems_for_value(snapshot.platform, key, fields[key]))
        if fields[key] is None:
            continue
        source = sources.get(key)
        if not isinstance(source, dict):
            problems.append(f"{key}: source is missing")
            continue
        for part in ("url", "seen_at", "screenshot"):
            if not source.get(part):
                problems.append(f"{key}: source has no {part}")
        if source.get("read_by") not in ("code", "model"):
            problems.append(f"{key}: read_by must be code or model")
        shot = source.get("screenshot")
        if shot:
            field_state = screenshot_state(shot if isinstance(shot, str) else None)
            if field_state != "present":
                label = "unreadable" if field_state == "unreadable" else "missing"
                problems.append(f"{key}: screenshot file is {label}")
    return problems


def needs_match_review(platform: str, match_status: str, twogis_fields: dict | None, fields: dict) -> bool:
    if platform == "2gis" or match_status != "found":
        return False
    return not phones_overlap((twogis_fields or {}).get("phones"), fields.get("phones"))
