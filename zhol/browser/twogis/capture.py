from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from zhol.browser.fixtures import LoadedPage
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
from zhol.browser.twogis.public import PLATFORM
from zhol.browser.twogis.visible import parse_visible
from zhol.timeutil import now

FIRM_URL = re.compile(r"^https://2gis\.kz/[a-z0-9_-]+/firm/\d+/?$")


class BrowserUnavailable(PageNotLoaded):
    pass


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable("playwright is not installed") from exc
    return sync_playwright


def _firm_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _phrases(spec: dict[str, Any], key: str) -> list[str]:
    values = spec.get(key)
    return list(values) if values else []


def _page_text(page: Any) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _observe(page: Any, http_status: int | None, spec: dict[str, Any], navigation_error: str | None = None) -> str:
    return classify_access(
        http_status=http_status,
        url=page.url,
        text=_page_text(page),
        navigation_error=navigation_error,
        robot_texts=_phrases(spec, "robot_texts"),
        denied_texts=_phrases(spec, "denied_texts"),
    )


def _save_view(page: Any, folder: Path) -> tuple[Path, Path, Path, str]:
    screenshot = folder / "screenshot.png"
    html_path = folder / "page.html"
    text_path = folder / "text.txt"
    text = _page_text(page)
    try:
        page.screenshot(path=str(screenshot), full_page=True)
    except Exception:
        screenshot.write_bytes(b"")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:
        html_path.write_text("", encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    return screenshot, html_path, text_path, text


def _record(folder: Path, *, access: str, http_status: int | None, url: str, screenshot: Path, extracted: bool) -> None:
    write_access(
        folder,
        {
            "access": access,
            "outcome": outcome_for(access),
            "http_status": http_status,
            "url": url,
            "seen_at": now().isoformat(),
            "screenshot": str(screenshot),
            "extracted": extracted,
        },
    )


def _stop(folder: Path, page: Any, access: str, http_status: int | None) -> None:
    screenshot, _html, _text, _body = _save_view(page, folder)
    _record(folder, access=access, http_status=http_status, url=page.url, screenshot=screenshot, extracted=False)
    message = f"access={access} outcome={outcome_for(access)}"
    if access == NAVIGATION_ERROR:
        raise PageNotLoaded(message, screenshot=str(screenshot), url=page.url, access=access, outcome=outcome_for(access))
    raise AccessRestricted(message, screenshot=str(screenshot), url=page.url, access=access, outcome=outcome_for(access))


def _poll_for_card(page: Any, spec: dict[str, Any], pause_seconds: float) -> str:
    print(
        "Проверка на робота. Окно браузера оставлено открытым. Пройдите её сами, если хотите. "
        "Программа кнопки не нажимает.",
        flush=True,
    )
    deadline = time.monotonic() + pause_seconds
    while True:
        access = _observe(page, 200, spec)
        if access in (CARD_VISIBLE, ACCESS_DENIED):
            return access
        if time.monotonic() >= deadline:
            return access
        page.wait_for_timeout(1000)


def _launch(playwright: Any, headed: bool) -> Any:
    try:
        return playwright.chromium.launch(headless=not headed)
    except Exception as exc:
        raise BrowserUnavailable(f"chromium is not available: {type(exc).__name__}: {exc}") from exc


def _read_firm(page: Any, url: str, folder: Path, spec: dict[str, Any], pause_seconds: float) -> LoadedPage:
    """Read one firm page that is already open in a normal browser. Does not solve a robot check."""
    folder = Path(folder)
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
    if access == ROBOT_CHECK and pause_seconds > 0:
        access = _poll_for_card(page, spec, pause_seconds)
    if not extraction_allowed(access):
        _stop(folder, page, access, status)
    try:
        page.get_by_role("heading").first.wait_for(state="visible", timeout=timeout)
    except Exception as exc:
        _record(folder, access=NAVIGATION_ERROR, http_status=status, url=page.url, screenshot=screenshot, extracted=False)
        page.screenshot(path=str(screenshot), full_page=True)
        raise UnexpectedPage(
            "2GIS firm heading was not shown",
            screenshot=str(screenshot),
            url=page.url,
            access=NAVIGATION_ERROR,
            outcome=NAVIGATION_ERROR,
        ) from exc
    name = page.get_by_role("heading").first.inner_text().strip()
    overview = _page_text(page)
    hrefs = page.eval_on_selector_all("a[href]", "nodes => nodes.map(node => node.href)")
    if not extraction_allowed(_observe(page, status, spec)):
        _stop(folder, page, _observe(page, status, spec), status)
    page.screenshot(path=str(screenshot), full_page=True)
    html = page.content()
    final = page.url
    tabs = spec.get("tabs") or {}
    reviews_text = _open_tab(page, tabs.get("reviews"), timeout, spec, screenshot, folder, status)
    photos_text = _open_tab(page, tabs.get("photos"), timeout, spec, screenshot, folder, status)
    prices_text = _open_tab(page, tabs.get("prices"), timeout, spec, screenshot, folder, status)
    info_text = _open_tab(page, tabs.get("info"), timeout, spec, screenshot, folder, status)
    return _loaded(
        url,
        folder,
        spec,
        name=name,
        overview=overview,
        hrefs=list(hrefs or []),
        html=html,
        final=final,
        status=status,
        screenshot=screenshot,
        reviews_text=reviews_text,
        photos_text=photos_text,
        prices_text=prices_text,
        info_text=info_text,
    )


def _loaded(
    url: str,
    folder: Path,
    spec: dict[str, Any],
    *,
    name: str,
    overview: str,
    hrefs: list[str],
    html: str,
    final: str,
    status: int | None,
    screenshot: Path,
    reviews_text: str | None,
    photos_text: str | None,
    prices_text: str | None,
    info_text: str | None,
) -> LoadedPage:
    html_path = folder / "page.html"
    text_path = folder / "text.txt"
    html_path.write_text(html, encoding="utf-8")
    text_path.write_text(overview, encoding="utf-8")
    _record(folder, access=CARD_VISIBLE, http_status=status, url=final, screenshot=screenshot, extracted=True)
    fields, notes = parse_visible(
        overview,
        name=name,
        hrefs=hrefs,
        patterns=dict(spec.get("patterns") or {}),
        labels={key: list(value) for key, value in (spec.get("labels") or {}).items()},
        prices_text=prices_text,
        price_phrases={key: list(value) for key, value in (spec.get("price_phrases") or {}).items()},
        reviews_text=reviews_text,
        info_text=info_text,
    )
    if photos_text and "photos_count" not in fields:
        photo_fields, photo_notes = parse_visible(
            photos_text,
            name=None,
            hrefs=[],
            patterns=dict(spec.get("patterns") or {}),
            labels={},
            prices_text=None,
            price_phrases={},
            reviews_text=None,
        )
        if photo_fields.get("photos_count"):
            fields["photos_count"] = photo_fields["photos_count"]
            notes["photos_count"] = photo_notes.get("photos_count", "shown")
    (folder / "notes.txt").write_text(
        "\n".join(f"{key}: {value}" for key, value in sorted(notes.items())),
        encoding="utf-8",
    )
    document = PageDocument(
        page="card",
        platform=PLATFORM,
        external_id=_firm_id(url),
        fields=fields,
        hrefs={},
        notes=notes,
    )
    return LoadedPage(
        url=final,
        html=html,
        raw_path=str(html_path),
        file_name=html_path.name,
        screenshot_path=str(screenshot),
        kind="card",
        document=document,
    )


def capture_firm(
    url: str,
    dest: Path,
    spec: dict[str, Any],
    *,
    headed: bool = False,
    pause_seconds: float = 0,
) -> LoadedPage:
    """Open one public 2GIS firm page. A robot check is not collected as a card."""
    if not FIRM_URL.match(url):
        raise PageNotLoaded(f"not a public 2GIS firm url: {url}", access=NAVIGATION_ERROR, outcome=NAVIGATION_ERROR)
    sync_playwright = _playwright()
    with sync_playwright() as playwright:
        browser = _launch(playwright, headed)
        try:
            return _read_firm(browser.new_page(), url, Path(dest), spec, pause_seconds)
        finally:
            browser.close()


@dataclass
class FirmResult:
    url: str
    page: LoadedPage | None = None
    error: BaseException | None = None


def open_sequence(
    pairs: list[tuple[str, Path]],
    reader: Callable[[str, Path], LoadedPage],
    *,
    wait_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[FirmResult]:
    """Read firm pages in order. A robot check stops the sequence before the next address."""
    results: list[FirmResult] = []
    for index, (url, folder) in enumerate(pairs):
        if index and wait_seconds:
            sleeper(wait_seconds)
        try:
            results.append(FirmResult(url, reader(url, folder)))
        except (AccessRestricted, BrowserUnavailable) as exc:
            results.append(FirmResult(url, error=exc))
            break
        except (PageNotLoaded, UnexpectedPage) as exc:
            results.append(FirmResult(url, error=exc))
    return results


def capture_firms(
    pairs: list[tuple[str, Path]],
    spec: dict[str, Any],
    *,
    headed: bool = True,
    pause_seconds: float = 0,
    wait_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[FirmResult]:
    """One normal visible browser for several public firm pages. No fingerprint spoofing."""
    for url, _folder in pairs:
        if not FIRM_URL.match(url):
            raise PageNotLoaded(f"not a public 2GIS firm url: {url}", access=NAVIGATION_ERROR, outcome=NAVIGATION_ERROR)
    sync_playwright = _playwright()
    with sync_playwright() as playwright:
        browser = _launch(playwright, headed)
        try:
            page = browser.new_page()

            def reader(url: str, folder: Path) -> LoadedPage:
                return _read_firm(page, url, folder, spec, pause_seconds)

            return open_sequence(pairs, reader, wait_seconds=wait_seconds, sleeper=sleeper)
        finally:
            browser.close()


def _open_tab(page: Any, label: str | None, timeout: int, spec: dict[str, Any], screenshot: Path, folder: Path, http_status: int | None) -> str | None:
    if not label:
        return None
    locator = page.get_by_text(label, exact=True)
    try:
        if locator.count() == 0:
            return None
        locator.first.click(timeout=timeout)
    except Exception:
        return None
    access = _observe(page, http_status, spec)
    if not extraction_allowed(access):
        _stop(folder, page, access, http_status)
    return _page_text(page)


def fetch_html(url: str, spec: dict[str, Any] | None = None) -> str:
    from zhol.browser.config import load_platforms
    from zhol.browser.twogis.visible import live_spec

    page = capture_firm(url, Path("data/scout-pilot") / _firm_id(url), spec or live_spec(load_platforms()))
    return page.html


class LiveStore:
    """Public 2GIS pages only. Yandex and Google stay on saved fixtures."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.screenshot = None

    def card(self, platform: str, external_id: str, url: str) -> LoadedPage:
        if platform != "2gis":
            from zhol.browser.live import LiveCollectionDisabled

            raise LiveCollectionDisabled(f"live {platform} collection is not part of this batch")
        from zhol.browser.config import load_platforms
        from zhol.browser.twogis.visible import live_spec

        return capture_firm(url, self.root / external_id, live_spec(load_platforms()))

    def search(self, platform: str, id_2gis: str, url: str) -> LoadedPage:
        from zhol.browser.live import LiveCollectionDisabled

        raise LiveCollectionDisabled("live search is not part of this batch")
