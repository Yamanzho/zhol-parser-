"""Fact-quoting text drafts. This provider does not call a model API."""

from __future__ import annotations

import json
import re

from zhol.content.brief import KAZAKH_LETTERS
from zhol.llm.providers import ProviderReply

_SKIP = frozenset({"phones", "messengers", "hours"})
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class OfflineFactProvider:
    """Build a draft from approved facts already present in the prompt."""

    def complete(self, *, prompt, model, system, user, temperature, images, json_schema) -> ProviderReply:
        del model, system, temperature, images, json_schema
        if prompt == "executor-review-reply":
            body = _reply(user)
        else:
            body = _draft(user)
        return ProviderReply(json.dumps(body, ensure_ascii=False), 0, 0)


def _draft(user: str) -> dict:
    language = "kk" if "Язык: kk" in user else "ru"
    claims = []
    quotes: list[str] = []
    for fact in _facts(user):
        path = str(fact.get("path") or "")
        source = fact.get("source_ref")
        quote = _quote(fact.get("value"))
        if path in _SKIP or not path or not isinstance(source, str) or not quote:
            continue
        claims.append({"fact": path, "source_ref": source, "text": quote})
        quotes.append(quote)
        if len(claims) >= 6:
            break
    text = ". ".join(quotes) if quotes else "Компания"
    if language == "ru" and _CYRILLIC.search(text) is None:
        text = f"Компания. {text}"
    if language == "kk" and not any(char in KAZAKH_LETTERS for char in text):
        text = f"{text}. қызмет"
    return {"language": language, "text": text[:750], "claims": claims or [{"fact": "name", "source_ref": "missing", "text": "Компания"}]}


def _reply(user: str) -> dict:
    kazakh = any(char in KAZAKH_LETTERS for char in user)
    if kazakh:
        return {"reply": "Пікір үшін рақмет.", "language": "kk", "needs_client_approval": False, "flags": []}
    return {"reply": "Спасибо за отзыв.", "language": "ru", "needs_client_approval": False, "flags": []}


def _facts(user: str) -> list[dict]:
    marker = user.find('{"language"')
    if marker < 0:
        return []
    try:
        data, _end = json.JSONDecoder().raw_decode(user[marker:])
    except json.JSONDecodeError:
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        return []
    return [item for item in facts if isinstance(item, dict)]


def _quote(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict) and isinstance(first.get("name"), str):
            text = first["name"].strip()
        elif isinstance(first, str):
            text = first.strip()
        else:
            return ""
    else:
        return ""
    if not text or len(text) > 80 or "\u2013" in text or "\u2014" in text:
        return ""
    return text
