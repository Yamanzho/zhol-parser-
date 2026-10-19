"""2GIS firm links from a saved page. A catalog request is refused before any browser opens."""

from __future__ import annotations

from zhol.browser.twogis.capture import FIRM_URL
from zhol.guards import saved_discovery_only

PLATFORM = "2gis"


def refuse_live(source_mode: str) -> None:
    saved_discovery_only(source_mode, PLATFORM)


def firm_url(href: str | None, needle: str) -> str | None:
    if not href or needle not in href:
        return None
    candidate = href.split("?")[0]
    if not FIRM_URL.match(candidate):
        return None
    return candidate


def firm_id(url: str | None) -> str | None:
    if not url or not FIRM_URL.match(url):
        return None
    return url.rstrip("/").split("/")[-1]
