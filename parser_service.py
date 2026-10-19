"""Один поиск 2ГИС.

Карточки читает search.run_search. Excel пишется, если передан output_path.
По умолчанию город Алматы. Другой город принимается только вместе со slug,
который уже задан в этом сборщике.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from browser_ops import is_browser_crash
from excel_writer import ExcelWriteError, ExcelWriter
from search import run_search as collect_search

log = logging.getLogger("gis.parser")

CITY_NAME = "Алматы"
CITY_SLUG = "almaty"
# Slug берётся только отсюда. Новый город не выдумывается.
KNOWN_CITY_SLUGS = {"алматы": CITY_SLUG}
CONFIRM_MESSAGE = "2GIS требует подтверждение в браузере."
CHROMIUM_MESSAGE = "Для сбора нужен установленный Google Chrome."
DEFAULT_DELAY_RANGE = (0.0, 0.3)
LOG_NAME = "collector.log"
MAX_BROWSER_RESTARTS_PER_QUERY = 3
CHROME_PROFILE = Path.home() / "Library" / "Application Support" / "Almaty2GISCollector" / "ChromeProfile"
# A healthy headless start finishes in a few seconds. This is not the 120s driver read timeout.
BROWSER_START_TIMEOUT = 30
BROWSER_QUIT_TIMEOUT = 5


class ManualConfirmationRequired(Exception):
    """Headless-сбор остановился: сайт просит ручное подтверждение."""


class ChromiumMissing(Exception):
    """Установленный Chrome или ChromeDriver не найден."""


def _chromium_unavailable(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(
        marker in text
        for marker in (
            "cannot find Chrome binary",
            "Unable to obtain driver",
            "Unable to locate or obtain driver",
            "session not created",
            "Chrome instance exited",
            "selenium-manager",
            "chromedriver",
            "ENOENT",
            "No such file or directory",
        )
    )


def chrome_profile_dir() -> Path:
    override = os.environ.get("ZHOL_CHROME_PROFILE", "").strip()
    path = Path(override).expanduser() if override else CHROME_PROFILE
    path.mkdir(parents=True, exist_ok=True)
    return path


def _chrome_options(headless: bool) -> Options:
    options = Options()
    options.page_load_strategy = "eager"
    profile = chrome_profile_dir()
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--profile-directory=Default")
    binary = os.environ.get("ZHOL_CHROME_BINARY", "").strip()
    if binary:
        options.binary_location = binary
    if os.environ.get("ZHOL_CHROME_CONTAINER", "").strip() == "1":
        # Required to start Chromium as root inside a container. Not a stealth profile.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    if os.environ.get("ZHOL_BROWSER_BASE_URL", "").strip():
        # Local fixture mode only. These hosts resolve nowhere, so a redirect cannot reach a live map site.
        options.add_argument(
            "--host-resolver-rules="
            "MAP 2gis.kz 0.0.0.0, MAP www.2gis.kz 0.0.0.0, MAP 2gis.ru 0.0.0.0, "
            "MAP yandex.ru 0.0.0.0, MAP yandex.kz 0.0.0.0, MAP maps.yandex.ru 0.0.0.0, "
            "MAP maps.google.com 0.0.0.0"
        )
    return options


def _process_rows() -> list[tuple[int, int, str]]:
    proc = Path("/proc")
    if proc.is_dir():
        rows: list[tuple[int, int, str]] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
                stat = (entry / "stat").read_text(encoding="utf-8")
                tail = stat[stat.rfind(")") + 2 :].split()
                rows.append((int(entry.name), int(tail[1]), command))
            except (OSError, IndexError, ValueError):
                continue
        return rows
    import subprocess

    try:
        output = subprocess.check_output(["ps", "-axww", "-o", "pid=,ppid=,command="], text=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _descendants(rows: list[tuple[int, int, str]], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid, _command in rows:
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in found and child not in roots:
                found.add(child)
                stack.append(child)
    return found


def _signal_pid(pid: int, sig: int) -> None:
    if pid <= 1 or pid == os.getpid():
        return
    try:
        os.kill(pid, sig)
    except OSError:
        return


def _our_chromedrivers(rows: list[tuple[int, int, str]]) -> set[int]:
    me = os.getpid()
    return {pid for pid, ppid, command in rows if ppid == me and "chromedriver" in command.casefold()}


def _uses_profile(command: str, profile: Path) -> bool:
    resolved = str(profile.resolve())
    raw = str(profile)
    return f"--user-data-dir={resolved}" in command or f"--user-data-dir={raw}" in command


def release_browser(profile: Path | None, extra_pids: set[int] | None = None) -> None:
    """Stop Chromium and ChromeDriver left on this profile, including a failed start."""
    rows = _process_rows()
    matched: set[int] = set(extra_pids or ())
    commands = {pid: command for pid, _ppid, command in rows}
    parents = {pid: ppid for pid, ppid, _command in rows}
    if profile is not None:
        for pid, _ppid, command in rows:
            if _uses_profile(command, profile):
                matched.add(pid)
    for pid in list(matched):
        parent = parents.get(pid)
        if parent and "chromedriver" in commands.get(parent, "").casefold():
            matched.add(parent)
    matched |= _descendants(rows, matched)
    if not matched:
        _clear_profile_locks(profile)
        return
    log.info("browser cleanup profile=%s pids=%s", profile or "-", sorted(matched))
    for pid in matched:
        _signal_pid(pid, signal.SIGTERM)
    time_left = 0.4
    import time

    time.sleep(time_left)
    for pid in matched:
        _signal_pid(pid, signal.SIGKILL)
    for pid in matched:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            continue
    _clear_profile_locks(profile)


def _clear_profile_locks(profile: Path | None) -> None:
    if profile is None or not profile.exists():
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        path = profile / name
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
        except OSError:
            continue


def _call_bounded(fn, seconds: float) -> None:
    done = threading.Event()

    def run() -> None:
        try:
            fn()
        except Exception:
            return
        finally:
            done.set()

    threading.Thread(target=run, name="browser-call", daemon=True).start()
    done.wait(seconds)


def _service_pid(driver) -> int | None:
    service = getattr(driver, "service", None)
    process = getattr(service, "process", None)
    pid = getattr(process, "pid", None)
    return int(pid) if isinstance(pid, int) and pid > 1 else None


def show_owned_window(driver) -> bool:
    """Bring this session's Chrome window forward.

    The process is the one chromedriver already started. This does not launch
    Chrome, does not attach to another profile, and does not change the page.
    A headless session has no window to show.
    """
    if driver is None or getattr(driver, "_zhol_headed", None) is not True:
        return False
    pid = _owned_chrome_pid(driver)
    if pid is None:
        return False
    if sys.platform == "darwin":
        import subprocess

        script = (
            'tell application "System Events" to '
            f"set frontmost of the first process whose unix id is {pid} to true"
        )
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=5, capture_output=True)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True
    try:
        driver.switch_to.window(driver.current_window_handle)
    except Exception:
        return False
    return True


def _owned_chrome_pid(driver) -> int | None:
    service_pid = _service_pid(driver)
    if service_pid is None:
        return None
    rows = _process_rows()
    commands = {pid: command for pid, _ppid, command in rows}
    for pid in _descendants(rows, {service_pid}):
        command = commands.get(pid, "").casefold()
        if "chromedriver" in command:
            continue
        if "google chrome" in command or "chromium" in command:
            return pid
    return None


def stop_chrome(driver) -> None:
    """Quit one session, then kill anything it still holds. A dead driver does not block."""
    profile = getattr(driver, "_zhol_profile", None) if driver is not None else None
    pid = _service_pid(driver) if driver is not None else None
    if driver is not None:
        quit_driver = getattr(driver, "quit", None)
        if callable(quit_driver):
            _call_bounded(quit_driver, BROWSER_QUIT_TIMEOUT)
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            _call_bounded(stop, 3)
    extra = {pid} if pid else set()
    release_browser(Path(profile) if profile else None, extra)


def _launch_chrome(headless: bool):
    driver_path = os.environ.get("ZHOL_CHROMEDRIVER", "").strip()
    service = Service(
        executable_path=driver_path or None,
        popen_kw={"start_new_session": True},
    )
    return webdriver.Chrome(options=_chrome_options(headless), service=service)


def _start_chrome(headless: bool):
    log.info("selenium headless=%s", headless)
    profile = chrome_profile_dir()
    release_browser(profile)
    last_error: BaseException | None = None
    for attempt in (1, 2):
        before = _our_chromedrivers(_process_rows())
        box: dict[str, object] = {}

        def launch() -> None:
            try:
                box["driver"] = _launch_chrome(headless)
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=launch, name="chrome-start", daemon=True)
        thread.start()
        thread.join(BROWSER_START_TIMEOUT)
        driver = box.get("driver")
        error = box.get("error")
        if thread.is_alive() or error is not None or driver is None:
            extras = _our_chromedrivers(_process_rows()) - before
            release_browser(profile, extras)
            thread.join(5)
            if isinstance(error, BaseException):
                last_error = error
            else:
                last_error = WebDriverException("chrome not reachable: session was not created")
            if _chromium_unavailable(last_error):
                raise ChromiumMissing(CHROMIUM_MESSAGE) from last_error
            if attempt == 1:
                log.warning("browser start failed, cleaning the profile and trying once more: %s", last_error)
                continue
            raise last_error
        setattr(driver, "_zhol_profile", str(profile))
        setattr(driver, "_zhol_headed", headless is False)
        log.info("browser started mode=%s", "headless" if headless else "headed")
        return driver
    raise WebDriverException("chrome not reachable: session was not created")


def _logs_dir() -> Path:
    path = Path("logs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging() -> None:
    logger = logging.getLogger("gis")
    if logger.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler = logging.FileHandler(_logs_dir() / LOG_NAME, encoding="utf-8")
    handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    if os.environ.get("ZHOL_CHROME_CONTAINER", "").strip() == "1":
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    log.info("Chrome profile: %s", chrome_profile_dir())


def resolve_city(city: str | None = None, city_slug: str | None = None) -> tuple[str, str]:
    """Вернуть (название, slug) только для города с проверенным путём 2ГИС."""
    from zhol.agents.scout.localities import twogis_slug

    if city is None and city_slug is None:
        return CITY_NAME, CITY_SLUG
    try:
        name, slug = twogis_slug(city or CITY_NAME)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if city_slug is not None and city_slug != slug:
        raise ValueError(
            f"Город «{name}» не совпадает со slug «{city_slug}». Сбор не переключается на Алматы."
        )
    return name, slug


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _check_delay(delay_range: tuple[float, float] | None) -> tuple[float, float]:
    if delay_range is None:
        return DEFAULT_DELAY_RANGE
    low, high = float(delay_range[0]), float(delay_range[1])
    if low < 0 or high < low:
        raise ValueError("Интервал паузы задан неверно.")
    return (low, high)


def _print_query(job: dict, result: dict, total_unique: int) -> None:
    print(f"Query {job['index']}/{job['group_size']}: {job['query']}")
    print(f"Pages scanned: {result['pages_loaded']}")
    print(f"URLs found: {result['unique_links']}")
    print(f"Processed: {result['attempted']}")
    print(f"New rows: {result['success']}")
    print(f"Duplicates: {result['duplicates']}")
    print(f"Total unique: {total_unique}")
    print()


def _close_group(group: str, jobs: list[dict], writer: ExcelWriter) -> None:
    group_jobs = [job for job in jobs if job["group"] == group]
    if not group_jobs or len(group_jobs) != group_jobs[0]["group_size"]:
        return
    done = sum(1 for job in group_jobs if job["query"] in writer.completed_queries())
    if done != group_jobs[0]["group_size"]:
        return
    print(f"[{group}] completed")
    print(f"Queries completed: {done}/{done}")
    print(f"Total unique so far: {len(writer.rows)}")
    print()


def run_batch(
    jobs: list[dict],
    output_path: str | Path,
    *,
    headless: bool = False,
    resume: bool = False,
    debug_limit: int | None = None,
    delay_range: tuple[float, float] | None = None,
    pause=None,
    resume_hint: str = "",
    city: str | None = None,
    city_slug: str | None = None,
) -> dict:
    """Отдельный Chrome на каждый запрос. По умолчанию город Алматы."""
    city_name, slug = resolve_city(city, city_slug)
    setup_logging()
    if debug_limit is not None and debug_limit < 1:
        raise ValueError("Отладочный лимит должен быть не меньше 1.")
    delay_range = _check_delay(delay_range)
    writer = ExcelWriter(Path(output_path), resume=resume)
    stop = threading.Event()
    try:
        signal.signal(signal.SIGINT, lambda *_args: stop.set())
    except ValueError:
        pass

    def pause_fn(message: str) -> None:
        writer.persist()
        if headless:
            log.warning("Нужно ручное подтверждение. %s", (message or "").strip())
            stop.set()
            raise ManualConfirmationRequired(CONFIRM_MESSAGE)
        if pause is None:
            input(message)
            return
        pause(message)

    def on_record(record: dict):
        return writer.add(record)

    def remember(query: str, result: dict) -> None:
        confirmed = result["status"] == "completed" and bool(result["link_collection_complete"])
        writer.set_query(
            query,
            status="complete" if confirmed else "incomplete",
            link_collection_complete=bool(result["link_collection_complete"]),
            stop_reason=result.get("stop_reason") or "",
            finished_at=_stamp(),
        )
        writer.persist()
        log.info(
            "query=%s status=%s complete=%s reason=%s",
            query,
            "complete" if confirmed else "incomplete",
            result["link_collection_complete"],
            result.get("stop_reason"),
        )

    def leave_incomplete(query: str, stop_reason: str) -> None:
        writer.set_query(
            query,
            status="incomplete",
            link_collection_complete=False,
            stop_reason=stop_reason,
            finished_at=_stamp(),
        )
        writer.persist()

    def close_driver(driver) -> None:
        if driver is None:
            return
        try:
            stop_chrome(driver)
        except Exception:
            log.exception("Не удалось закрыть Chrome")

    outcome = "completed"
    aborted = False
    duplicates = 0
    errors = 0
    shown_group = None
    session_id = ""
    try:
        for job in jobs:
            if stop.is_set():
                outcome = "interrupted"
                break
            group = job["group"]
            if group != shown_group:
                if shown_group is not None:
                    _close_group(shown_group, jobs, writer)
                print(f"[{group}]")
                shown_group = group
            if job["query"] in writer.completed_queries():
                print(f"Query {job['index']}/{job['group_size']}: {job['query']}")
                print("Пропуск: конец выдачи уже подтверждён")
                print()
                log.info("skip complete query=%s", job["query"])
                continue
            failures = 0
            while True:
                driver = None
                try:
                    try:
                        driver = _start_chrome(headless)
                    except Exception as exc:
                        log.exception("Не удалось запустить Chrome")
                        if _chromium_unavailable(exc):
                            raise ChromiumMissing(CHROMIUM_MESSAGE) from exc
                        raise
                    session_id = getattr(driver, "session_id", "") or ""
                    log.info("chrome session=%s query=%s", session_id, job["query"])
                    result = collect_search(
                        driver,
                        city_name,
                        slug,
                        job["query"],
                        debug_limit,
                        pause_fn,
                        stop,
                        on_record=on_record,
                        delay_range=delay_range,
                        known_ids=writer.seen,
                        quiet=True,
                    )
                    remember(job["query"], result)
                    duplicates += result["duplicates"]
                    errors += result["errors"]
                    _print_query(job, result, len(writer.rows))
                    if result["status"] != "completed" or stop.is_set():
                        outcome = "interrupted"
                        aborted = True
                    break
                except ManualConfirmationRequired:
                    leave_incomplete(job["query"], CONFIRM_MESSAGE)
                    outcome = "confirmation"
                    aborted = True
                    break
                except KeyboardInterrupt:
                    leave_incomplete(job["query"], "Сбор прерван")
                    outcome = "interrupted"
                    aborted = True
                    break
                except (ChromiumMissing, ExcelWriteError):
                    raise
                except Exception as exc:
                    if not is_browser_crash(exc):
                        log.exception("Сбор завершился с ошибкой")
                        raise
                    failures += 1
                    log.exception("Browser crashed during query: %s", job["query"])
                    print(f"Browser crashed during query: {job['query']}")
                    leave_incomplete(job["query"], "Browser crashed")
                    print(f"Existing rows preserved: {len(writer.rows)}")
                    if failures >= MAX_BROWSER_RESTARTS_PER_QUERY:
                        print(
                            f"Query left incomplete after {MAX_BROWSER_RESTARTS_PER_QUERY} "
                            f"browser failures: {job['query']}"
                        )
                        errors += 1
                        break
                    print(
                        f"Restarting Chrome: attempt {failures}/{MAX_BROWSER_RESTARTS_PER_QUERY}"
                    )
                finally:
                    close_driver(driver)
                    driver = None
            if aborted:
                break
        if shown_group is not None and outcome == "completed":
            _close_group(shown_group, jobs, writer)
    except ExcelWriteError:
        raise
    finally:
        try:
            saved = writer.persist()
        except ExcelWriteError:
            log.exception("Финальная запись Excel не выполнена")
            raise
    confirmed = [job for job in jobs if job["query"] in writer.completed_queries()]
    summary = {
        "city": city_name,
        "outcome": outcome,
        "groups": len({job["group"] for job in jobs}),
        "queries_scheduled": len(jobs),
        "queries_completed": len(confirmed),
        "queries_unconfirmed": len(jobs) - len(confirmed),
        "unique": len(writer.rows),
        "duplicates": duplicates,
        "errors": errors,
        "output": str(saved or writer.final_path),
        "incomplete_queries": 1 if aborted else 0,
        "resume_hint": resume_hint,
        "session_id": session_id,
    }
    if outcome == "confirmation":
        log.info("Сбор остановлен: нужно ручное подтверждение. Строки записаны: %s", saved)
        print(CONFIRM_MESSAGE)
    return summary
