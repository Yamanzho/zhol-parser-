"""Shared gate for map collectors. A loopback fixture is not a live catalog.

The scope tokens match the two written-retention meanings used for 2GIS.
Each map source reads only its own environment variable.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from zhol.agents.scout.browser_2gis import LOCAL_HOSTS, BrowserCollectionRefused
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.live_2gis import _strip_city
from zhol.agents.scout.localities import require_locality, supports

__all__ = [
    "BrowserCollectionRefused",
    "GRANT_COMMERCIAL",
    "GRANT_LOCAL",
    "GRANT_SCOPES",
    "allow_url",
    "closed_mode",
    "collection_state",
    "grant_scope",
    "map_plans",
    "refuse_live_url",
]

GRANT_LOCAL = "written-browser-retention-local"
GRANT_COMMERCIAL = "written-browser-retention-commercial"
GRANT_SCOPES = {GRANT_LOCAL: "local", GRANT_COMMERCIAL: "commercial"}
_LIVE_MARKERS = ("yandex", "google", "gstatic", "2gis")


def collection_state(*, available: bool, scope: str | None) -> str:
    """Separate a missing grant from a configured source that cannot run yet."""
    if available:
        return "available"
    if scope:
        return "restricted"
    return "unauthorized"


def grant_scope(env_name: str) -> str | None:
    """Return local or commercial. Any other text, including another source's variable, is no grant."""
    return GRANT_SCOPES.get(os.environ.get(env_name, "").strip())


def allow_url(url: str, blocker: str, *, live_origin: str | None) -> None:
    """Allow a loopback fixture, or one configured live host when that source is authorized.

    A missing grant refuses every public host. An authorized source accepts only its own host.
    """
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and host in LOCAL_HOSTS:
        if any(marker in host for marker in _LIVE_MARKERS):
            raise BrowserCollectionRefused(blocker)
        return
    if not live_origin:
        raise BrowserCollectionRefused(blocker)
    allowed = (urlsplit(live_origin).hostname or "").lower()
    if not allowed or host != allowed:
        raise BrowserCollectionRefused(blocker)


def refuse_live_url(url: str, blocker: str) -> None:
    """Refuse every public host. Callers that hold a grant use allow_url instead."""
    allow_url(url, blocker, live_origin=None)


def map_plans(
    *,
    source_mode: str,
    platform: str,
    city: str,
    categories: list[str],
    queries: list[str],
    platforms: list[str],
    limit: int,
) -> list[dict]:
    """Validate one authorized search. This does not open Chrome."""
    if platforms != [platform]:
        raise BatchError("Для этого сборщика доступен только его собственный источник.")
    if limit < 1 or limit > 500:
        raise BatchError("Лимит должен быть от 1 до 500.")
    try:
        found = require_locality(city)
    except ValueError as exc:
        raise BatchError(str(exc)) from exc
    if not supports(found.name, platform):
        raise BatchError(
            f"Город «{found.name}» не поддержан для этого источника. Сбор не переключается на Алматы."
        )
    city_name = found.name
    labels: list[str] = []
    seen: set[str] = set()
    for raw in [*categories, *queries]:
        text = " ".join(str(raw).split())
        if not text:
            continue
        if len(text) > 120:
            raise BatchError("Слишком длинный поисковый запрос.")
        cleaned = _strip_city(text, city_name)
        if not cleaned:
            raise BatchError("Запрос не должен состоять только из названия города.")
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(cleaned)
    if not labels:
        raise BatchError("Укажите категорию или поисковый запрос.")
    return [
        {
            "source_mode": source_mode,
            "city": city_name,
            "category": label,
            "platforms": [platform],
            "limit": limit,
            "catalog": source_mode,
        }
        for label in labels
    ]


def closed_mode(*, source_mode: str, label: str, blocker: str) -> dict:
    return {
        "id": source_mode,
        "kind": "browser",
        "label": label,
        "available": False,
        "cities": ["Алматы"],
        "note": blocker,
        "source_kind": "live_refused",
        "engine": False,
        "origin": None,
        "authorization_scope": None,
        "live_available": False,
    }
