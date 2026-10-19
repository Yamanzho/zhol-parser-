from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from zhol.agents.scout.errors import InvalidField
from zhol.agents.scout.fields import BOOL_KEYS, DATE_KEYS, FLOAT_KEYS, HREF_KEYS, INT_KEYS, LIST_KEYS, enum_values, field_keys
from zhol.agents.scout.normalize import phone_parts
from zhol.browser.page import PageDocument
from zhol.guards import unseen_must_be_null

ModelReader = Callable[[str, str, str, list[str], str], dict[str, Any]]


def _blank(raw: str | None) -> bool:
    return raw is None or raw.strip() == ""


def _parse(platform: str, key: str, raw: str | None) -> Any:
    if _blank(raw):
        unseen_must_be_null(False, None)
        return None
    assert raw is not None
    text = raw.strip()
    if key in BOOL_KEYS:
        if text not in ("0", "1"):
            raise InvalidField(key, text)
        return text == "1"
    if key in INT_KEYS:
        if not re.fullmatch(r"\d+", text):
            raise InvalidField(key, text)
        return int(text)
    if key in FLOAT_KEYS:
        try:
            value = float(text.replace(",", "."))
        except ValueError as exc:
            raise InvalidField(key, text) from exc
        if isinstance(value, bool) or not 0 <= value <= 5:
            raise InvalidField(key, text)
        return value
    if key in DATE_KEYS:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise InvalidField(key, text) from exc
    if key in LIST_KEYS:
        numbers, _shown = canonical_phones(text)
        return numbers
    allowed = enum_values(platform, key)
    if allowed is not None and text not in allowed:
        raise InvalidField(key, text)
    return text


def canonical_phones(raw: str | None) -> tuple[list[str] | None, list[str]]:
    """Store one digit form. The second list is the text that was on the page."""
    if _blank(raw):
        return None, []
    assert raw is not None
    numbers: list[str] = []
    shown: list[str] = []
    for part in re.split(r"[,;]", raw):
        number, display = phone_parts(part)
        if number and number not in numbers:
            numbers.append(number)
            if display:
                shown.append(display)
    return (numbers or None), shown


def _source(url: str, seen_at: str, screenshot: str, raw_path: str, *, read_by: str | None, reason: str | None) -> dict[str, Any]:
    source: dict[str, Any] = {"url": url, "seen_at": seen_at, "screenshot": screenshot, "raw_path": raw_path}
    if read_by is not None:
        source["read_by"] = read_by
    if reason is not None:
        source["reason"] = reason
    return source


def extract_card(
    platform: str,
    document: PageDocument,
    *,
    url: str,
    seen_at: str,
    screenshot: str,
    raw_path: str,
    code_fields: list[str],
    model_fields: list[str],
    model_reader: ModelReader | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    code = set(code_fields)
    model = set(model_fields)
    fields: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    page_notes = document.notes or {}
    page_text = "\n".join(f"{key}: {value}" for key, value in document.fields.items())
    read_model: dict[str, Any] = {}
    if model_reader is not None and model_fields:
        read_model = model_reader(platform, url, seen_at, list(model_fields), page_text) or {}
    for key in field_keys(platform):
        if key in code:
            present = key in document.fields or (key in HREF_KEYS and key in document.hrefs)
            raw = document.hrefs.get(key) if key in HREF_KEYS and document.hrefs.get(key) else document.fields.get(key)
            if not present:
                unseen_must_be_null(False, None)
                fields[key] = None
                sources[key] = _source(
                    url,
                    seen_at,
                    screenshot,
                    raw_path,
                    read_by="code",
                    reason=page_notes.get(key) or "not_shown",
                )
            elif key == "phones":
                fields[key], display = canonical_phones(None if raw is None else str(raw))
                reason = None if fields[key] is not None else (page_notes.get(key) or "empty")
                sources[key] = _source(url, seen_at, screenshot, raw_path, read_by="code", reason=reason)
                if display:
                    sources[key]["display"] = display
            else:
                fields[key] = _parse(platform, key, raw)
                reason = None if fields[key] is not None else "empty"
                sources[key] = _source(url, seen_at, screenshot, raw_path, read_by="code", reason=reason)
        elif key in model and model_reader is not None:
            if key not in read_model:
                fields[key] = None
                reason = "not_shown"
            else:
                fields[key] = read_model[key]
                if fields[key] is not None:
                    fields[key] = _parse(platform, key, str(fields[key]))
                reason = None if fields[key] is not None else "not_shown"
            sources[key] = _source(url, seen_at, screenshot, raw_path, read_by="model", reason=reason)
        else:
            unseen_must_be_null(False, None)
            fields[key] = None
            sources[key] = _source(url, seen_at, screenshot, raw_path, read_by=None, reason="model_not_called")
    return fields, sources


def empty_observation(
    platform: str,
    *,
    url: str,
    seen_at: str,
    screenshot: str,
    raw_path: str,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = {key: None for key in field_keys(platform)}
    sources = {
        key: _source(url, seen_at, screenshot, raw_path, read_by="code", reason=reason) for key in fields
    }
    return fields, sources
