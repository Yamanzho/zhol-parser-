"""Yandex organization links from a saved page. Live search stays closed."""

from __future__ import annotations

from zhol.browser.maps.visible import place_id
from zhol.guards import saved_discovery_only

PLATFORM = "yandex"


def refuse_live(source_mode: str) -> None:
    saved_discovery_only(source_mode, PLATFORM)


def firm_url(href: str | None, needle: str) -> str | None:
    if not href or needle not in href:
        return None
    return href.split("?")[0]


def external_id(url: str | None) -> str | None:
    return place_id(PLATFORM, url)
