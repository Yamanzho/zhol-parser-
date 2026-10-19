"""Yandex and Google Selenium adapters against local HTML and real Chrome.

These tests do not open Yandex or Google. Chrome is pointed at 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.browser_2gis import GRANT_ENV, GRANT_VALUE
from zhol.agents.scout.browser_google import BLOCKER as GOOGLE_BLOCKER
from zhol.agents.scout.browser_maps import BrowserCollectionRefused
from zhol.agents.scout.browser_yandex import BLOCKER as YANDEX_BLOCKER
from zhol.agents.scout.browser_yandex import browser_mode as yandex_mode
from zhol.agents.scout.maps_selenium import run_maps_batch
from zhol.agents.scout.normalize import normalize_phone
from zhol.agents.scout.setup import collection_setup
from zhol.api.main import app
from zhol.browser.discovery.types import RunControl
from zhol.browser.google import selenium as google_selenium
from zhol.browser.yandex import selenium as yandex_selenium
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, CompanyPlatformId, Job

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "maps-selenium" / "server.py"
CATALOG = ROOT / "tests" / "fixtures" / "catalog.yaml"
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}


def _load_fixture():
    spec = importlib.util.spec_from_file_location("maps_selenium_server", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    monkeypatch.delenv(GRANT_ENV, raising=False)
    monkeypatch.delenv("ZHOL_YANDEX_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_GOOGLE_BROWSER_GRANT", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if not Path(CHROME).is_file() and not os.environ.get("ZHOL_CHROME_BINARY"):
        pytest.skip("Chrome is not installed")
    module = _load_fixture()
    paths: list[str] = []

    class Recording(module.Handler):
        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recording)
    threading.Thread(target=server.serve_forever, name="maps-lab", daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    binary = os.environ.get("ZHOL_CHROME_BINARY", "").strip() or CHROME
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", origin)
    monkeypatch.setenv("ZHOL_CHROME_HEADLESS", "1")
    monkeypatch.setenv("ZHOL_CHROME_BINARY", binary)
    monkeypatch.setenv("ZHOL_CHROME_PROFILE", str(tmp_path / "chrome-profile"))
    monkeypatch.delenv(GRANT_ENV, raising=False)
    monkeypatch.delenv("ZHOL_YANDEX_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_GOOGLE_BROWSER_GRANT", raising=False)
    try:
        yield {"origin": origin, "paths": paths, "binary": binary, "cards": module.cards()}
    finally:
        server.shutdown()
        server.server_close()


def _chrome(binary: str, profile: Path):
    options = Options()
    options.page_load_strategy = "eager"
    options.binary_location = binary
    profile.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "--host-resolver-rules="
        "MAP 2gis.kz 0.0.0.0, MAP www.2gis.kz 0.0.0.0, MAP 2gis.ru 0.0.0.0, "
        "MAP yandex.ru 0.0.0.0, MAP yandex.kz 0.0.0.0, MAP maps.yandex.ru 0.0.0.0, "
        "MAP maps.google.com 0.0.0.0, MAP www.google.com 0.0.0.0"
    )
    driver_path = os.environ.get("ZHOL_CHROMEDRIVER", "").strip()
    service = Service(executable_path=driver_path) if driver_path else None
    return webdriver.Chrome(options=options, service=service)


class _Gate:
    def __init__(self, inner, *, after: int, action) -> None:
        self._inner = inner
        self._after = after
        self._action = action
        self.cards = 0
        self.closed = False

    def execute_script(self, script, *args):
        text = script or ""
        if "zhol-card-extract" in text:
            self.cards += 1
            if self.cards == self._after:
                self._action()
        return self._inner.execute_script(script, *args)

    def quit(self) -> None:
        self.closed = True
        try:
            self._inner.quit()
        except Exception:
            pass

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _external(card, platform: str) -> str:
    return card.yandex_id if platform == "yandex" else card.google_id


def _run(factory, folder: Path, lab: dict, platform: str, **kwargs) -> int:
    profile = kwargs.pop("profile", folder / "chrome-profile")
    driver_factory = kwargs.pop("driver_factory", None)
    if driver_factory is None:
        driver_factory = lambda: _chrome(lab["binary"], profile)  # noqa: E731
    return run_maps_batch(
        factory,
        platform=platform,
        city="Алматы",
        category=kwargs.pop("category", "Салон красоты"),
        limit=kwargs.pop("limit"),
        out=folder / "out.txt",
        resume=kwargs.pop("resume", None),
        control=kwargs.pop("control", None),
        driver_factory=driver_factory,
        run_dir=folder / "run",
        delay_range=kwargs.pop("delay_range", (0.0, 0.0)),
    )


def _seed(factory, platform: str, external_id: str) -> None:
    with factory() as session, session.begin():
        company = Company(
            id_2gis=None,
            name="Салон Север",
            city="Алматы",
            niche="Салон красоты",
            source="seed",
        )
        session.add(company)
        session.flush()
        session.add(
            CompanyPlatformId(
                company_id=company.id,
                platform=platform,
                external_id=external_id,
                public_url="http://127.0.0.1/seed",
            )
        )


def _progress(factory, batch_id: int) -> tuple[dict, str]:
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        return dict(batch.progress or {}), batch.status


def _count(factory, model) -> int:
    with factory() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _snap(factory, platform: str, external_id: str) -> CardSnapshot:
    with factory() as db:
        row = db.scalar(
            select(CompanyPlatformId).where(
                CompanyPlatformId.platform == platform,
                CompanyPlatformId.external_id == external_id,
            )
        )
        assert row is not None
        snap = db.scalar(
            select(CardSnapshot)
            .where(CardSnapshot.company_id == row.company_id, CardSnapshot.platform == platform)
            .order_by(CardSnapshot.id.desc())
        )
        assert snap is not None
        db.expunge(snap)
        return snap


def _assert_local(lab: dict) -> None:
    assert lab["paths"]
    for path in lab["paths"]:
        assert "yandex." not in path
        assert "google." not in path
        assert "2gis." not in path


def test_each_source_needs_its_own_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from zhol.agents.scout.browser_google import GRANT_ENV as GOOGLE_GRANT
    from zhol.agents.scout.browser_google import browser_mode as google_mode
    from zhol.agents.scout.browser_maps import GRANT_COMMERCIAL, GRANT_LOCAL
    from zhol.agents.scout.browser_yandex import GRANT_ENV as YANDEX_GRANT

    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_HTML_ROOT", raising=False)
    monkeypatch.delenv(GRANT_ENV, raising=False)
    monkeypatch.delenv(YANDEX_GRANT, raising=False)
    monkeypatch.delenv(GOOGLE_GRANT, raising=False)
    monkeypatch.setenv("ZHOL_BROWSER_ENGINE", "1")
    monkeypatch.setenv(YANDEX_GRANT, GRANT_LOCAL)
    yandex = yandex_mode()
    google = google_mode()
    assert yandex["available"] is True
    assert yandex["authorization_scope"] == "local"
    assert yandex["origin"] == "https://yandex.kz"
    assert "Коммерческое использование не подтверждено" in yandex["note"]
    assert google["available"] is False
    assert "No Scraping" in google["note"]
    from urllib.parse import quote

    yandex_url = yandex_selenium.search_page_url("https://yandex.kz", "Салон красоты", 1)
    assert yandex_url == "https://yandex.kz/maps/162/almaty/search/" + quote("Салон красоты", safe="")
    with pytest.raises(BrowserCollectionRefused):
        google_selenium.search_page_url("https://www.google.com", "Салон красоты", 1)
    with pytest.raises(BrowserCollectionRefused):
        yandex_selenium.search_page_url("https://yandex.ru", "Салон красоты", 1)
    monkeypatch.setenv(YANDEX_GRANT, "yes")
    assert yandex_mode()["available"] is False
    monkeypatch.setenv(YANDEX_GRANT, GRANT_COMMERCIAL)
    commercial = yandex_mode()
    assert commercial["available"] is True
    assert commercial["authorization_scope"] == "commercial"
    assert commercial["authorization_scope"] != "local"
    monkeypatch.delenv(YANDEX_GRANT, raising=False)
    monkeypatch.setenv(GOOGLE_GRANT, GRANT_LOCAL)
    opened = google_mode()
    assert opened["available"] is True
    assert opened["collection_state"] == "available"
    assert opened["authorization_scope"] == "local"
    assert opened["origin"] == "https://www.google.com"
    assert yandex_mode()["available"] is False
    google_url = google_selenium.search_page_url("https://www.google.com", "Салон красоты", 1)
    assert google_url.startswith("https://www.google.com/maps/search/")
    with pytest.raises(BrowserCollectionRefused):
        yandex_selenium.search_page_url("https://yandex.kz", "Салон красоты", 1)


def test_authorized_collect_enqueues_and_does_not_open_chrome(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    from zhol.agents.scout.browser_maps import GRANT_LOCAL

    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.setenv("ZHOL_BROWSER_ENGINE", "1")
    monkeypatch.setenv("ZHOL_YANDEX_BROWSER_GRANT", GRANT_LOCAL)
    monkeypatch.delenv("ZHOL_GOOGLE_BROWSER_GRANT", raising=False)
    body = collection_setup(CATALOG)
    modes = {item["id"]: item for item in body["collection_modes"]}
    assert modes["browser_yandex"]["available"] is True
    assert modes["browser_yandex"]["authorization_scope"] == "local"
    assert modes["browser_google"]["available"] is False
    assert body["sources"][1]["id"] == "yandex"
    sources = {item["id"]: item for item in body["sources"]}
    assert sources["yandex"]["live_available"] is True
    assert sources["yandex"]["available"] is False
    assert sources["yandex"]["collection_state"] == "available"
    assert "429" in sources["yandex"]["limit_note"]
    assert "успешн" in sources["yandex"]["limit_note"]
    assert sources["google"]["live_available"] is False
    assert sources["google"]["collection_state"] == "unauthorized"
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Салон красоты"],
                "platforms": ["yandex"],
                "limit": 10,
                "source_mode": "browser_yandex",
            },
        )
        denied = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Салон красоты"],
                "platforms": ["google"],
                "limit": 10,
                "source_mode": "browser_google",
            },
        )
    assert accepted.status_code == 200
    assert accepted.json()["source_mode"] == "browser_yandex"
    assert accepted.json()["jobs"][0]["status"] == "queued"
    assert denied.status_code == 400
    assert denied.json()["detail"] == GOOGLE_BLOCKER
    with factory() as db:
        job = db.scalar(select(Job))
        assert job is not None
        assert job.queue == "collector"
        assert job.payload["source_mode"] == "browser_yandex"
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0


def test_a_page_that_only_says_limited_is_an_access_restriction() -> None:
    from zhol.agents.scout.maps_selenium import _blocked_kind

    assert _blocked_kind({"text": "limited", "access": ""}) == "access_restriction"
    assert _blocked_kind({"text": "Company Limited", "access": ""}) is None


def test_a_foreign_city_is_not_stored_as_almaty() -> None:
    from zhol.agents.scout.maps_selenium import city_verdict

    assert city_verdict("Алматы", "ул. Тимирязева 60, Алматы 050060", "Алматы") == ("match", "Алматы")
    assert city_verdict("Алматы", "6VGV+357, Темириазев-стрит, Сарань 050060", None) == ("out_of_city", "Сарань")
    assert city_verdict("Алматы", "ул. Абая 1, Алматы, Сарань 050060", None) == ("ambiguous", None)
    assert city_verdict("Алматы", "улица Абая, 1", None) == ("not_shown", None)


def test_live_hosts_and_the_2gis_grant_do_not_open_the_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_YANDEX_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_GOOGLE_BROWSER_GRANT", raising=False)
    monkeypatch.setenv(GRANT_ENV, GRANT_VALUE)
    mode = yandex_mode()
    assert mode["available"] is False
    assert "некоммерческ" in mode["note"]
    assert "2ГИС" not in mode["note"]
    body = collection_setup(CATALOG)
    listed = {item["id"]: item for item in body["collection_modes"]}
    assert listed["browser_2gis"]["id"] == "browser_2gis"
    assert listed["browser_yandex"]["available"] is False
    assert listed["browser_google"]["available"] is False
    assert "No Scraping" in listed["browser_google"]["note"]
    with pytest.raises(BrowserCollectionRefused):
        yandex_selenium.search_page_url("https://yandex.kz", "Салон красоты", 1)
    with pytest.raises(BrowserCollectionRefused):
        google_selenium.search_page_url("https://maps.google.com", "Салон красоты", 1)
    called: list[str] = []
    with pytest.raises(BrowserCollectionRefused):
        run_maps_batch(
            lambda: called.append("db"),  # type: ignore[arg-type]
            platform="google",
            city="Алматы",
            category="Салон красоты",
            limit=1,
            out=Path("/tmp/zhol-maps-refused.txt"),
            driver_factory=lambda: called.append("chrome"),
        )
    assert called == []
    assert "search-snippet-view" not in google_selenium.READ_SEARCH_JS
    assert "maps-place-link" not in yandex_selenium.READ_SEARCH_JS
    assert "gisCollectSearch" not in yandex_selenium.READ_SEARCH_JS
    assert "gisCollectSearch" not in google_selenium.READ_SEARCH_JS


def test_api_rejects_map_collection_before_a_job(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GRANT_ENV, GRANT_VALUE)
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", "http://127.0.0.1:9")
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "categories": ["Салон красоты"],
                "platforms": ["yandex"],
                "limit": 10,
                "source_mode": "browser_yandex",
            },
        )
        denied_google = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Салон красоты"],
                "platforms": ["google"],
                "limit": 10,
                "source_mode": "browser_google",
            },
        )
    assert denied.status_code == 400
    assert denied.json()["detail"] == YANDEX_BLOCKER
    assert denied_google.status_code == 400
    assert denied_google.json()["detail"] == GOOGLE_BLOCKER
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0


@pytest.mark.parametrize("platform", ["yandex", "google"])
def test_ten_companies_keep_existing_rows_and_null_contacts(factory, lab, tmp_path: Path, platform: str) -> None:
    first = lab["cards"][0]
    missing = lab["cards"][2]
    _seed(factory, platform, _external(first, platform))
    batch_id = _run(factory, tmp_path, lab, platform, limit=10)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert len(progress["seen_ids"]) == 10
    assert progress["existing_companies"] == 1
    assert progress["new_companies"] == 9
    assert progress["duplicates"] >= 1
    assert progress["metrics"]["snapshots_written"] == 10
    assert _count(factory, Company) == 10
    assert _count(factory, CardSnapshot) == 10
    assert _count(factory, CollectionBatchItem) == 10
    if platform == "yandex":
        assert progress["pages_loaded"] >= 2
    else:
        assert progress["scrolls"] >= 1
    kept = _snap(factory, platform, _external(first, platform))
    assert kept.fields["name"] == "Салон Север"
    assert kept.fields["address"] == "улица Абая, 1"
    assert kept.fields["phones"] == [normalize_phone(first.phone)]
    assert kept.fields["website"] == first.website
    assert kept.fields["whatsapp"] == normalize_phone(first.phone)
    assert kept.fields["instagram"] == "salon.almaty.01"
    assert kept.fields["rating"] == 4.6
    assert kept.fields["reviews_count"] == 12
    assert kept.fields["category"] == "Салон красоты"
    assert str(kept.url).startswith(lab["origin"])
    assert kept.screenshot_path and Path(kept.screenshot_path).is_file()
    empty = _snap(factory, platform, _external(missing, platform))
    assert empty.fields["phones"] is None
    assert empty.fields["website"] is None
    assert empty.fields["whatsapp"] is None
    assert empty.fields["instagram"] is None
    assert empty.fields["name"] == missing.name
    with factory() as db:
        companies = list(db.scalars(select(Company)).all())
        assert all(company.id_2gis is None for company in companies)
        ids = list(
            db.scalars(
                select(CompanyPlatformId.external_id).where(CompanyPlatformId.platform == platform)
            ).all()
        )
        assert len(ids) == len(set(ids)) == 10
    _assert_local(lab)


@pytest.mark.parametrize("platform", ["yandex", "google"])
def test_pause_and_resume(factory, lab, tmp_path: Path, platform: str) -> None:
    control = RunControl()

    def pausing() -> _Gate:
        return _Gate(_chrome(lab["binary"], tmp_path / "pause-profile"), after=2, action=control.pause)

    paused = _run(
        factory,
        tmp_path / "paused",
        lab,
        platform,
        limit=4,
        control=control,
        driver_factory=pausing,
        profile=tmp_path / "pause-profile",
    )
    progress, status = _progress(factory, paused)
    assert status == "paused"
    assert len(progress["seen_ids"]) == 2
    assert _count(factory, Company) == 2
    _run(factory, tmp_path / "resume", lab, platform, limit=4, resume=paused, profile=tmp_path / "resume-profile")
    progress, status = _progress(factory, paused)
    assert status == "completed"
    assert len(progress["seen_ids"]) == 4
    assert _count(factory, Company) == 4
    assert _count(factory, CardSnapshot) == 4
    assert progress["metrics"]["browser_restarts"] >= 1
    _assert_local(lab)


@pytest.mark.parametrize("platform", ["yandex", "google"])
def test_stop_is_terminal(factory, lab, tmp_path: Path, platform: str) -> None:
    control = RunControl()
    opened: list[int] = []

    def stopping() -> _Gate:
        opened.append(1)
        return _Gate(_chrome(lab["binary"], tmp_path / "stop-profile"), after=2, action=control.stop)

    stopped = _run(
        factory,
        tmp_path / "stopped",
        lab,
        platform,
        limit=4,
        control=control,
        driver_factory=stopping,
        profile=tmp_path / "stop-profile",
    )
    _, status = _progress(factory, stopped)
    assert status == "stopped"
    assert _count(factory, Company) == 2
    again = _run(
        factory,
        tmp_path / "ignored",
        lab,
        platform,
        limit=4,
        resume=stopped,
        driver_factory=lambda: opened.append(2),
        profile=tmp_path / "ignored-profile",
    )
    assert again == stopped
    assert opened == [1]
    assert _count(factory, Company) == 2
    _assert_local(lab)


@pytest.mark.parametrize("platform", ["yandex", "google"])
def test_browser_restart_keeps_committed_rows(factory, lab, tmp_path: Path, platform: str) -> None:
    def crash() -> None:
        raise WebDriverException("tab crashed")

    def crashing() -> _Gate:
        return _Gate(_chrome(lab["binary"], tmp_path / "crash-profile"), after=3, action=crash)

    with pytest.raises(WebDriverException, match="tab crashed"):
        _run(
            factory,
            tmp_path / "crash",
            lab,
            platform,
            limit=6,
            driver_factory=crashing,
            profile=tmp_path / "crash-profile",
        )
    with factory() as db:
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"
        assert len((batch.progress or {}).get("seen_ids") or []) == 2
        crashed = batch.id
        assert _count(factory, Company) == 2
        assert _count(factory, CardSnapshot) == 2
    _run(factory, tmp_path / "after-crash", lab, platform, limit=6, resume=crashed, profile=tmp_path / "after-profile")
    progress, status = _progress(factory, crashed)
    assert status == "completed"
    assert len(progress["seen_ids"]) == 6
    assert _count(factory, Company) == 6
    assert _count(factory, CardSnapshot) == 6
    assert progress["metrics"]["browser_restarts"] >= 1
    _assert_local(lab)


def test_captcha_stops_the_batch_and_closes_chrome(factory, lab, tmp_path: Path) -> None:
    holder: dict[str, _Gate] = {}

    def opening() -> _Gate:
        gate = _Gate(_chrome(lab["binary"], tmp_path / "captcha-profile"), after=99, action=lambda: None)
        holder["gate"] = gate
        return gate

    batch_id = _run(
        factory,
        tmp_path,
        lab,
        "yandex",
        category="капча",
        limit=3,
        driver_factory=opening,
        profile=tmp_path / "captcha-profile",
    )
    progress, status = _progress(factory, batch_id)
    assert status == "interrupted"
    assert progress.get("manual_verification", {}).get("outcome") == "unavailable"
    assert "недоступна" in str(progress.get("error") or "")
    assert progress.get("interruption", {}).get("classification") == "captcha"
    assert progress.get("interruption", {}).get("retryable") is False
    assert _count(factory, Company) == 0
    assert holder["gate"].closed is True
    calls: list[str] = []
    again = _run(
        factory,
        tmp_path / "no-resume",
        lab,
        "yandex",
        category="капча",
        limit=3,
        resume=batch_id,
        driver_factory=lambda: calls.append("chrome"),
        profile=tmp_path / "no-resume-profile",
    )
    assert again == batch_id
    assert calls == []
    _assert_local(lab)
