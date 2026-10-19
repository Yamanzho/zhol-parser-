"""Classify a saved cabinet page. A check or a login wall is not listing data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from zhol.browser.config import load_platforms
from zhol.browser.page import PageDocument, parse_html

PAUSE_ORDER = ("sms", "captcha", "login")
CABINET_PAGES = frozenset({"cabinet", "card"})


def classify_pause(text: str, pause: Mapping[str, Sequence[str]]) -> str | None:
    folded = text.lower()
    for kind in PAUSE_ORDER:
        phrases = pause.get(kind) or ()
        if any(phrase.lower() in folded for phrase in phrases if phrase):
            return kind
    return None


def parse_cabinet(html: str) -> PageDocument:
    spec = load_platforms()
    return parse_html(html, spec["markers"])


def page_is_cabinet(document: PageDocument, platform: str) -> bool:
    return document.page in CABINET_PAGES and document.platform == platform
