from __future__ import annotations

import os
import re
from datetime import datetime

LIVE_ENV = "ZHOL_SCOUT_LIVE"
_FIRM = re.compile(r"^https://2gis\.kz/[a-z0-9_-]+/firm/\d+/?$")


class LiveCollectionDisabled(Exception):
    pass


def live_enabled() -> bool:
    return os.environ.get(LIVE_ENV) == "1"


def fetch_public(url: str) -> str:
    """Fetch one public 2GIS firm page. Other platforms stay off."""
    if not live_enabled():
        raise LiveCollectionDisabled(f"live collection is disabled for {url}")
    if not _FIRM.match(url):
        raise LiveCollectionDisabled("live collection in this batch is a public 2GIS firm page")
    from zhol.browser.twogis import capture

    return capture.fetch_html(url)


def wait_seconds(last: datetime | None, *, rate_limit_seconds: float, at: datetime) -> float:
    if last is None:
        return 0.0
    return max(0.0, rate_limit_seconds - (at - last).total_seconds())
