"""Local HTML and localhost browser sources. These tests do not open 2gis.kz."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from search import canonical_firm_url, search_host, search_page_url
from zhol.agents.scout.browser_2gis import (
    BLOCKER,
    ENGINE_BLOCKER,
    ENGINE_ENV,
    GRANT_COMMERCIAL,
    GRANT_ENV,
    GRANT_LOCAL,
    LOCAL_BASE_ENV,
    LOCAL_HTML_ENV,
    BrowserCollectionRefused,
    active_source,
    browser_collection_authorized,
    browser_engine_available,
    browser_mode,
    build_browser_plans,
    live_2gis_authorized,
    source_authorized,
)
from zhol.agents.scout.selenium_collector import _LocalHtmlSite
from zhol.agents.scout.setup import collection_setup

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests" / "fixtures" / "catalog.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "selenium"


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (GRANT_ENV, ENGINE_ENV, LOCAL_BASE_ENV, LOCAL_HTML_ENV, "ZHOL_CHROME_CONTAINER", "ZHOL_CHROME_BINARY"):
        monkeypatch.delenv(name, raising=False)


def test_live_site_stays_closed_without_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert live_2gis_authorized() is False
    assert browser_engine_available() is False
    assert active_source() == "live_2gis"
    assert source_authorized() is False
    mode = browser_mode()
    assert mode["available"] is False
    assert mode["engine"] is False
    assert mode["source_kind"] == "live_2gis"
    assert "выключен" in mode["note"]
    with pytest.raises(BrowserCollectionRefused, match="выключен"):
        build_browser_plans(city="Алматы", categories=["Салон красоты"], queries=[], platforms=["2gis"], limit=1)


def test_local_grant_does_not_count_as_commercial(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(ENGINE_ENV, "1")
    monkeypatch.setenv(GRANT_ENV, GRANT_LOCAL)
    assert GRANT_LOCAL != GRANT_COMMERCIAL
    assert live_2gis_authorized() is True
    mode = browser_mode()
    assert mode["authorization_scope"] == "local"
    assert mode["available"] is True
    assert "некоммерческ" in mode["note"]
    assert "Коммерческое разрешение не подтверждено" in mode["note"]
    assert "коммерческое использование" not in mode["note"]
    plans = build_browser_plans(city="Алматы", categories=["Салон красоты"], queries=[], platforms=["2gis"], limit=1)
    assert plans[0]["source_mode"] == "browser_2gis"
    body = collection_setup(CATALOG)
    sources = {item["id"]: item for item in body["sources"]}
    assert sources["2gis"]["live_available"] is True
    assert "некоммерческ" in sources["2gis"]["live_note"]
    assert sources["yandex"]["live_available"] is False
    assert sources["google"]["live_available"] is False
    assert "2ГИС" not in sources["yandex"]["live_note"]
    assert "2ГИС" not in sources["google"]["live_note"]


def test_commercial_grant_stays_a_separate_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(ENGINE_ENV, "1")
    monkeypatch.setenv(GRANT_ENV, GRANT_COMMERCIAL)
    mode = browser_mode()
    assert mode["authorization_scope"] == "commercial"
    assert mode["available"] is True
    assert "некоммерческ" not in mode["note"]
    assert "коммерческое использование" in mode["note"]


def test_other_grant_text_does_not_open_the_site(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(ENGINE_ENV, "1")
    monkeypatch.setenv(GRANT_ENV, "operator-says-yes")
    assert live_2gis_authorized() is False
    mode = browser_mode()
    assert mode["available"] is False
    assert mode["note"] == BLOCKER


def test_engine_flag_does_not_authorize_the_live_site(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(ENGINE_ENV, "1")
    assert browser_engine_available() is True
    assert browser_collection_authorized() is False
    mode = browser_mode()
    assert mode["available"] is False
    assert mode["note"] == BLOCKER
    assert search_host() == "https://2gis.kz"


def test_public_base_url_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(ENGINE_ENV, "1")
    monkeypatch.setenv(LOCAL_BASE_ENV, "https://2gis.kz")
    assert active_source() == "live_2gis"
    assert browser_collection_authorized() is False
    assert search_host() == "https://2gis.kz"
    assert search_page_url("almaty", "Салон красоты", 1).startswith("https://2gis.kz/")


def test_localhost_source_does_not_need_the_grant_or_catalog_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.setenv(ENGINE_ENV, "1")
    monkeypatch.setenv(LOCAL_BASE_ENV, "http://127.0.0.1:8765")
    assert live_2gis_authorized() is False
    assert source_authorized() is True
    assert active_source() == "localhost"
    mode = browser_mode()
    assert mode["available"] is True
    assert mode["origin"] == "http://127.0.0.1:8765"
    assert "2ГИС не открывается" in mode["note"]
    plans = build_browser_plans(city="Алматы", categories=["Салон красоты"], queries=[], platforms=["2gis"], limit=1)
    assert plans[0]["source_mode"] == "browser_2gis"
    assert search_page_url("almaty", "Салон красоты", 1) == "http://127.0.0.1:8765/almaty/search/%D0%A1%D0%B0%D0%BB%D0%BE%D0%BD%20%D0%BA%D1%80%D0%B0%D1%81%D0%BE%D1%82%D1%8B"
    assert canonical_firm_url("almaty", "70000001000000901") == "http://127.0.0.1:8765/almaty/firm/70000001000000901"
    body = collection_setup(CATALOG)
    listed = {item["id"]: item for item in body["collection_modes"]}
    assert listed["browser_2gis"]["available"] is True
    assert body["browser_2gis"]["source_kind"] == "localhost"
    assert body["stop"]["supported"] is True
    assert body["resume"]["supported"] is True


def test_local_source_without_the_engine_stays_unselectable(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(LOCAL_HTML_ENV, str(FIXTURE))
    assert source_authorized("local_html") is True
    assert browser_mode()["available"] is False
    assert browser_mode()["note"] == ENGINE_BLOCKER
    with pytest.raises(BrowserCollectionRefused, match="не готов"):
        build_browser_plans(city="Алматы", categories=["Салон красоты"], queries=[], platforms=["2gis"], limit=1)


def test_local_html_directory_is_served_only_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(LOCAL_HTML_ENV, str(FIXTURE))
    monkeypatch.setenv(ENGINE_ENV, "1")
    assert active_source() == "local_html"
    assert browser_mode()["available"] is True
    site = _LocalHtmlSite(FIXTURE)
    try:
        origin = site.start()
        assert origin.startswith("http://127.0.0.1:")
        assert search_host() == origin
        import urllib.request

        with urllib.request.urlopen(origin + "/almaty/firm/70000001000000901", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert "North Beauty" in page
        with urllib.request.urlopen(origin + "/almaty/search/salon", timeout=5) as response:
            listing = response.read().decode("utf-8")
        assert "70000001000000901" in listing
    finally:
        site.close()
    assert os.environ.get(LOCAL_BASE_ENV) in {None, ""}
