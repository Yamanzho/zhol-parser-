import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse

from browser_ops import BrowserTimeout
from browser_ops import WebDriverException
from browser_ops import assert_local_destination
from browser_ops import click_css
from browser_ops import count_css
from browser_ops import eval_js
from browser_ops import is_browser_crash
from browser_ops import open_url
from browser_ops import wait_js
from browser_ops import wait_visible

from card import CARD_TIMEOUT_MS
from card import ExtractionError
from card import NAV_TIMEOUT_MS
from card import read_company

log = logging.getLogger("gis.search")

DEFAULT_DELAY_RANGE = (0.0, 0.3)
# Предел страниц — только защита от бесконечного цикла, не лимит бизнесов.
MAX_SEARCH_PAGES = 1000
EMPTY_PAGE_LIMIT = 3
SEARCH_READY_TIMEOUT_MS = 15_000
HOST = "https://2gis.kz"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})


def search_host() -> str:
    """Live site, or a loopback test origin. A public override is ignored."""
    raw = os.environ.get("ZHOL_BROWSER_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return HOST
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS or "2gis." in host:
        return HOST
    return raw

STOP_LIMIT = "Достигнут отладочный лимит уникальных карточек"
STOP_END = "Подтверждён конец выдачи"
STOP_REPEAT = "Повторяющаяся выдача без новых идентификаторов"
STOP_EMPTY = "Три страницы подряд без новых карточек, конец выдачи не подтверждён"
STOP_PAGE_CAP = "Достигнут предел страниц поиска, конец выдачи не подтверждён"
STOP_INTERRUPTED = "Сбор ссылок прерван"

# Выдача 2ГИС — нумерованные страницы /{город}/search/{запрос}/page/N.
# Ссылки карточек в списке: /{город}/firm/{id}?stat=...
# Рекламные баннеры и блоки «филиалы» ведут не на /{город}/firm/{id}.
SEARCH_DOM_JS = r"""
function gisCollectSearch(slug) {
  const firmRe = new RegExp("/" + slug + "/firm/(\\d+)(?:[/?#]|$)");
  const pageRe = new RegExp("/" + slug + "/search/[^?#]*/page/(\\d+)");
  const heading = document.querySelector("h1");
  let root = heading;
  while (root) {
    if (root.querySelector('a[href*="/' + slug + '/firm/"]')) break;
    root = root.parentElement;
  }
  const scope = root || document;
  const items = [];
  const seen = new Set();
  for (const link of scope.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href") || "";
    const match = href.match(firmRe);
    if (!match || seen.has(match[1])) continue;
    let node = link;
    let row = null;
    while (node && node !== scope.parentElement) {
      const ids = new Set();
      for (const candidate of node.querySelectorAll("a[href]")) {
        const found = (candidate.getAttribute("href") || "").match(firmRe);
        if (found) ids.add(found[1]);
      }
      if (ids.size === 1) row = node;
      else if (ids.size > 1 && row) break;
      node = node.parentElement;
    }
    const text = ((row && row.innerText) || link.innerText || "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (text.startsWith("Реклама")) continue;
    seen.add(match[1]);
    items.push({
      branchId: match[1],
      listName: (link.innerText || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim(),
    });
  }
  const pageNumbers = new Set();
  for (const link of document.querySelectorAll("a[href]")) {
    const found = (link.getAttribute("href") || "").match(pageRe);
    if (found) pageNumbers.add(Number(found[1]));
  }
  return {
    items,
    pageNumbers: [...pageNumbers],
    heading: heading ? heading.innerText.replace(/\s+/g, " ").trim() : "",
  };
}
"""


def runs_dir() -> Path:
    return Path("logs") / "runs"


def search_page_url(slug: str, query: str, page_number: int) -> str:
    encoded = quote(query, safe="")
    path = f"/{slug}/search/{encoded}"
    if page_number > 1:
        path += f"/page/{page_number}"
    return search_host() + path


def canonical_firm_url(slug: str, branch_id: str) -> str:
    return f"{search_host()}/{slug}/firm/{branch_id}"


def page_number_from_url(url: str) -> int:
    match = re.search(r"/page/(\d+)(?:[/?#]|$)", urlparse(url).path)
    return int(match.group(1)) if match else 1


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


def _cities_differ(actual: str | None, requested: str) -> bool:
    if not actual:
        return False
    return actual.casefold() != requested.casefold()


def _install_search(driver) -> None:
    eval_js(driver, SEARCH_DOM_JS + "\nwindow.gisCollectSearch = gisCollectSearch;")


def _read_search(driver, slug: str) -> dict:
    _install_search(driver)
    return eval_js(driver, "return window.gisCollectSearch(arguments[0]);", slug)


def _wait_for_search(driver, slug: str, pause) -> None:
    for attempt in range(2):
        try:
            wait_visible(driver, "h1", CARD_TIMEOUT_MS)
            wait_js(
                driver,
                """
                return !!document.querySelector(
                  'a[href*="/' + arguments[0] + '/firm/"]'
                );
                """,
                SEARCH_READY_TIMEOUT_MS,
                slug,
            )
            return
        except BrowserTimeout:
            if attempt == 1:
                return
            print("Выдача не появилась. Если на странице капча, пройдите её в окне браузера.")
            pause("После прохождения нажмите Enter, чтобы повторить загрузку выдачи.\n")
            _install_search(driver)


def _open_results_page(driver, url: str, page_number: int, slug: str, pause) -> None:
    open_url(driver, url, NAV_TIMEOUT_MS)
    _wait_for_search(driver, slug, pause)
    if page_number == 1 or page_number_from_url(driver.current_url) == page_number:
        return
    selector = f'a[href$="/page/{page_number}"]'
    if count_css(driver, selector) == 0:
        return
    click_css(driver, selector, CARD_TIMEOUT_MS)
    assert_local_destination(driver)
    try:
        wait_js(
            driver,
            "return location.pathname.includes('/page/' + arguments[0]);",
            SEARCH_READY_TIMEOUT_MS,
            str(page_number),
        )
    except BrowserTimeout:
        return
    _wait_for_search(driver, slug, pause)


def _note(quiet: bool, message: str) -> None:
    log.info("%s", message)
    if not quiet:
        print(message)


def collect_queue(
    driver,
    slug: str,
    query: str,
    limit: int | None,
    pause,
    should_stop,
    quiet: bool = False,
    known_ids: set[str] | None = None,
    pace=None,
    reserved_ids: set[str] | None = None,
) -> dict:
    """Собрать уникальные карточки запроса в пределах лимита.

    Конец подтверждается, только если на странице нет ссылки на следующую.
    Страница без новых branch_id сама по себе сбор не заканчивает: три такие
    страницы подряд останавливают запрос без подтверждения конца.
    Уже известные branch_id не занимают место в лимите и не открываются повторно.
    Карточки из reserved_ids открываются снова и не занимают новый слот лимита.
    Страница, где есть только уже известные карточки, концом выдачи не считается.
    """
    queue: list[dict] = []
    seen: set[str] = set()
    skipped_ids: list[str] = []
    signatures: list[tuple[str, ...]] = []
    pages_loaded = 0
    empty_streak = 0
    reason = STOP_PAGE_CAP
    complete = False
    reserved = {str(item) for item in (reserved_ids or ())}
    queued_reserved: set[str] = set()
    ordinary = 0

    def limit_reached() -> bool:
        if limit is None:
            return False
        return ordinary >= limit and reserved <= queued_reserved

    def packed(stop_reason: str, done: bool, *, interrupted: bool = False) -> dict:
        body = {
            "items": queue,
            "stop_reason": stop_reason,
            "complete": done,
            "pages_loaded": pages_loaded,
            "skipped_known": len(skipped_ids),
            "skipped_known_ids": list(skipped_ids),
        }
        if interrupted:
            body["interrupted"] = True
        return body

    if limit is not None and limit <= 0 and not reserved:
        return packed(STOP_LIMIT, False)

    for page_number in range(1, MAX_SEARCH_PAGES + 1):
        if should_stop():
            return packed(STOP_INTERRUPTED, False, interrupted=True)
        if pace is not None:
            pace()
            if should_stop():
                return packed(STOP_INTERRUPTED, False, interrupted=True)
        url = search_page_url(slug, query, page_number)
        _note(quiet, f"Страница выдачи {page_number}: {url}")
        try:
            _open_results_page(driver, url, page_number, slug, pause)
        except BrowserTimeout as exc:
            reason = f"Не удалось открыть страницу выдачи {page_number}: {exc}"
            break
        except ExtractionError as exc:
            reason = f"Не удалось открыть страницу выдачи {page_number}: {exc}"
            break

        actual_page = page_number_from_url(driver.current_url)
        data = _read_search(driver, slug)
        items = data.get("items") or []
        signature = tuple(item["branchId"] for item in items)
        if actual_page != page_number:
            reason = f"Сайт открыл страницу {actual_page} вместо {page_number}"
            break
        if signature and signature in signatures:
            reason = STOP_REPEAT
            break
        signatures.append(signature)
        pages_loaded += 1

        kept = 0
        known_on_page = 0
        for item in items:
            branch_id = str(item["branchId"])
            if branch_id in seen:
                continue
            seen.add(branch_id)
            if branch_id in reserved:
                queue.append(
                    {
                        "branch_id": branch_id,
                        "url": canonical_firm_url(slug, branch_id),
                        "list_name": item.get("listName") or None,
                    }
                )
                queued_reserved.add(branch_id)
                kept += 1
                if limit_reached():
                    reason = STOP_LIMIT
                    return packed(reason, False)
                continue
            if known_ids is not None and branch_id in known_ids:
                skipped_ids.append(branch_id)
                known_on_page += 1
                continue
            if limit is not None and ordinary >= limit:
                continue
            queue.append(
                {
                    "branch_id": branch_id,
                    "url": canonical_firm_url(slug, branch_id),
                    "list_name": item.get("listName") or None,
                }
            )
            ordinary += 1
            kept += 1
            if limit_reached():
                reason = STOP_LIMIT
                log.info(
                    "query=%s debug limit=%s pages=%s",
                    query,
                    limit,
                    pages_loaded,
                )
                return packed(reason, False)

        next_page = page_number + 1
        has_next = next_page in set(data.get("pageNumbers") or [])
        if not has_next:
            reason = STOP_END
            complete = True
            break
        if reserved - queued_reserved:
            empty_streak = 0
        elif kept + known_on_page == 0:
            empty_streak += 1
            log.info(
                "query=%s page=%s new=0 empty_streak=%s",
                query,
                page_number,
                empty_streak,
            )
            if empty_streak >= EMPTY_PAGE_LIMIT:
                reason = STOP_EMPTY
                complete = False
                break
        else:
            empty_streak = 0
    else:
        reason = STOP_PAGE_CAP
        complete = False

    log.info(
        "query=%s stop=%s complete=%s pages=%s urls=%s",
        query,
        reason,
        complete,
        pages_loaded,
        len(queue),
    )
    return packed(reason, complete)


def _empty_manifest(run_id: str, city: str, slug: str, query: str, limit: int | None) -> dict:
    return {
        "run_id": run_id,
        "city": city,
        "city_slug": slug,
        "query": query,
        "limit": limit,
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "link_collection_stop_reason": None,
        "link_collection_complete": False,
        "pages_loaded": 0,
        "counters": {
            "unique_links": 0,
            "attempted": 0,
            "success": 0,
            "duplicates": 0,
            "skipped": 0,
            "errors": 0,
        },
    }


def _print_counters(counters: dict, quiet: bool) -> None:
    _note(
        quiet,
        "Успешно: {success}, дубликаты: {duplicates}, пропущено: {skipped}, ошибок: {errors}".format(
            **counters
        ),
    )


def _interruptible_sleep(seconds: float, should_stop) -> None:
    remaining = seconds
    while remaining > 0:
        if should_stop():
            return
        step = min(0.25, remaining)
        time.sleep(step)
        remaining -= step


def _delay_bounds(delay_range: tuple[float, float] | None) -> tuple[float, float]:
    low, high = DEFAULT_DELAY_RANGE if not delay_range else delay_range
    low, high = float(low), float(high)
    if high < low:
        low, high = high, low
    return low, high


def run_search(
    page,
    city: str,
    slug: str,
    query: str,
    limit: int | None,
    pause,
    stop=None,
    on_progress=None,
    on_record=None,
    delay_range: tuple[float, float] | None = None,
    known_ids: set[str] | None = None,
    quiet: bool = False,
    run_dir: Path | None = None,
    pace=None,
    refresh_ids: set[str] | None = None,
    on_error=None,
    reserved_ids: set[str] | None = None,
) -> dict:
    run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    if run_dir is None:
        run_dir = runs_dir() / run_id
    else:
        run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = _empty_manifest(run_id, city, slug, query, limit)
    manifest_path = run_dir / "manifest.json"
    queue_path = run_dir / "queue.json"
    results_path = run_dir / "results.jsonl"
    errors_path = run_dir / "errors.jsonl"
    results_path.touch()
    errors_path.touch()
    _write_json(manifest_path, manifest)
    status = "completed"

    delay_low, delay_high = _delay_bounds(delay_range)

    def should_stop() -> bool:
        return bool(stop and stop.is_set())

    def pause_before_next(company: str) -> None:
        if should_stop():
            return
        if pace is not None:
            pace()
            return
        seconds = random.uniform(delay_low, delay_high)
        if seconds < 0.001:
            return
        _note(quiet, "Пауза перед следующей карточкой...")
        notify("Пауза перед следующей карточкой...", company)
        _interruptible_sleep(seconds, should_stop)

    def notify(status: str, company: str = "") -> None:
        if on_progress is None:
            return
        counters = manifest["counters"]
        try:
            on_progress(
                {
                    "processed": counters["attempted"],
                    "successful": counters["success"],
                    "skipped": counters["skipped"],
                    "duplicates": counters["duplicates"],
                    "errors": counters["errors"],
                    "limit": limit,
                    "company": company,
                    "status": status,
                }
            )
        except Exception:
            pass

    def report_error(
        branch_id: str,
        url: str,
        message: str,
        *,
        kind: str = "extraction",
        record: dict | None = None,
        count_attempt: bool = True,
    ) -> None:
        if count_attempt:
            manifest["counters"]["attempted"] += 1
        manifest["counters"]["errors"] += 1
        body = {
            "branch_id": branch_id,
            "url": url,
            "kind": kind,
            "message": message,
            "collected_at": _now(),
        }
        if record is not None:
            body["record"] = record
        _append_jsonl(errors_path, body)
        if on_error is not None:
            on_error({"branch_id": str(branch_id), "url": url, "kind": kind, "message": message})

    try:
        reserved = {str(item) for item in (reserved_ids or ())}
        collected = collect_queue(
            page,
            slug,
            query,
            limit,
            pause,
            should_stop,
            quiet,
            known_ids,
            pace,
            reserved_ids=reserved,
        )
        if collected.get("interrupted"):
            status = "interrupted"
        queue = collected["items"]
        manifest["link_collection_stop_reason"] = collected["stop_reason"]
        manifest["link_collection_complete"] = collected["complete"]
        manifest["pages_loaded"] = collected["pages_loaded"]
        manifest["counters"]["unique_links"] = len(queue)
        manifest["counters"]["duplicates"] += int(collected.get("skipped_known") or 0)
        _write_json(queue_path, {"items": queue})
        _write_json(manifest_path, manifest)
        _note(
            quiet,
            f"Очередь: {len(queue)}. Причина остановки сбора ссылок: {collected['stop_reason']}. "
            f"Конец выдачи подтверждён: {'да' if collected['complete'] else 'нет'}.",
        )
        notify(
            f"Очередь: {len(queue)}. {collected['stop_reason']}"
        )

        total = len(queue)
        if status == "interrupted":
            total = 0
        for index, item in enumerate(queue, start=1):
            if status == "interrupted":
                break
            if should_stop():
                status = "interrupted"
                break
            label = item.get("list_name") or item["branch_id"]
            if known_ids is not None and item["branch_id"] in known_ids:
                manifest["counters"]["duplicates"] += 1
                _note(quiet, f"Дубликат {item['branch_id']}")
                _write_json(manifest_path, manifest)
                continue
            _note(quiet, f"Карточка {index}/{total}: {label}")
            notify(f"Карточка {index}/{total}", label)
            try:
                record = read_company(page, item["url"], pause)
            except (ExtractionError, WebDriverException) as exc:
                if is_browser_crash(exc):
                    log.exception("Сессия Chrome оборвалась на карточке %s", item["branch_id"])
                    raise
                report_error(item["branch_id"], item["url"], str(exc))
                log.warning("Ошибка карточки %s: %s", item["branch_id"], exc)
                _note(quiet, f"Ошибка карточки {item['branch_id']}: {exc}")
                _print_counters(manifest["counters"], quiet)
                notify(f"Ошибка карточки {item['branch_id']}", label)
                _write_json(manifest_path, manifest)
                if index < total:
                    pause_before_next(label)
                    if should_stop():
                        status = "interrupted"
                        break
                continue

            record["branch_id"] = str(record.get("branch_id") or item["branch_id"])
            record["search_query"] = query
            record["requested_city"] = city
            record["collected_at"] = _now()
            manifest["counters"]["attempted"] += 1
            actual_city = record.get("city")
            if _cities_differ(actual_city, city):
                manifest["counters"]["skipped"] += 1
                _append_jsonl(
                    errors_path,
                    {
                        "branch_id": record["branch_id"],
                        "url": item["url"],
                        "kind": "skipped",
                        "message": (
                            f"Город карточки «{actual_city}» не совпадает с запрошенным «{city}»"
                        ),
                        "city": actual_city,
                        "collected_at": record["collected_at"],
                    },
                )
                _note(quiet, f"Пропуск {record['branch_id']}: город «{actual_city}»")
            else:
                field_errors = (record.get("diagnostics") or {}).get("errors") or []
                if field_errors:
                    message = "; ".join(error["message"] for error in field_errors)
                    report_error(
                        record["branch_id"],
                        item["url"],
                        message,
                        record=record,
                        count_attempt=False,
                    )
                    _note(quiet, f"Ошибка полей {record.get('name') or record['branch_id']}")
                else:
                    decision = on_record(record) if on_record is not None else True
                    if decision == "filtered":
                        manifest["counters"]["skipped"] += 1
                        _note(quiet, f"Пропуск по фильтру: {record.get('name') or record['branch_id']}")
                    elif decision == "invalid":
                        manifest["counters"]["errors"] += 1
                        _append_jsonl(
                            errors_path,
                            {
                                "branch_id": record["branch_id"],
                                "url": item["url"],
                                "kind": "extraction",
                                "message": "Нет идентификатора филиала или названия",
                                "collected_at": record["collected_at"],
                            },
                        )
                        _note(quiet, f"Ошибка полей {record.get('name') or record['branch_id']}")
                    elif decision is False:
                        manifest["counters"]["duplicates"] += 1
                        _note(quiet, f"Дубликат {record['branch_id']}")
                    else:
                        manifest["counters"]["success"] += 1
                        _append_jsonl(results_path, record)
                        _note(quiet, f"Сохранено: {record.get('name') or record['branch_id']}")
            _print_counters(manifest["counters"], quiet)
            notify(
                "Карточка обработана",
                record.get("name") or record["branch_id"],
            )
            _write_json(manifest_path, manifest)
            if index < total:
                pause_before_next(record.get("name") or record["branch_id"])
                if should_stop():
                    status = "interrupted"
                    break
        refresh = set(refresh_ids or ())
        # Refresh stays inside the same attempt budget. A known card is not a free extra open.
        refresh_budget = None if limit is None else int(limit) + len(reserved)
        for branch_id in collected.get("skipped_known_ids") or []:
            if refresh_budget is not None and manifest["counters"]["attempted"] >= refresh_budget:
                break
            if status == "interrupted" or should_stop() or branch_id not in refresh:
                continue
            url = canonical_firm_url(slug, branch_id)
            if pace is not None:
                pace()
            elif delay_high > 0:
                pause_before_next(branch_id)
            try:
                record = read_company(page, url, pause)
            except (ExtractionError, WebDriverException) as exc:
                if is_browser_crash(exc):
                    raise
                report_error(branch_id, url, str(exc))
                continue
            record["branch_id"] = str(record.get("branch_id") or branch_id)
            record["search_query"] = query
            record["requested_city"] = city
            record["collected_at"] = _now()
            manifest["counters"]["attempted"] += 1
            field_errors = (record.get("diagnostics") or {}).get("errors") or []
            if field_errors:
                report_error(
                    record["branch_id"],
                    url,
                    "; ".join(error["message"] for error in field_errors),
                    record=record,
                    count_attempt=False,
                )
                continue
            decision = on_record(record) if on_record is not None else True
            if decision == "invalid":
                manifest["counters"]["errors"] += 1
                continue
            if decision is False:
                manifest["counters"]["duplicates"] += 1
                continue
            manifest["counters"]["success"] += 1
            _append_jsonl(results_path, record)
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except Exception:
        if status == "completed":
            status = "failed"
        raise
    finally:
        manifest["status"] = status
        manifest["finished_at"] = _now()
        _write_json(manifest_path, manifest)
        if not queue_path.exists():
            _write_json(queue_path, {"items": []})
        final_status = {
            "completed": "Готово",
            "interrupted": "Остановлено",
            "failed": "Ошибка сбора",
        }.get(status, status)
        notify(final_status)
    counters = manifest["counters"]
    return {
        "status": status,
        "link_collection_complete": bool(manifest["link_collection_complete"]),
        "stop_reason": manifest["link_collection_stop_reason"],
        "pages_loaded": manifest["pages_loaded"] or 0,
        "unique_links": counters["unique_links"],
        "attempted": counters["attempted"],
        "success": counters["success"],
        "duplicates": counters["duplicates"],
        "errors": counters["errors"],
        "skipped": counters["skipped"],
        "skipped_known_ids": list(collected.get("skipped_known_ids") or []),
        "run_dir": str(run_dir),
    }
