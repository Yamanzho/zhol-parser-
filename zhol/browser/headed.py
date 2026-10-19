"""Open an owner cabinet in a normal headed browser.

The window has no saved profile and no stored password. A person signs in.
This module does not click Save, Publish, or Submit.
"""

from __future__ import annotations

from typing import Any

from zhol.browser.config import load_platforms


def launch_blank(playwright: Any, url: str) -> Any:
    """Start a visible browser with a new blank context. No profile directory is loaded."""
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    return page


def open_headed(
    url: str,
    *,
    enabled: bool,
    closed_message: str,
    playwright: Any = None,
    cabinet: Any,
) -> Any:
    if not enabled:
        raise PermissionError(closed_message)
    if not url.startswith("https://"):
        raise PermissionError("the cabinet address must be https")
    markers = load_platforms()["markers"]
    if playwright is None:
        return _live(url, markers, cabinet)
    return cabinet(launch_blank(playwright, url), markers)


def _live(url: str, markers: dict[str, str], cabinet: Any) -> Any:
    from playwright.sync_api import sync_playwright

    manager = sync_playwright().start()
    try:
        page = launch_blank(manager, url)
    except Exception:
        manager.stop()
        raise
    return cabinet(page, markers, close=manager.stop)
