from __future__ import annotations

from zhol.browser.page import PageDocument, UnexpectedPage, parse_html

PLATFORM = "2gis"


def read_card(html: str, markers: dict[str, str]) -> PageDocument:
    document = parse_html(html, markers)
    if document.page != "card" or document.platform != PLATFORM:
        raise UnexpectedPage(PLATFORM)
    return document
