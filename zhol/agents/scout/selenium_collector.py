"""Zhol job adapter for the existing Selenium 2GIS collector.

Parsing stays in search.py and card.py. This module does not write Excel
and does not start Chrome unless the browser grant is set and no driver
was injected. A robot check keeps that same Chrome session and waits.
The program does not solve or hide that check, and it does not open a second window.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from zhol.agents.scout.browser_2gis import (
    LOCAL_BASE_ENV,
    SOURCE_MODE,
    active_source,
    browser_collection_authorized,
    grant_scope,
    local_base_url,
    local_html_root,
    refuse_browser_collection,
)
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.live_2gis import _mark, _progress, _save, _status
from zhol.agents.scout.normalize import normalize_platform_id, phone_parts
from zhol.agents.scout.persist import find_reusable, insert_snapshot, remember_collection_context, remember_platform_id, remember_search_query
from zhol.browser.discovery.normalize import clean_address, verified_instagram, verified_whatsapp
from zhol.browser.pace import wait_turn
from zhol.browser.discovery.types import RunControl
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company
from zhol.timeutil import now

# A card is opened again only while its class is temporary and attempts remain.
# A captcha recorded on one card is not opened again. A page-level robot check
# pauses the whole batch instead, and that card is not marked rejected.
MAX_CARD_ATTEMPTS = 3
TEMPORARY_FAILURES = frozenset({"page_timeout", "network_failure", "browser_session"})
PERMANENT_FAILURES = frozenset({"missing_identity", "invalid_card", "access_restriction", "captcha"})
AWAITING_MANUAL_VERIFICATION = "awaiting_manual_verification"
VERIFY_PASSED = "verification_passed"
VERIFY_FAILED = "verification_failed"
_VERIFY_WAIT_SECONDS = 20 * 60

MESSAGE_PENDING = (
    "Площадка попросила проверку в браузере. "
    "Проверка проходит в том же окне Chrome, которое ведет сбор. "
    "Программа проверку не решает и не обходит. "
    "Уже сохраненные компании и снимки остаются."
)
MESSAGE_OPEN = (
    "Открыто то же окно Chrome, в котором шел сбор. Пройдите проверку сами. "
    "Программа не нажимает проверку и не скрывает её. "
    "Новое окно не открывается. "
    "После этого нажмите «Проверка пройдена» или «Проверка не пройдена»."
)
MESSAGE_PASSED = (
    "Проверка пройдена вручную, и страница больше не просит её. "
    "Сбор продолжается в том же окне Chrome. "
    "Лимит и паузы между запросами сохраняются. Повторный снимок уже записанной карточки не создается."
)
MESSAGE_FAILED = (
    "Проверка в браузере не пройдена. Сбор остановлен. "
    "Сохраненные компании остаются. Автоматическое решение проверки не выполняется."
)
MESSAGE_STILL = (
    "Страница по-прежнему просит проверку. Программа её не решает. "
    "Сбор остановлен. Сохраненные компании остаются."
)
MESSAGE_DENIED = (
    "Площадка ограничила доступ. Обход не выполняется. "
    "Сбор остановлен. Сохраненные компании остаются."
)
MESSAGE_UNREAD = (
    "Страница проверки не прочитана. Сбор остановлен. "
    "Сохраненные компании остаются. Проверка не решается автоматически."
)
MESSAGE_STOP = (
    "Сбор остановлен. Проверка не завершена. Сохраненные компании остаются."
)
MESSAGE_TIMEOUT = (
    "Окно проверки закрыто: время ожидания вышло. "
    "Новое окно не открывается, потому что это была бы другая сессия. "
    "Проверка не решается автоматически. Сохраненные компании остаются."
)
MESSAGE_WINDOW = (
    "То же окно Chrome не удалось удержать. Новое окно не открывается. "
    "Проверка не решается автоматически. Сохраненные компании остаются."
)
MESSAGE_NO_DISPLAY = (
    "Видимое окно Chrome здесь не открывается: у процесса сборщика нет экрана. "
    "Новое окно не открывается, потому что это была бы другая сессия. "
    "Проверка не решается автоматически. Сохраненные компании остаются."
)
MESSAGE_HEADLESS = (
    "Текущий Chrome запущен без окна. Ту же сессию показать нельзя, "
    "и новое окно не открывается. "
    "Проверка не решается автоматически. Сохраненные компании остаются."
)
MESSAGE_SESSION_CLOSED = (
    "Исходное окно Chrome уже закрыто. Новое окно не открывается: "
    "это была бы другая сессия, а не продолжение той же. "
    "Проверка не решается автоматически. Сохраненные компании остаются."
)
MESSAGE_ALREADY_OPEN = (
    "Исходное окно Chrome уже открыто. Пройдите проверку в нём. Новое окно не открывается."
)
MESSAGE_NOT_PERMITTED = (
    "Видимый Chrome для проверки не открыт: сайт 2ГИС не разрешен. "
    "Локальное некоммерческое разрешение и коммерческое разрешение задаются отдельно и не заменяют друг друга. "
    "Проверка не обходится. Сбор остановлен. Сохраненные компании остаются."
)
MESSAGE_LOCAL_BOUNDARY = (
    "Видимый Chrome не открыл страницу: локальный сбор не переходит на внешний сайт. "
    "Это не обход проверки. Сбор остановлен. Сохраненные компании остаются."
)
MESSAGE_RESUME_BLOCKED = "Сначала пройдите проверку в видимом Chrome. Программа её не решает."

COMPANY_SOURCE = SOURCE_MODE
CARD_KIND = "selenium_public_card"
LIVE_ORIGIN = "https://2gis.kz"
CARD_DELAY = (1.0, 1.5)
ROOT = Path(__file__).resolve().parents[3]

DriverFactory = Callable[[], Any]


class CityNotConfigured(BatchError):
    pass


@dataclass(frozen=True)
class BrowserFirm:
    firm_id: str
    name: str
    city: str
    address: str | None
    address_reason: str | None
    phones: tuple[str, ...]
    phone_display: tuple[str, ...]
    phones_reason: str | None
    whatsapp: str | None
    whatsapp_reason: str | None
    instagram: str | None
    instagram_reason: str | None
    url: str
    query: str
    collected_at: str
    screenshot_path: str | None = None
    shown_city: str | None = None
    category: str | None = None
    category_checked: bool = False


class VerificationSettled(Exception):
    """The batch row already records how manual verification ended. The caller closes Chrome."""


@dataclass
class _Flag:
    control: RunControl
    forced: bool = False

    def set(self) -> None:
        self.forced = True

    def is_set(self) -> bool:
        return self.forced or self.control._stopped or self.control._paused


def _phone_number(raw: str) -> tuple[str | None, str | None]:
    return phone_parts(raw)


def _whatsapp_number(raw: str) -> tuple[str | None, str | None]:
    number = verified_whatsapp(raw, None)
    return (number, raw) if number else (None, None)


def _instagram_handle(raw: str) -> tuple[str | None, str | None]:
    handle = verified_instagram(raw, None)
    return (handle, handle) if handle else (None, None)


def _hidden(record: dict[str, Any], field: str) -> bool:
    errors = (record.get("diagnostics") or {}).get("errors") or []
    return any(isinstance(item, dict) and item.get("field") == field for item in errors)


def _contact_list(record: dict[str, Any], key: str, parser) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    """None means the value was read. empty means the card showed no value. not_shown means it was not read."""
    if key not in record:
        return (), (), "not_shown"
    numbers: list[str] = []
    shown: list[str] = []
    for raw in record.get(key) or []:
        number, display = parser(str(raw))
        if number and number not in numbers:
            numbers.append(number)
            if display:
                shown.append(display)
    if numbers:
        return tuple(numbers), tuple(shown), None
    if key == "phones" and _hidden(record, "phones"):
        return (), (), "not_shown"
    return (), (), "empty"


def _address_state(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """A missing address link is not a reason to drop the card. The text is never invented."""
    if "address" not in record and "address_seen" not in record:
        return None, "not_shown"
    address = clean_address(record.get("address"))
    if address:
        return address, None
    if record.get("address_seen"):
        return None, "empty"
    return None, "not_shown"


def normalize_firm(record: dict[str, Any], *, city: str, query: str, slug: str = "almaty") -> BrowserFirm | None:
    """Map one Selenium record onto the fields the card actually showed."""
    firm_id = normalize_platform_id("2gis", str(record.get("branch_id") or ""))
    name = " ".join(str(record.get("name") or "").split())
    if not firm_id or not firm_id.isdigit() or len(firm_id) < 8 or not name:
        return None
    phones, phone_display, phones_reason = _contact_list(record, "phones", _phone_number)
    whatsapp, _whatsapp_display, whatsapp_reason = _contact_list(record, "whatsapp", _whatsapp_number)
    instagram, _instagram_display, instagram_reason = _contact_list(record, "instagram", _instagram_handle)
    address, address_reason = _address_state(record)
    category_checked = bool(record.get("category_seen"))
    category = " ".join(str(record.get("category") or "").split()) if category_checked else ""
    category = category or None
    url = str(record.get("two_gis_url") or "").split("?", 1)[0].split("#", 1)[0]
    if f"/firm/{firm_id}" not in url and slug and all(part.isalnum() or part in "-_" for part in (slug,)):
        url = f"https://2gis.kz/{slug}/firm/{firm_id}"
    card_city = " ".join(str(record.get("city") or "").split())
    return BrowserFirm(
        firm_id=firm_id,
        name=name,
        city=card_city or city,
        shown_city=card_city or None,
        address=address,
        address_reason=address_reason,
        phones=phones,
        phone_display=phone_display,
        phones_reason=phones_reason,
        whatsapp=whatsapp[0] if whatsapp else None,
        whatsapp_reason=whatsapp_reason,
        instagram=instagram[0] if instagram else None,
        instagram_reason=instagram_reason,
        url=url,
        query=query,
        collected_at=str(record.get("collected_at") or ""),
        category=category,
        category_checked=category_checked,
    )


def collect_with_driver(
    driver: Any,
    *,
    city: str,
    query: str,
    limit: int | None,
    stop: _Flag | Any,
    known_ids: set[str] | None,
    run_dir: Path,
    pause: Callable[[str], None],
    delay_range: tuple[float, float] | None = None,
    refresh_ids: set[str] | None = None,
    persist: Callable[[dict[str, Any], Any], bool | str] | None = None,
    on_error: Callable[[dict[str, str]], None] | None = None,
    reserved_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Read one query through search.run_search. The driver is not closed here.

    When persist is set, each accepted card is committed before the next card opens.
    """
    if not browser_collection_authorized():
        refuse_browser_collection()
    city_name, slug = _city(city)
    search, _parser = _collector_modules()
    records: list[dict[str, Any]] = []

    def on_record(record: dict[str, Any]) -> bool | str:
        if persist is None:
            records.append(record)
            return True
        return persist(record, driver)

    card_delay, pace = _delays(delay_range)
    result = search.run_search(
        driver,
        city_name,
        slug,
        query,
        limit,
        pause,
        stop,
        on_record=on_record,
        delay_range=card_delay,
        known_ids=known_ids,
        quiet=True,
        run_dir=run_dir,
        pace=pace,
        refresh_ids=refresh_ids,
        on_error=on_error,
        reserved_ids=reserved_ids,
    )
    firms: list[BrowserFirm] = []
    if persist is not None:
        records.clear()
    for record in records:
        firm = normalize_firm(record, city=city_name, query=query, slug=slug)
        if firm is None:
            continue
        shot = _capture(driver, run_dir, firm.firm_id)
        if shot:
            firm = _with_screenshot(firm, shot)
        firms.append(firm)
    errors = _error_rows(Path(result["run_dir"]) / "errors.jsonl")
    seen = set(known_ids or ())
    seen.update(firm.firm_id for firm in firms)
    checkpoint = {
        "city": city_name,
        "slug": slug,
        "query": query,
        "seen_ids": sorted(seen),
        "pages_loaded": int(result.get("pages_loaded") or 0),
        "link_collection_complete": bool(result.get("link_collection_complete")),
        "stop_reason": result.get("stop_reason"),
        "status": result.get("status"),
    }
    return {
        "firms": firms,
        "stats": {
            "unique_links": int(result.get("unique_links") or 0),
            "attempted": int(result.get("attempted") or 0),
            "success": int(result.get("success") or 0),
            "duplicates": int(result.get("duplicates") or 0),
            "errors": int(result.get("errors") or 0),
            "skipped": int(result.get("skipped") or 0),
        },
        "errors": errors,
        "skipped_known_ids": [str(item) for item in result.get("skipped_known_ids") or []],
        "checkpoint": checkpoint,
        "confirmation": False,
    }


def classify_failure(message: str, kind: str = "") -> str:
    """Map a parser error onto one stored class. Unknown card text is not retried."""
    text = f"{kind} {message}".casefold()
    if any(token in text for token in ("captcha", "recaptcha", "hcaptcha", "smartcaptcha", "капча", "не робот")):
        return "captcha"
    if any(
        token in text
        for token in (
            "доступ ограничен",
            "доступ к карточке ограничен",
            "доступ запрещ",
            "access denied",
            "too many requests",
        )
    ):
        return "access_restriction"
    if any(
        token in text
        for token in (
            "err_connection",
            "err_internet",
            "err_name_not_resolved",
            "err_network",
            "err_address",
            "err_empty_response",
            "err_socket",
            "net::err",
            "connection reset",
            "connection refused",
            "connection aborted",
            "connection closed",
            "name or service not known",
            "temporary failure in name resolution",
            "network is unreachable",
        )
    ):
        return "network_failure"
    if any(
        token in text
        for token in (
            "tab crashed",
            "chrome not reachable",
            "invalid session",
            "session deleted",
            "target frame detached",
            "disconnected",
        )
    ):
        return "browser_session"
    if any(token in text for token in ("слишком долго", "timed out", "timeout", "время ожидания")):
        return "page_timeout"
    if any(
        token in text
        for token in (
            "нет идентификатора филиала или названия",
            "в ссылке нет идентификатора филиала",
            "не найдено название",
            "идентификатор в ссылке не совпал",
        )
    ):
        return "missing_identity"
    if any(
        token in text
        for token in (
            "на странице нет названия или идентификатора",
            "не удалось прочитать область",
        )
    ):
        return "invalid_card"
    return "invalid_card"


def run_browser_batch(
    factory: sessionmaker[Session],
    *,
    city: str,
    category: str,
    limit: int,
    out: Path,
    resume: int | None = None,
    control: RunControl | None = None,
    on_open: Callable[[int], None] | None = None,
    ensure_owner: Callable[[], None] | None = None,
    driver_factory: DriverFactory | None = None,
    run_dir: Path | None = None,
    delay_range: tuple[float, float] | None = None,
    job_attempt: int | None = None,
    operator_sleep: Callable[[float], None] | None = None,
    verification_wait_seconds: float | None = None,
    hold_completion: bool = False,
    slice_limit: int | None = None,
) -> int:
    """Run one saved query. Chrome starts only for an authorized call without an injected driver."""
    if not browser_collection_authorized():
        refuse_browser_collection()
    if limit < 1:
        raise BatchError("Лимит должен быть от 1 до 500.")
    try:
        city_name, slug = _city(city)
    except CityNotConfigured as exc:
        raise BatchError(str(exc)) from exc
    control = control or RunControl()
    batch_id = resume or _open(factory, city=city_name, category=category, limit=limit, out=out)
    if on_open is not None:
        on_open(batch_id)
    if _status(factory, batch_id) == "stopped":
        return batch_id
    if _status(factory, batch_id) == AWAITING_MANUAL_VERIFICATION and not _verification_passed(factory, batch_id):
        return batch_id
    if _status(factory, batch_id) == "completed" and not _retryable_ids(factory, batch_id):
        return batch_id
    if resume is not None:
        stored_limit = _item_limit(factory, batch_id)
        if stored_limit is not None:
            limit = stored_limit
    remembered = _remembered_delay(factory, batch_id)
    if remembered is not None:
        delay_range = remembered
    elif delay_range is not None:
        _remember_delay(factory, batch_id, delay_range)
    if control._stopped:
        _mark(factory, batch_id, "stopped", finish=True)
        return batch_id
    if control._paused:
        _mark(factory, batch_id, "paused", finish=False)
        return batch_id
    state = _progress(factory, batch_id)
    seen = {str(item) for item in state.get("seen_ids") or []}
    rejected = {str(item) for item in state.get("rejected_ids") or []}
    retryable = _retryable_ids(factory, batch_id)
    existing_ids = _existing_ids(factory)
    # Finished cards in this batch are not opened again.
    # A company already stored by another batch still spends one slot when this batch opens it.
    # A retry is the same distinct card, so it does not spend a second slot.
    finished = seen | rejected
    new_slots = limit - len(finished) - len(retryable)
    if slice_limit is not None:
        new_slots = min(new_slots, slice_limit)
    if new_slots < 1 and not retryable:
        _mark(factory, batch_id, "completed", finish=True)
        return batch_id
    if new_slots < 0:
        new_slots = 0
    known = finished
    artifact = run_dir or Path(out).parent / f"browser-run-{batch_id}"
    artifact.mkdir(parents=True, exist_ok=True)
    stop = _Flag(control)
    driver = None
    site: _LocalHtmlSite | None = None

    def accept(record: dict[str, Any], page: Any) -> bool | str:
        if ensure_owner is not None:
            ensure_owner()
        firm = normalize_firm(record, city=city_name, query=category, slug=slug)
        if firm is None:
            branch = str(record.get("branch_id") or "")
            if branch:
                _commit_failure(
                    factory,
                    batch_id,
                    branch,
                    "Нет идентификатора филиала или названия",
                    kind="identity",
                )
            return "invalid"
        shot = _capture(page, artifact, firm.firm_id)
        if shot:
            firm = _with_screenshot(firm, shot)
        kind = _commit_firm(factory, batch_id, firm)
        return False if kind == "duplicate" else True

    def reject(item: dict[str, str]) -> None:
        firm_id = str(item.get("branch_id") or "")
        if not firm_id:
            return
        _commit_failure(
            factory,
            batch_id,
            firm_id,
            str(item.get("message") or "Карточка не прочитана"),
            kind=str(item.get("kind") or ""),
        )

    try:
        if resume is not None:
            _note_browser_restart(factory, batch_id)
        if job_attempt is not None:
            _note_job_retry(factory, batch_id, max(0, int(job_attempt) - 1))
        site = _maybe_local_site()
        driver = driver_factory() if driver_factory is not None else _open_chrome()
        if ensure_owner is not None:
            ensure_owner()
        _mark(factory, batch_id, "running", finish=False)
        state = _progress(factory, batch_id)
        state["phase"] = "collect"
        state["active_query"] = category
        state["browser_source"] = active_source()
        state["browser_origin"] = local_base_url() or LIVE_ORIGIN
        _save(factory, batch_id, state)
        outcome = collect_with_driver(
            driver,
            city=city_name,
            query=category,
            limit=new_slots,
            stop=stop,
            known_ids=known,
            run_dir=artifact,
            pause=_pause(
                stop,
                driver,
                factory=factory,
                batch_id=batch_id,
                control=control,
                sleep=operator_sleep or _operator_sleep,
                max_wait_seconds=(
                    _VERIFY_WAIT_SECONDS if verification_wait_seconds is None else verification_wait_seconds
                ),
            ),
            delay_range=delay_range,
            refresh_ids=None,
            persist=accept,
            on_error=reject,
            reserved_ids=retryable,
        )
        for firm_id in outcome["skipped_known_ids"]:
            if firm_id not in existing_ids or firm_id not in seen:
                continue
            if ensure_owner is not None:
                ensure_owner()
            _link_existing(factory, batch_id, firm_id, category)
        checkpoint = outcome["checkpoint"]
        stored = _progress(factory, batch_id)
        waiting = _retryable_ids(factory, batch_id)
        if waiting:
            stored["retry_pending"] = True
        else:
            stored.pop("retry_pending", None)
            stored.pop("interruption", None)
        stored.update(
            {
                "phase": "collect",
                "active_query": category,
                "city_slug": checkpoint["slug"],
                "pages_loaded": checkpoint["pages_loaded"],
                "link_collection_complete": checkpoint["link_collection_complete"],
                "stop_reason": checkpoint["stop_reason"],
                "hits": len(stored.get("seen_ids") or []),
                "duplicates": outcome["stats"]["duplicates"],
                "errors": outcome["stats"]["errors"],
            }
        )
        _save(factory, batch_id, stored)
        if control._stopped:
            _mark(factory, batch_id, "stopped", finish=True)
        elif control._paused:
            _mark(factory, batch_id, "paused", finish=False)
        elif stop.forced or outcome["checkpoint"]["status"] == "interrupted" or waiting:
            _mark(factory, batch_id, "interrupted", finish=True)
        elif hold_completion and _source_cap_open(factory, batch_id):
            _mark(factory, batch_id, "running", finish=False)
        else:
            _mark(factory, batch_id, "completed", finish=True)
        return batch_id
    except Exception as exc:
        from browser_ops import is_browser_crash

        parser = _collector_modules()[1]
        if isinstance(exc, VerificationSettled):
            return batch_id
        if isinstance(exc, parser.ManualConfirmationRequired):
            if control._stopped:
                _mark(factory, batch_id, "stopped", finish=True)
                return batch_id
            if not visible_chrome_available():
                hold_message = MESSAGE_NO_DISPLAY
            elif getattr(driver, "_zhol_headed", None) is False:
                hold_message = MESSAGE_HEADLESS
            else:
                hold_message = MESSAGE_PENDING
            _hold_for_manual_verification(
                factory,
                batch_id,
                url=_page_url(exc, driver),
                reason=str(exc)[:300],
                message=hold_message,
            )
            return batch_id
        state = _progress(factory, batch_id)
        state["error"] = str(exc)[:300]
        if is_browser_crash(exc):
            state["interruption"] = {
                "classification": "browser_session",
                "retryable": True,
                "reason": str(exc)[:300],
            }
        _save(factory, batch_id, state)
        _mark(factory, batch_id, "interrupted", finish=True)
        raise
    finally:
        _close_driver(driver)
        if driver is not None:
            _force_browser_closed(factory, batch_id)
        if site is not None:
            site.close()


def _source_cap_open(factory: sessionmaker[Session], batch_id: int) -> bool:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or batch.item_limit is None:
            return False
        progress = batch.progress if isinstance(batch.progress, dict) else {}
        hits = len(progress.get("seen_ids") or [])
        return hits < int(batch.item_limit)


def _city(city: str) -> tuple[str, str]:
    parser = _collector_modules()[1]
    try:
        return parser.resolve_city(city, None)
    except ValueError as exc:
        raise CityNotConfigured(str(exc)) from exc


def _collector_modules() -> tuple[Any, Any]:
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    import parser_service
    import search

    return search, parser_service


class _LocalHtmlSite:
    """Serve saved search and firm pages on 127.0.0.1. Does not open 2gis.kz."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.origin = ""
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> str:
        root = self.root

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                firm = re.search(r"/firm/(\d+)", path)
                if firm:
                    target = (root / f"{firm.group(1)}.html").resolve()
                elif "/search/" in path:
                    target = (root / "search.html").resolve()
                else:
                    target = None
                if target is None or target.parent != root or not target.is_file():
                    self.send_error(404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, name="local-html", daemon=True).start()
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        os.environ[LOCAL_BASE_ENV] = self.origin
        return self.origin

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.origin and os.environ.get(LOCAL_BASE_ENV) == self.origin:
            os.environ.pop(LOCAL_BASE_ENV, None)


def _maybe_local_site() -> _LocalHtmlSite | None:
    if local_base_url() or active_source() != "local_html":
        return None
    root = local_html_root()
    if root is None:
        return None
    site = _LocalHtmlSite(root)
    site.start()
    return site


def _open_chrome(*, headless: bool | None = None) -> Any:
    if not browser_collection_authorized():
        refuse_browser_collection()
    _search, parser = _collector_modules()
    if headless is None:
        headless = os.environ.get("ZHOL_CHROME_HEADLESS", "").strip() == "1"
    return parser._start_chrome(headless)


def _close_driver(driver: Any) -> None:
    parser = _collector_modules()[1]
    try:
        parser.stop_chrome(driver)
    except Exception:
        pass
    if driver is None:
        raw = os.environ.get("ZHOL_CHROME_PROFILE", "").strip()
        if raw:
            parser.release_browser(Path(raw))


def _operator_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _session_can_stay_visible(driver: Any) -> bool:
    """The operator can use this driver only when it already has a window on a display."""
    if driver is None or not visible_chrome_available():
        return False
    if getattr(driver, "_zhol_headed", None) is False:
        return False
    return True


def _pause(
    stop: _Flag,
    driver: Any,
    *,
    factory: sessionmaker[Session],
    batch_id: int,
    control: RunControl,
    sleep: Callable[[float], None],
    max_wait_seconds: float,
) -> Callable[[str], None]:
    """Wait in the Chrome that hit the check. Returning continues that same session."""

    def pause(_message: str) -> None:
        _search, parser = _collector_modules()
        if stop.forced or control._stopped:
            stop.set()
            error = parser.ManualConfirmationRequired(parser.CONFIRM_MESSAGE)
            error.page_url = _page_url(error, driver)
            raise error
        if not _session_can_stay_visible(driver):
            stop.set()
            error = parser.ManualConfirmationRequired(parser.CONFIRM_MESSAGE)
            error.page_url = _page_url(error, driver)
            raise error
        _hold_for_manual_verification(
            factory,
            batch_id,
            url=_page_url(parser.ManualConfirmationRequired(parser.CONFIRM_MESSAGE), driver),
            reason=parser.CONFIRM_MESSAGE,
            message=MESSAGE_OPEN,
        )
        status = run_visible_verification(
            factory,
            batch_id,
            driver=driver,
            control=control,
            sleep=sleep,
            max_wait_seconds=max_wait_seconds,
        )
        if status == "running":
            return
        stop.set()
        raise VerificationSettled()

    return pause


def _page_url(exc: BaseException, driver: Any) -> str:
    stored = str(getattr(exc, "page_url", "") or "")
    if stored:
        return stored
    if driver is None:
        return ""
    try:
        return str(getattr(driver, "current_url", "") or "")
    except Exception:
        return ""


def _scope_sentence(scope: str | None) -> str:
    if scope == "local":
        return " Коммерческое разрешение не подтверждено."
    return ""


def _verification_record(progress: dict[str, Any]) -> dict[str, Any]:
    raw = progress.get("manual_verification")
    return dict(raw) if isinstance(raw, dict) else {}


def _verification_passed(factory: sessionmaker[Session], batch_id: int) -> bool:
    state = _progress(factory, batch_id)
    return _verification_record(state).get("outcome") == "passed"


def _item_limit(factory: sessionmaker[Session], batch_id: int) -> int | None:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or batch.item_limit is None:
            return None
        return int(batch.item_limit)


def _remembered_delay(factory: sessionmaker[Session], batch_id: int) -> tuple[float, float] | None:
    raw = _progress(factory, batch_id).get("delay_range")
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw):
        return None
    return (float(raw[0]), float(raw[1]))


def _remember_delay(factory: sessionmaker[Session], batch_id: int, delay_range: tuple[float, float]) -> None:
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        progress["delay_range"] = [float(delay_range[0]), float(delay_range[1])]
        _assign_progress(batch, progress)


def _hold_for_manual_verification(
    factory: sessionmaker[Session],
    batch_id: int,
    *,
    url: str,
    reason: str,
    message: str | None = None,
) -> None:
    """Pause one batch. Committed companies, snapshots, and seen ids stay as they are."""
    scope = grant_scope() if active_source() == "live_2gis" else None
    message = (message or MESSAGE_PENDING) + _scope_sentence(scope)
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None or batch.status == "stopped":
            return
        progress = dict(batch.progress or {})
        progress.pop("interruption", None)
        progress["error"] = message
        progress["manual_verification"] = {
            "outcome": "pending",
            "reason": reason[:300],
            "url": url[:500],
            "browser": "closed",
            "headed": False,
            "authorization_scope": scope,
            "source_kind": active_source(),
            "message": message,
        }
        _assign_progress(batch, progress)
        batch.status = AWAITING_MANUAL_VERIFICATION
        batch.finished_at = None


def _write_verification(
    factory: sessionmaker[Session],
    batch_id: int,
    *,
    outcome: str,
    status: str,
    message: str,
    browser: str | None = None,
    headed: bool | None = None,
) -> None:
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        record = _verification_record(progress)
        record["outcome"] = outcome
        record["message"] = message
        if browser is not None:
            record["browser"] = browser
        if headed is not None:
            record["headed"] = headed
        progress["manual_verification"] = record
        progress["error"] = message
        progress.pop("admin_signal", None)
        _assign_progress(batch, progress)
        batch.status = status
        batch.finished_at = now() if status in {"stopped", "completed", "interrupted"} else None


def _admin_signal(factory: sessionmaker[Session], batch_id: int) -> str:
    state = _progress(factory, batch_id)
    signal = state.get("admin_signal")
    if signal in {"stop", VERIFY_PASSED, VERIFY_FAILED}:
        return str(signal)
    return ""


def visible_chrome_available() -> bool:
    """A container without a display cannot show the operator a window."""
    if os.environ.get("ZHOL_CHROME_CONTAINER", "").strip() != "1":
        return True
    return bool(os.environ.get("DISPLAY", "").strip())


def _verification_refusal() -> str | None:
    if browser_collection_authorized():
        return None
    if active_source() == "live_2gis":
        return MESSAGE_NOT_PERMITTED
    from zhol.agents.scout.browser_2gis import ENGINE_BLOCKER

    return ENGINE_BLOCKER


def _classify_page(driver: Any) -> str:
    """Read the open page. This does not click the check or change the page."""
    from card import _access_marker, _captcha_marker, _page_snapshot

    snapshot = _page_snapshot(driver)
    if not isinstance(snapshot, dict):
        return "unreadable"
    if _captcha_marker(snapshot):
        return "captcha"
    if _access_marker(snapshot):
        return "access_restriction"
    return "clear"


def _mark_browser(factory: sessionmaker[Session], batch_id: int, browser: str, *, headed: bool) -> None:
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        record = _verification_record(progress)
        record["browser"] = browser
        record["headed"] = headed
        if browser == "open":
            record["message"] = MESSAGE_OPEN + _scope_sentence(record.get("authorization_scope"))
            progress["error"] = record["message"]
            progress.pop("admin_signal", None)
        progress["manual_verification"] = record
        _assign_progress(batch, progress)


def _wait_for_operator(
    factory: sessionmaker[Session],
    batch_id: int,
    control: RunControl | None,
    sleep: Callable[[float], None],
    max_wait_seconds: float,
) -> str:
    deadline = time.monotonic() + max_wait_seconds
    while True:
        if control is not None and control._stopped:
            return "stop"
        signal = _admin_signal(factory, batch_id)
        if signal:
            return signal
        if time.monotonic() >= deadline:
            return "timeout"
        sleep(0.2)


def _apply_verification_signal(
    factory: sessionmaker[Session],
    batch_id: int,
    driver: Any,
    signal: str,
) -> str:
    record = _verification_record(_progress(factory, batch_id))
    scope_note = _scope_sentence(record.get("authorization_scope") if isinstance(record.get("authorization_scope"), str) else None)
    if signal == "timeout":
        _write_verification(
            factory,
            batch_id,
            outcome="pending",
            status=AWAITING_MANUAL_VERIFICATION,
            message=MESSAGE_TIMEOUT + scope_note,
            browser="closed",
        )
        return AWAITING_MANUAL_VERIFICATION
    if signal == "stop":
        _write_verification(
            factory,
            batch_id,
            outcome="failed",
            status="stopped",
            message=MESSAGE_STOP,
            browser="closed",
        )
        return "stopped"
    kind = _classify_page(driver)
    if kind == "access_restriction":
        _write_verification(
            factory,
            batch_id,
            outcome="not_permitted",
            status="stopped",
            message=MESSAGE_DENIED,
            browser="closed",
        )
        return "stopped"
    if kind == "unreadable":
        _write_verification(
            factory,
            batch_id,
            outcome="failed",
            status="stopped",
            message=MESSAGE_UNREAD,
            browser="closed",
        )
        return "stopped"
    if signal == VERIFY_FAILED:
        _write_verification(
            factory,
            batch_id,
            outcome="failed",
            status="stopped",
            message=MESSAGE_FAILED,
            browser="closed",
        )
        return "stopped"
    if kind == "captcha":
        _write_verification(
            factory,
            batch_id,
            outcome="failed",
            status="stopped",
            message=MESSAGE_STILL,
            browser="closed",
        )
        return "stopped"
    _write_verification(
        factory,
        batch_id,
        outcome="passed",
        status="running",
        message=MESSAGE_PASSED + scope_note,
        browser="open",
        headed=True,
    )
    return "running"


def run_visible_verification(
    factory: sessionmaker[Session],
    batch_id: int,
    *,
    driver: Any = None,
    driver_factory: DriverFactory | None = None,
    control: RunControl | None = None,
    sleep: Callable[[float], None] | None = None,
    max_wait_seconds: float = _VERIFY_WAIT_SECONDS,
) -> str:
    """Wait on the Chrome session that already opened the check.

    Nothing here clicks or solves a check. A driver factory is ignored:
    calling it would start a different browser and drop the cookies of this one.
    """
    if driver_factory is not None and driver is None:
        driver_factory = None
    if _status(factory, batch_id) != AWAITING_MANUAL_VERIFICATION:
        return _status(factory, batch_id)
    refusal = _verification_refusal()
    if refusal:
        _write_verification(
            factory,
            batch_id,
            outcome="not_permitted",
            status="stopped",
            message=refusal,
            browser="closed",
            headed=False,
        )
        return "stopped"
    if driver is None or not _session_can_stay_visible(driver):
        record = _verification_record(_progress(factory, batch_id))
        if record.get("browser") == "open":
            return AWAITING_MANUAL_VERIFICATION
        if not visible_chrome_available():
            message = MESSAGE_NO_DISPLAY
        elif driver is not None and getattr(driver, "_zhol_headed", None) is False:
            message = MESSAGE_HEADLESS
        else:
            message = MESSAGE_SESSION_CLOSED
        _write_verification(
            factory,
            batch_id,
            outcome="pending",
            status=AWAITING_MANUAL_VERIFICATION,
            message=message,
            browser="closed",
            headed=False,
        )
        return AWAITING_MANUAL_VERIFICATION
    if _admin_signal(factory, batch_id) == "stop" or (control is not None and control._stopped):
        _write_verification(
            factory,
            batch_id,
            outcome="failed",
            status="stopped",
            message=MESSAGE_STOP,
            browser="closed",
        )
        return "stopped"
    _mark_browser(factory, batch_id, "open", headed=True)
    try:
        parser = _collector_modules()[1]
        parser.show_owned_window(driver)
        signal = _wait_for_operator(
            factory,
            batch_id,
            control,
            sleep or _operator_sleep,
            max_wait_seconds,
        )
        return _apply_verification_signal(factory, batch_id, driver, signal)
    except Exception:
        _write_verification(
            factory,
            batch_id,
            outcome="pending",
            status=AWAITING_MANUAL_VERIFICATION,
            message=MESSAGE_WINDOW,
            browser="closed",
            headed=False,
        )
        return _status(factory, batch_id)


def _force_browser_closed(factory: sessionmaker[Session], batch_id: int) -> None:
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        record = _verification_record(progress)
        if not record:
            return
        record["browser"] = "closed"
        progress["manual_verification"] = record
        _assign_progress(batch, progress)


def _existing_ids(factory: sessionmaker[Session]) -> set[str]:
    with factory() as session:
        rows = session.scalars(select(Company.id_2gis)).all()
    return {str(item) for item in rows if item}


def _open(factory: sessionmaker[Session], *, city: str, category: str, limit: int, out: Path) -> int:
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city=city,
            category=category,
            platforms=["2gis"],
            item_limit=limit,
            source_mode=SOURCE_MODE,
            status="running",
            out_path=str(out),
            catalog_path=SOURCE_MODE,
            progress={
                "phase": "collect",
                "active_query": category,
                "seen_ids": [],
                "hits": 0,
                "duplicates": 0,
                "new_companies": 0,
                "existing_companies": 0,
                "errors": 0,
                "city_slug": "almaty",
                "rejected_ids": [],
                "metrics": _empty_metrics(),
            },
        )
        session.add(batch)
        session.flush()
        return batch.id


def _empty_metrics() -> dict[str, Any]:
    return {
        "companies_processed": 0,
        "companies_committed": 0,
        "duplicate_companies": 0,
        "snapshots_written": 0,
        "failed_records": 0,
        "persistence_ms_total": 0.0,
        "persistence_samples": 0,
        "average_persistence_ms": 0.0,
        "retries": 0,
        "browser_restarts": 0,
    }


def _metrics_of(progress: dict[str, Any]) -> dict[str, Any]:
    metrics = _empty_metrics()
    raw = progress.get("metrics")
    if isinstance(raw, dict):
        for key in metrics:
            if key in raw:
                metrics[key] = raw[key]
    return metrics


def _finish_metrics(metrics: dict[str, Any]) -> None:
    samples = int(metrics.get("persistence_samples") or 0)
    total = float(metrics.get("persistence_ms_total") or 0)
    metrics["average_persistence_ms"] = (total / samples) if samples else 0.0


def _assign_progress(batch: CollectionBatch, progress: dict[str, Any]) -> None:
    batch.progress = progress
    flag_modified(batch, "progress")


def _lock_batch(session: Session, batch_id: int) -> CollectionBatch | None:
    return session.execute(
        select(CollectionBatch).where(CollectionBatch.id == batch_id).with_for_update()
    ).scalar_one_or_none()


def _collection_key(batch_id: int, firm_id: str) -> str:
    return f"{SOURCE_MODE}:{batch_id}:{firm_id}"


def _note_job_retry(factory: sessionmaker[Session], batch_id: int, retries: int) -> None:
    """Store how many times the queue job has been started again. The first claim is not a retry."""
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        metrics = _metrics_of(progress)
        metrics["retries"] = max(int(metrics["retries"]), int(retries))
        progress["metrics"] = metrics
        _assign_progress(batch, progress)


def _retryable_ids(factory: sessionmaker[Session], batch_id: int) -> set[str]:
    with factory() as session:
        rows = session.scalars(
            select(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == batch_id,
                CollectionBatchItem.status.in_(("pending", "in_progress")),
            )
        ).all()
    found: set[str] = set()
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        if detail.get("retryable") is not True:
            continue
        if detail.get("classification") not in TEMPORARY_FAILURES:
            continue
        if int(row.attempts or 0) >= MAX_CARD_ATTEMPTS:
            continue
        found.add(str(row.id_2gis))
    return found


def _note_browser_restart(factory: sessionmaker[Session], batch_id: int) -> None:
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        metrics = _metrics_of(progress)
        metrics["browser_restarts"] = int(metrics["browser_restarts"]) + 1
        progress["metrics"] = metrics
        _assign_progress(batch, progress)


def _commit_firm(factory: sessionmaker[Session], batch_id: int, firm: BrowserFirm) -> str:
    """Commit one company, its snapshot, and the checkpoint together."""
    started = time.perf_counter()
    for attempt in range(2):
        try:
            with factory() as session, session.begin():
                return _commit_firm_locked(session, batch_id, firm, started)
        except IntegrityError:
            if attempt == 1:
                raise
    raise RuntimeError("company commit did not finish")


def _commit_firm_locked(
    session: Session,
    batch_id: int,
    firm: BrowserFirm,
    started: float,
) -> str:
    batch = _lock_batch(session, batch_id)
    if batch is None:
        return "duplicate"
    progress = dict(batch.progress or {})
    seen = {str(item) for item in progress.get("seen_ids") or []}
    item = session.scalar(
        select(CollectionBatchItem).where(
            CollectionBatchItem.batch_id == batch_id,
            CollectionBatchItem.id_2gis == firm.firm_id,
        )
    )
    metrics = _metrics_of(progress)
    elapsed = (time.perf_counter() - started) * 1000
    metrics["persistence_ms_total"] = float(metrics["persistence_ms_total"]) + elapsed
    metrics["persistence_samples"] = int(metrics["persistence_samples"]) + 1
    if item is not None and item.status == "completed":
        seen.add(firm.firm_id)
        progress["seen_ids"] = sorted(seen)
        progress["hits"] = len(seen)
        metrics["duplicate_companies"] = int(metrics["duplicate_companies"]) + 1
        metrics["companies_processed"] = int(metrics["companies_processed"]) + 1
        _finish_metrics(metrics)
        progress["metrics"] = metrics
        _assign_progress(batch, progress)
        return "duplicate"
    if item is None:
        metrics["companies_processed"] = int(metrics["companies_processed"]) + 1
    company = session.scalar(select(Company).where(Company.id_2gis == firm.firm_id))
    created = company is None
    if company is None:
        company = Company(
            id_2gis=firm.firm_id,
            name=firm.name,
            city=firm.city or batch.city,
            niche=firm.query,
            whatsapp=firm.whatsapp,
            instagram=firm.instagram,
            source=COMPANY_SOURCE,
        )
        session.add(company)
        session.flush()
    wrote = _snapshot_once(session, batch, company, firm)
    if item is None:
        _link_company(session, batch_id, company, firm.query, None if created else "already_present")
    else:
        item.status = "completed"
        item.company_id = company.id
        item.detail = {
            "name": company.name,
            "city": company.city,
            "category": firm.query,
            "reason": None if created else "already_present",
        }
        flag_modified(item, "detail")
    seen.add(firm.firm_id)
    rejected = {str(value) for value in progress.get("rejected_ids") or []}
    rejected.discard(firm.firm_id)
    progress["rejected_ids"] = sorted(rejected)
    progress["seen_ids"] = sorted(seen)
    progress["hits"] = len(seen)
    if created:
        progress["new_companies"] = int(progress.get("new_companies") or 0) + 1
    else:
        progress["existing_companies"] = int(progress.get("existing_companies") or 0) + 1
        metrics["duplicate_companies"] = int(metrics["duplicate_companies"]) + 1
    metrics["companies_committed"] = int(metrics["companies_committed"]) + 1
    if wrote:
        metrics["snapshots_written"] = int(metrics["snapshots_written"]) + 1
    _finish_metrics(metrics)
    progress["metrics"] = metrics
    _assign_progress(batch, progress)
    return "new" if created else "existing"


def _snapshot_once(session: Session, batch: CollectionBatch, company: Company, firm: BrowserFirm) -> bool:
    key = _collection_key(batch.id, firm.firm_id)
    if find_reusable(session, company.id, "2gis", key) is not None:
        return False
    _snapshot(session, batch, company, firm)
    return True


def _commit_failure(
    factory: sessionmaker[Session],
    batch_id: int,
    firm_id: str,
    reason: str,
    *,
    kind: str = "",
) -> None:
    """Record one card failure in the same transaction as its checkpoint.

    A temporary class stays pending until the attempt limit. It is not added to
    rejected_ids, and seen_ids moves only when a card is committed.
    """
    classification = classify_failure(reason, kind)
    if classification not in TEMPORARY_FAILURES and classification not in PERMANENT_FAILURES:
        classification = "invalid_card"
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None:
            return
        progress = dict(batch.progress or {})
        seen = {str(item) for item in progress.get("seen_ids") or []}
        if firm_id in seen:
            return
        item = session.scalar(
            select(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == batch_id,
                CollectionBatchItem.id_2gis == firm_id,
            )
        )
        if item is not None and item.status == "completed":
            return
        if item is not None and item.status == "failed":
            return
        attempts = int(item.attempts or 0) + 1 if item is not None else 1
        permanent = classification not in TEMPORARY_FAILURES or attempts >= MAX_CARD_ATTEMPTS
        detail = {
            "reason": reason[:300],
            "classification": classification,
            "retryable": not permanent,
            "attempts": attempts,
        }
        created = item is None
        if created:
            session.add(
                CollectionBatchItem(
                    batch_id=batch_id,
                    id_2gis=firm_id,
                    status="failed" if permanent else "pending",
                    attempts=attempts,
                    detail=detail,
                )
            )
        else:
            item.status = "failed" if permanent else "pending"
            item.attempts = attempts
            item.detail = detail
            flag_modified(item, "detail")
        rejected = {str(value) for value in progress.get("rejected_ids") or []}
        metrics = _metrics_of(progress)
        if created:
            metrics["companies_processed"] = int(metrics["companies_processed"]) + 1
        if permanent:
            if firm_id not in rejected:
                rejected.add(firm_id)
                metrics["failed_records"] = int(metrics["failed_records"]) + 1
        else:
            rejected.discard(firm_id)
        progress["rejected_ids"] = sorted(rejected)
        progress["seen_ids"] = sorted(seen)
        progress["metrics"] = metrics
        _assign_progress(batch, progress)


def _store_firm(factory: sessionmaker[Session], batch_id: int, firm: BrowserFirm) -> str:
    return _commit_firm(factory, batch_id, firm)


def _link_existing(factory: sessionmaker[Session], batch_id: int, firm_id: str, query: str) -> bool:
    with factory() as session, session.begin():
        company = session.scalar(select(Company).where(Company.id_2gis == firm_id))
        if company is None:
            return False
        before = session.scalar(
            select(CollectionBatchItem.id).where(
                CollectionBatchItem.batch_id == batch_id,
                CollectionBatchItem.id_2gis == firm_id,
            )
        )
        _link_company(session, batch_id, company, query, "already_present")
        return before is None


def _link_company(session: Session, batch_id: int, company: Company, query: str, reason: str | None) -> None:
    existing_item = session.scalar(
        select(CollectionBatchItem).where(
            CollectionBatchItem.batch_id == batch_id,
            CollectionBatchItem.id_2gis == company.id_2gis,
        )
    )
    if existing_item is not None:
        return
    session.add(
        CollectionBatchItem(
            batch_id=batch_id,
            id_2gis=company.id_2gis,
            company_id=company.id,
            status="completed",
            detail={"name": company.name, "city": company.city, "category": query, "reason": reason},
        )
    )


def _delays(delay_range: tuple[float, float] | None) -> tuple[tuple[float, float], Any]:
    if delay_range == (0.0, 0.0):
        return (0.0, 0.0), None
    if delay_range is None:
        return (0.0, 0.0), wait_turn
    return delay_range, None


def _with_screenshot(firm: BrowserFirm, path: str) -> BrowserFirm:
    return BrowserFirm(
        firm_id=firm.firm_id,
        name=firm.name,
        city=firm.city,
        address=firm.address,
        address_reason=firm.address_reason,
        phones=firm.phones,
        phone_display=firm.phone_display,
        phones_reason=firm.phones_reason,
        whatsapp=firm.whatsapp,
        whatsapp_reason=firm.whatsapp_reason,
        instagram=firm.instagram,
        instagram_reason=firm.instagram_reason,
        url=firm.url,
        query=firm.query,
        collected_at=firm.collected_at,
        screenshot_path=path,
        shown_city=firm.shown_city,
        category=firm.category,
        category_checked=firm.category_checked,
    )


def _capture(driver: Any, run_dir: Path, firm_id: str) -> str | None:
    save = getattr(driver, "save_screenshot", None)
    if not callable(save):
        return None
    folder = Path(run_dir) / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{firm_id}.png"
    try:
        save(str(path))
    except Exception:
        return None
    if not path.is_file():
        return None
    return str(path)


def _observed(url: str, seen_at: str, screenshot: str | None, *, reason: str | None, display: tuple[str, ...] = ()) -> dict[str, Any]:
    source: dict[str, Any] = {
        "url": url,
        "seen_at": seen_at,
        "read_by": "code",
        "source": "2gis.kz",
        "screenshot": screenshot,
    }
    if reason:
        source["reason"] = reason
    if display:
        source["display"] = list(display)
    return source


def _put(fields: dict[str, Any], sources: dict[str, Any], key: str, value: Any, source: dict[str, Any], reason: str | None) -> None:
    if reason == "not_shown":
        fields[key] = None
        return
    fields[key] = value
    sources[key] = source


def _snapshot(session: Session, batch: CollectionBatch, company: Company, firm: BrowserFirm) -> CardSnapshot:
    seen_at = firm.collected_at or ""
    fields = {key: None for key in field_keys("2gis")}
    sources: dict[str, Any] = {}
    shot = firm.screenshot_path
    _put(fields, sources, "name", firm.name, _observed(firm.url, seen_at, shot, reason=None), None)
    fields["address"] = firm.address if firm.address_reason is None else None
    sources["address"] = _observed(firm.url, seen_at, shot, reason=firm.address_reason)
    _put(
        fields,
        sources,
        "city",
        firm.shown_city,
        _observed(firm.url, seen_at, shot, reason=None if firm.shown_city else "not_shown"),
        None if firm.shown_city else "not_shown",
    )
    _put(
        fields,
        sources,
        "phones",
        list(firm.phones) if firm.phones_reason is None else None,
        _observed(firm.url, seen_at, shot, reason=firm.phones_reason, display=firm.phone_display),
        firm.phones_reason,
    )
    _put(
        fields,
        sources,
        "has_phone",
        True if firm.phones_reason is None else (False if firm.phones_reason == "empty" else None),
        _observed(firm.url, seen_at, shot, reason=firm.phones_reason),
        firm.phones_reason,
    )
    _put(
        fields,
        sources,
        "whatsapp",
        firm.whatsapp,
        _observed(firm.url, seen_at, shot, reason=firm.whatsapp_reason),
        firm.whatsapp_reason,
    )
    _put(
        fields,
        sources,
        "has_whatsapp",
        firm.whatsapp is not None if firm.whatsapp_reason is None else (False if firm.whatsapp_reason == "empty" else None),
        _observed(firm.url, seen_at, shot, reason=firm.whatsapp_reason),
        firm.whatsapp_reason,
    )
    _put(
        fields,
        sources,
        "instagram",
        firm.instagram,
        _observed(firm.url, seen_at, shot, reason=firm.instagram_reason),
        firm.instagram_reason,
    )
    _put(
        fields,
        sources,
        "has_instagram",
        firm.instagram is not None if firm.instagram_reason is None else (False if firm.instagram_reason == "empty" else None),
        _observed(firm.url, seen_at, shot, reason=firm.instagram_reason),
        firm.instagram_reason,
    )
    if firm.category_checked:
        fields["category"] = firm.category
        category_source = _observed(
            firm.url,
            seen_at,
            shot,
            reason=None if firm.category else "not_shown",
        )
        category_source["checked"] = True
        sources["category"] = category_source
    remember_platform_id(session, company.id, "2gis", firm.firm_id, firm.url)
    snapshot = insert_snapshot(
        session,
        company_id=company.id,
        platform="2gis",
        purpose="prospect",
        match_status="found",
        url=firm.url,
        fields=fields,
        field_sources=sources,
        screenshot_path=firm.screenshot_path,
        collection_key=f"{SOURCE_MODE}:{batch.id}:{firm.firm_id}",
        page_file=None,
        kind=CARD_KIND,
        raw_path=None,
    )
    parsed = _parsed_time(firm.collected_at)
    if parsed is not None:
        snapshot.collected_at = parsed
    remember_search_query(snapshot, firm.query)
    from zhol.agents.scout.localities import region_of

    remember_collection_context(snapshot, city=batch.city, region=region_of(batch.city))
    return snapshot


def _failed_item(factory: sessionmaker[Session], batch_id: int, firm_id: str, reason: str) -> None:
    with factory() as session, session.begin():
        existing = session.scalar(
            select(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == batch_id,
                CollectionBatchItem.id_2gis == firm_id,
            )
        )
        if existing is not None:
            return
        session.add(
            CollectionBatchItem(
                batch_id=batch_id,
                id_2gis=firm_id,
                status="failed",
                detail={"reason": reason[:300]},
            )
        )


def _error_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "branch_id": str(item.get("branch_id") or ""),
                "message": str(item.get("message") or ""),
            }
        )
    return rows


def _parsed_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed
