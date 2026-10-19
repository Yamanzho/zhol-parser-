"""Open a Yandex Business cabinet in a normal headed browser.

The window has no saved profile and no stored password. A person signs in.
This module does not click Save, Publish, or Submit.
"""

from __future__ import annotations

import os
from typing import Any

from zhol.browser.headed import launch_blank, open_headed
from zhol.browser.yandex.page import PlaywrightCabinet

LIVE_ENV = "ZHOL_YANDEX_CABINET"

__all__ = ["LIVE_ENV", "launch_blank", "open_interactive"]


def open_interactive(url: str, *, playwright: Any = None) -> PlaywrightCabinet:
    return open_headed(
        url,
        enabled=os.environ.get(LIVE_ENV) == "1",
        closed_message="live Yandex Business cabinet is closed",
        playwright=playwright,
        cabinet=PlaywrightCabinet,
    )
