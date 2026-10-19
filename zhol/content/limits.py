"""Platform text caps. A draft longer than the cap is rejected before it can be approved."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from zhol.browser.config import load_platforms

PLATFORMS = ("2gis", "yandex", "google")


@lru_cache(maxsize=1)
def text_limits() -> dict[str, Any]:
    raw = load_platforms().get("text_limits")
    if not isinstance(raw, dict):
        raise ValueError("platforms.yaml text_limits is missing")
    company = raw.get("company_description")
    service = raw.get("service_description")
    reply = raw.get("review_reply")
    platforms = raw.get("platform_description")
    if not all(isinstance(item, int) and item > 0 for item in (company, service, reply)):
        raise ValueError("text_limits need positive integer caps")
    if not isinstance(platforms, dict) or any(not isinstance(platforms.get(name), int) or platforms[name] < 1 for name in PLATFORMS):
        raise ValueError("text_limits.platform_description needs a cap for 2gis, yandex, and google")
    return {
        "company_description": int(company),
        "service_description": int(service),
        "review_reply": int(reply),
        "platform_description": {name: int(platforms[name]) for name in PLATFORMS},
    }


def char_limit(kind: str, platform: str | None = None) -> int:
    limits = text_limits()
    if kind == "platform_description":
        if platform not in limits["platform_description"]:
            raise ValueError(f"no description cap for platform {platform!r}")
        return int(limits["platform_description"][platform])
    if kind in {"company_description", "service_description", "review_reply"}:
        return int(limits[kind])
    raise ValueError(f"no text cap for {kind}")
