"""Code checks for generated text. The model does not decide that a claim is allowed."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from zhol.content.brief import KAZAKH_LETTERS
from zhol.guards import forbidden_phrases, no_long_dash

_NUMBER = re.compile(r"\d{3,}")
_CLOCK = re.compile(r"\d{1,2}:\d{2}")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def check_text(
    *,
    text: str,
    language: str,
    claims: Sequence[Mapping[str, Any]],
    approved: Sequence[Mapping[str, Any]],
    unverified: Sequence[Mapping[str, Any]],
    max_chars: int,
    allow_clock: bool = False,
    require_claims: bool = True,
) -> str | None:
    if language not in {"ru", "kk"}:
        return "language is not supported"
    if len(text) > max_chars:
        return "text is longer than the platform limit"
    if not no_long_dash(text):
        return "text uses a long dash"
    found = forbidden_phrases(text)
    if found:
        return "forbidden phrase"
    if language == "kk" and not any(char in KAZAKH_LETTERS for char in text):
        return "language does not match"
    if language == "ru" and _CYRILLIC.search(text) is None:
        return "language does not match"
    if not allow_clock and _CLOCK.search(text) and not _has_path(approved, "hours"):
        return "unsupported hours"
    corpus = _numbers(approved)
    for number in _NUMBER.findall(text):
        if number not in corpus:
            return "unsupported number"
    leaked = _leaked(text, unverified)
    if leaked:
        return "uses unverified information"
    if not require_claims:
        return None
    return _claims(claims, approved)


def check_reply(
    *,
    text: str,
    language: str,
    rating: int | None,
    review_text: str,
    approved: Sequence[Mapping[str, Any]],
    max_chars: int,
) -> str | None:
    extra = ({"path": "review", "source_ref": "review", "value": review_text},)
    reason = check_text(
        text=text,
        language=language,
        claims=(),
        approved=tuple(approved) + extra,
        unverified=(),
        max_chars=max_chars,
        allow_clock=True,
        require_claims=False,
    )
    if reason:
        return reason
    if review_text and any(char in KAZAKH_LETTERS for char in review_text) and language != "kk":
        return "language does not match"
    if rating is not None and not text.strip():
        return "unsupported claim"
    return None


def _claims(claims: Sequence[Mapping[str, Any]], approved: Sequence[Mapping[str, Any]]) -> str | None:
    by_path = {str(item["path"]): item for item in approved}
    if not claims:
        return "unsupported claim"
    for claim in claims:
        path = str(claim.get("fact") or "")
        source = claim.get("source_ref")
        quote = str(claim.get("text") or "")
        fact = by_path.get(path)
        if fact is None or fact.get("source_ref") != source or not quote.strip():
            return "unsupported claim"
        if quote.casefold() not in _blob(fact.get("value")).casefold():
            return "unsupported claim"
    return None


def _has_path(approved: Sequence[Mapping[str, Any]], path: str) -> bool:
    return any(item.get("path") == path for item in approved)


def _numbers(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    found: set[str] = set()
    for item in rows:
        found.update(_NUMBER.findall(json.dumps(item.get("value"), ensure_ascii=False)))
    return found


def _leaked(text: str, unverified: Sequence[Mapping[str, Any]]) -> bool:
    folded = text.casefold()
    for item in unverified:
        value = item.get("value")
        if not isinstance(value, str):
            continue
        sample = value.strip()
        if len(sample) >= 4 and sample.casefold() in folded:
            return True
    return False


def _blob(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
