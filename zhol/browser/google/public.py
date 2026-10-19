from __future__ import annotations

from zhol.browser.page import PageDocument, UnexpectedPage, parse_html

PLATFORM = "google"
MAX_CANDIDATES = 5


def read_search(html: str, markers: dict[str, str]) -> PageDocument:
    document = parse_html(html, markers)
    if document.page != "search" or document.platform != PLATFORM:
        raise UnexpectedPage(PLATFORM)
    document.candidates = document.candidates[:MAX_CANDIDATES]
    return document


def read_card(html: str, markers: dict[str, str]) -> PageDocument:
    document = parse_html(html, markers)
    if document.page != "card" or document.platform != PLATFORM:
        raise UnexpectedPage(PLATFORM)
    return document
