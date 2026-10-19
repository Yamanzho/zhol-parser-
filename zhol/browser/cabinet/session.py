"""Owner-cabinet session boundary.

A person signs in and passes any code or check. The listing batch reads a saved
page and refuses a live open, a stored password, and a form submit.
"""

from __future__ import annotations

from pathlib import Path

from zhol.guards import refuse_blocked_browser_action

_MANUAL = {"captcha": "solve_captcha", "sms": "read_sms", "login": "type_password"}


class CabinetStopped(Exception):
    """The saved cabinet page is not the organization in the order, or the screen is unknown."""


class BrowserCrash(Exception):
    """The browser closed and the submit result is not known."""


def require_human_check(kind: str) -> None:
    """The matching browser action is refused. The caller records a pause instead."""
    refuse_blocked_browser_action(_MANUAL[kind])


def open_owner_cabinet(url: str) -> None:
    """Live owner cabinets stay closed in this batch."""
    if url.startswith(("http://", "https://")):
        refuse_blocked_browser_action("bypass_login")
    refuse_blocked_browser_action("bypass_login")


def read_local_html(source: str) -> str:
    """Read HTML the caller already has. A web address is not fetched."""
    if source.startswith(("http://", "https://")):
        raise PermissionError("owner cabinet pages are not opened in this batch")
    path = Path(source)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if "<" in source:
        return source
    raise FileNotFoundError(source)
