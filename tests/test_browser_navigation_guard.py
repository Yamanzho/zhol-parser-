"""Local fixture navigation stays on the loopback allowlist."""

import os

import pytest

from browser_ops import ExternalNavigationRefused, host_allowed, open_url


class _Driver:
    def __init__(self, current: str) -> None:
        self.current_url = current
        self.opened: str | None = None
        self.timeout = None

    def set_page_load_timeout(self, seconds: float) -> None:
        self.timeout = seconds

    def get(self, url: str) -> None:
        self.opened = url
        if url.endswith("/redirect-external"):
            self.current_url = "https://2gis.kz/almaty"


def test_public_map_host_is_not_a_local_destination():
    assert host_allowed("http://127.0.0.1:8765/almaty/firm/1")
    assert host_allowed("http://host.docker.internal:8765/almaty/search/x")
    assert not host_allowed("https://2gis.kz/almaty")
    assert not host_allowed("https://maps.yandex.ru/")
    assert not host_allowed("https://maps.google.com/")


def test_open_url_refuses_a_live_host_while_the_fixture_base_is_set(monkeypatch):
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", "http://host.docker.internal:8765")
    driver = _Driver("http://host.docker.internal:8765/start")
    with pytest.raises(ExternalNavigationRefused):
        open_url(driver, "https://2gis.kz/almaty", 1000)
    assert driver.opened is None


def test_open_url_refuses_a_redirect_off_the_allowlist(monkeypatch):
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", "http://127.0.0.1:8765")
    driver = _Driver("http://127.0.0.1:8765/start")
    with pytest.raises(ExternalNavigationRefused):
        open_url(driver, "http://127.0.0.1:8765/redirect-external", 1000)
    assert driver.opened == "http://127.0.0.1:8765/redirect-external"


def test_live_collection_is_not_blocked_by_the_fixture_guard(monkeypatch):
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    driver = _Driver("https://2gis.kz/almaty")
    open_url(driver, "https://2gis.kz/almaty/firm/1", 1000)
    assert driver.opened == "https://2gis.kz/almaty/firm/1"
    assert os.environ.get("ZHOL_BROWSER_BASE_URL") is None
