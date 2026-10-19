"""Open public Yandex and Google map pages in a normal browser. A robot check stops the run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zhol.browser.fixtures import LoadedPage
from zhol.browser.maps.visible import candidates_from_links, parse_card, place_id
from zhol.browser.page import AccessRestricted, PageDocument, PageNotLoaded, UnexpectedPage
from zhol.browser.twogis.access import (
    ACCESS_DENIED,
    CARD_VISIBLE,
    NAVIGATION_ERROR,
    ROBOT_CHECK,
    classify_access,
    extraction_allowed,
    outcome_for,
    write_access,
)
from zhol.browser.twogis.capture import BrowserUnavailable, _launch, _page_text, _playwright
from zhol.timeutil import now

_CONSENT = ("before you continue", "прежде чем перейти")


def _phrases(spec: dict[str, Any], key: str) -> list[str]:
    values = spec.get(key)
    return list(values) if values else []


def _observe(page: Any, http_status: int | None, spec: dict[str, Any], navigation_error: str | None = None) -> str:
    return classify_access(
        http_status=http_status,
        url=page.url,
        text=_page_text(page),
        navigation_error=navigation_error,
        robot_texts=_phrases(spec, "robot_texts"),
        denied_texts=_phrases(spec, "denied_texts"),
    )


def _save_view(page: Any, folder: Path) -> tuple[Path, str]:
    folder.mkdir(parents=True, exist_ok=True)
    screenshot = folder / "screenshot.png"
    text = _page_text(page)
    try:
        page.screenshot(path=str(screenshot), full_page=True)
    except Exception:
        screenshot.write_bytes(b"")
    try:
        (folder / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        (folder / "page.html").write_text("", encoding="utf-8")
    (folder / "text.txt").write_text(text, encoding="utf-8")
    return screenshot, text


def _record(folder: Path, *, access: str, http_status: int | None, url: str, screenshot: Path, extracted: bool) -> None:
    write_access(
        folder,
        {
            "access": access,
            "outcome": outcome_for(access) if access in (CARD_VISIBLE, ROBOT_CHECK, ACCESS_DENIED, NAVIGATION_ERROR) else "stopped",
            "http_status": http_status,
            "url": url,
            "seen_at": now().isoformat(),
            "screenshot": str(screenshot),
            "extracted": extracted,
        },
    )


def _stop(page: Any, folder: Path, access: str, http_status: int | None) -> None:
    screenshot, _text = _save_view(page, folder)
    _record(folder, access=access, http_status=http_status, url=page.url, screenshot=screenshot, extracted=False)
    message = f"access={access} outcome={outcome_for(access) if access != 'consent_wall' else 'stopped'}"
    if access == NAVIGATION_ERROR:
        raise PageNotLoaded(message, screenshot=str(screenshot), url=page.url, access=access, outcome=NAVIGATION_ERROR)
    outcome = "stopped" if access == "consent_wall" else outcome_for(access)
    raise AccessRestricted(message, screenshot=str(screenshot), url=page.url, access=access, outcome=outcome)


def _links(page: Any, needle: str) -> list[tuple[str, str]]:
    raw = page.eval_on_selector_all(
        "a[href]",
        "nodes => nodes.map(node => [node.href, node.innerText || ''])",
    )
    found = []
    for href, text in raw or []:
        if needle and needle not in str(href):
            continue
        found.append((str(href), str(text or "")))
    return found


def _goto(page: Any, url: str, folder: Path, spec: dict[str, Any]) -> int | None:
    folder.mkdir(parents=True, exist_ok=True)
    screenshot = folder / "screenshot.png"
    timeout = int(spec.get("timeout_ms") or 15000)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as exc:
        try:
            page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            pass
        _record(folder, access=NAVIGATION_ERROR, http_status=None, url=url, screenshot=screenshot, extracted=False)
        raise PageNotLoaded(
            f"navigation failed: {type(exc).__name__}",
            screenshot=str(screenshot),
            url=url,
            access=NAVIGATION_ERROR,
            outcome=NAVIGATION_ERROR,
        ) from exc
    status = None if response is None else response.status
    access = _observe(page, status, spec)
    if not extraction_allowed(access):
        _stop(page, folder, access, status)
    return status


def _loaded_card(page: Any, folder: Path, spec: dict[str, Any], platform: str, status: int | None) -> LoadedPage:
    timeout = int(spec.get("timeout_ms") or 15000)
    screenshot = folder / "screenshot.png"
    try:
        page.get_by_role("heading").first.wait_for(state="visible", timeout=timeout)
        name = page.get_by_role("heading").first.inner_text().strip()
    except Exception as exc:
        _record(folder, access=NAVIGATION_ERROR, http_status=status, url=page.url, screenshot=screenshot, extracted=False)
        try:
            page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            pass
        raise UnexpectedPage(
            f"{platform} heading was not shown",
            screenshot=str(screenshot),
            url=page.url,
            access=NAVIGATION_ERROR,
            outcome=NAVIGATION_ERROR,
        ) from exc
    if not extraction_allowed(_observe(page, status, spec)):
        _stop(page, folder, _observe(page, status, spec), status)
    text = _page_text(page)
    folded = text.casefold()
    if any(phrase in folded for phrase in _CONSENT) and not _links(page, str(spec.get("link_href_contains") or "")):
        _stop(page, folder, "consent_wall", status)
    hrefs = page.eval_on_selector_all("a[href]", "nodes => nodes.map(node => node.href)")
    shot, _saved = _save_view(page, folder)
    _record(folder, access=CARD_VISIBLE, http_status=status, url=page.url, screenshot=shot, extracted=True)
    fields, notes = parse_card(text, name=name, hrefs=list(hrefs or []), platform=platform)
    (folder / "notes.txt").write_text("\n".join(f"{key}: {value}" for key, value in sorted(notes.items())) + "\n", encoding="utf-8")
    external_id = place_id(platform, page.url)
    document = PageDocument(
        page="card",
        platform=platform,
        external_id=external_id,
        fields=fields,
        hrefs={},
        notes=notes,
    )
    return LoadedPage(
        url=page.url,
        html=(folder / "page.html").read_text(encoding="utf-8"),
        raw_path=str(folder / "page.html"),
        file_name="page.html",
        screenshot_path=str(shot),
        kind="card",
        document=document,
    )


def _query_from_url(url: str) -> str:
    from urllib.parse import parse_qs, unquote, urlparse

    values = parse_qs(urlparse(url).query)
    for key in ("query", "text", "q"):
        if values.get(key):
            return unquote(values[key][0])
    return ""


def _type_query(page: Any, query: str, spec: dict[str, Any]) -> None:
    """Use the page search box when the address did not open a result. No other controls are clicked."""
    timeout = int(spec.get("timeout_ms") or 15000)
    box = None
    for role in ("combobox", "searchbox", "textbox"):
        locator = page.get_by_role(role)
        try:
            if locator.count() > 0:
                box = locator.first
                break
        except Exception:
            continue
    if box is None:
        return
    box.fill(query, timeout=timeout)
    box.press("Enter")
    needle = str(spec.get("link_href_contains") or "")
    try:
        page.locator(f"a[href*='{needle}']").first.wait_for(state="attached", timeout=timeout)
    except Exception:
        pass


def _loaded_search(page: Any, folder: Path, spec: dict[str, Any], platform: str, status: int | None) -> LoadedPage:
    timeout = int(spec.get("timeout_ms") or 15000)
    needle = str(spec.get("link_href_contains") or "")
    try:
        page.locator(f"a[href*='{needle}']").first.wait_for(state="attached", timeout=timeout)
    except Exception:
        pass
    hrefs = _links(page, needle)
    text = _page_text(page)
    if any(phrase in text.casefold() for phrase in _CONSENT) and not hrefs and place_id(platform, page.url) is None:
        _stop(page, folder, "consent_wall", status)
    if not extraction_allowed(_observe(page, status, spec)):
        _stop(page, folder, _observe(page, status, spec), status)
    self_id = place_id(platform, page.url)
    heading = ""
    try:
        heading = page.get_by_role("heading").first.inner_text().strip()
    except Exception:
        heading = ""
    if self_id and needle in page.url:
        prefix = heading + "\n" if heading else ""
        hrefs = [(page.url, prefix + text)] + hrefs
    candidates = candidates_from_links(platform, hrefs)
    shot, _saved = _save_view(page, folder)
    _record(folder, access=CARD_VISIBLE, http_status=status, url=page.url, screenshot=shot, extracted=True)
    document = PageDocument(
        page="search",
        platform=platform,
        external_id=self_id,
        fields={},
        hrefs={},
        candidates=candidates,
    )
    return LoadedPage(
        url=page.url,
        html=(folder / "page.html").read_text(encoding="utf-8"),
        raw_path=str(folder / "page.html"),
        file_name="page.html",
        screenshot_path=str(shot),
        kind="search",
        document=document,
    )


class MapsBrowser:
    """One visible Chromium window. It does not change fingerprints or solve checks."""

    def __init__(self, cfg: dict[str, Any], *, headed: bool = True) -> None:
        self.cfg = cfg
        self.headed = headed
        self._manager: Any = None
        self._browser: Any = None
        self._page: Any = None

    def __enter__(self) -> "MapsBrowser":
        manager = _playwright()()
        self._manager = manager
        playwright = manager.__enter__()
        self._browser = _launch(playwright, self.headed)
        self._page = self._browser.new_page()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._manager is not None:
                self._manager.__exit__(exc_type, exc, traceback)

    def _spec(self, platform: str) -> dict[str, Any]:
        spec = ((self.cfg.get("platforms") or {}).get(platform) or {}).get("live")
        if not isinstance(spec, dict):
            raise PageNotLoaded(f"{platform} live settings are missing")
        return spec

    def search(self, platform: str, _id_2gis: str, url: str, folder: Path) -> LoadedPage:
        spec = self._spec(platform)
        status = _goto(self._page, url, folder, spec)
        loaded = _loaded_search(self._page, folder, spec, platform, status)
        if loaded.document and loaded.document.candidates:
            return loaded
        if place_id(platform, self._page.url):
            return loaded
        query = _query_from_url(url)
        if not query:
            return loaded
        _type_query(self._page, query, spec)
        if not extraction_allowed(_observe(self._page, status, spec)):
            _stop(self._page, folder, _observe(self._page, status, spec), status)
        return _loaded_search(self._page, folder, spec, platform, status)

    def card(self, platform: str, _external_id: str, url: str, folder: Path) -> LoadedPage:
        spec = self._spec(platform)
        status = _goto(self._page, url, folder, spec)
        return _loaded_card(self._page, folder, spec, platform, status)


__all__ = ["BrowserUnavailable", "MapsBrowser"]
