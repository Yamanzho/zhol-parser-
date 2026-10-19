"""Вызовы Selenium на месте прежних вызовов Playwright. Логика сбора не здесь."""

import os
from urllib.parse import urlparse

from selenium.common.exceptions import TimeoutException as BrowserTimeout
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

__all__ = [
    "BrowserTimeout",
    "By",
    "ExternalNavigationRefused",
    "WebDriverException",
    "assert_local_destination",
    "click_css",
    "count_css",
    "eval_js",
    "is_browser_crash",
    "open_url",
    "wait_js",
    "wait_visible",
]

# Loopback names only. A local fixture run must not follow a redirect onto a live map site.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})

_CRASH_MARKERS = (
    "tab crashed",
    "chrome not reachable",
    "invalid session id",
    "disconnected",
    "session deleted",
    "target frame detached",
)


def is_browser_crash(exc: BaseException) -> bool:
    """Сессия Chrome умерла. Обычный таймаут карточки сюда не входит."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    if type(exc).__name__ == "InvalidSessionIdException":
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _CRASH_MARKERS)


class ExternalNavigationRefused(RuntimeError):
    """A local fixture run tried to open a host outside the loopback allowlist."""


def local_collection_locked() -> bool:
    return bool(os.environ.get("ZHOL_BROWSER_BASE_URL", "").strip())


def host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS:
        return False
    return "2gis." not in host and "yandex." not in host and "google." not in host


def assert_local_destination(driver: WebDriver) -> None:
    """Stop after navigation if Chrome left the loopback allowlist."""
    if not local_collection_locked():
        return
    try:
        current = driver.current_url or ""
    except WebDriverException:
        raise
    if current and not host_allowed(current):
        raise ExternalNavigationRefused(current)


def open_url(driver: WebDriver, url: str, timeout_ms: int) -> None:
    if local_collection_locked() and not host_allowed(url):
        raise ExternalNavigationRefused(url)
    driver.set_page_load_timeout(timeout_ms / 1000)
    try:
        driver.get(url)
    except WebDriverException:
        assert_local_destination(driver)
        raise
    assert_local_destination(driver)


def eval_js(driver: WebDriver, script: str, *args):
    return driver.execute_script(script, *args)


def wait_visible(driver: WebDriver, css: str, timeout_ms: int) -> None:
    WebDriverWait(driver, timeout_ms / 1000).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, css))
    )


def wait_js(driver: WebDriver, script: str, timeout_ms: int, *args) -> None:
    WebDriverWait(driver, timeout_ms / 1000).until(
        lambda current: current.execute_script(script, *args)
    )


def count_css(driver: WebDriver, css: str) -> int:
    return len(driver.find_elements(By.CSS_SELECTOR, css))


def click_css(driver: WebDriver, css: str, timeout_ms: int) -> None:
    WebDriverWait(driver, timeout_ms / 1000).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, css))
    ).click()
