"""Page session for saved HTML. The document URL is a local file, never an http address."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from zhol.browser.discovery.types import DiscoveryRefused

BLANK = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "pages" / "blank.png"
_TAG = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_MARKUP = re.compile(r"(?s)<[^>]+>")


def visible_text(html: str) -> str:
    text = _TAG.sub(" ", html)
    text = _MARKUP.sub(" ", text)
    return " ".join(text.split())


def document_url(path: Path) -> str:
    return path.resolve().as_uri()


def refuse_network(url: str) -> None:
    lowered = url.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        raise DiscoveryRefused("live requests are disabled")


class LocalHtmlPage:
    """The same four calls a Playwright page uses, without launching a browser."""

    def __init__(self) -> None:
        self.html = ""
        self.urls: list[str] = []
        self.closed = False

    def set_content(self, html: str, wait_until: str | None = None) -> None:
        self.html = html

    def content(self) -> str:
        return self.html

    def inner_text(self, selector: str) -> str:
        if selector != "body":
            raise DiscoveryRefused(f"unsupported selector {selector}")
        return visible_text(self.html)

    def screenshot(self, path: str, full_page: bool = True) -> None:
        """Copy the known blank fixture when it can be read. Never invent a file."""
        dest = Path(path)
        try:
            if not BLANK.is_file():
                return
            data = BLANK.read_bytes()
        except OSError:
            return
        if not data:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def close(self) -> None:
        self.html = ""
        self.closed = True


class PlaywrightSavedSession:
    """Loads saved HTML with page.set_content. It has no navigation to a website."""

    def __init__(self, page: Any) -> None:
        self.page = page

    def show(self, html: str, url: str, screenshot: Path) -> str:
        refuse_network(url)
        self.page.set_content(html, wait_until="domcontentloaded")
        text = self.page.inner_text("body")
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(screenshot), full_page=True)
        return text


def show_saved(page: Any, html: str, url: str, screenshot: Path) -> str:
    session = page if isinstance(page, PlaywrightSavedSession) else PlaywrightSavedSession(page)
    return session.show(html, url, screenshot)
