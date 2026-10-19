"""Open a 2GIS owner cabinet in a normal headed browser.

The window has no saved profile and no stored password. A person signs in.
This module does not click Save, Publish, or Submit.
"""

from __future__ import annotations

import os
from typing import Any

from zhol.browser.headed import launch_blank, open_headed
from zhol.browser.twogis.page import PlaywrightCabinet

LIVE_ENV = "ZHOL_2GIS_CABINET"

__all__ = ["LIVE_ENV", "launch_blank", "open_interactive"]


def open_interactive(url: str, *, playwright: Any = None) -> PlaywrightCabinet:
    return open_headed(
        url,
        enabled=os.environ.get(LIVE_ENV) == "1",
        closed_message="live 2GIS cabinet is closed",
        playwright=playwright,
        cabinet=PlaywrightCabinet,
    )
