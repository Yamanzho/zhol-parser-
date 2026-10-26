"""Release-blocker checks for map completion, captcha state, partial status, and Compose.

These tests use an isolated database and loopback fixtures. They do not open
Yandex, Google, or 2GIS, and they do not change a running application database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from selenium.common.exceptions import WebDriverException
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.browser_2gis import GRANT_LOCAL
from zhol.agents.scout.localities import google_search_text
from zhol.agents.scout.maps_selenium import _discover, run_maps_batch
from zhol.agents.scout.selenium_collector import VERIFY_PASSED
from zhol.api.admin_data import collection_display_status
from zhol.api.admin_jobs import AdminJobError, resume_batch, signal_batch
from zhol.api.main import app
from zhol.browser.google import selenium as google_selenium
from zhol.browser.yandex import selenium as yandex_selenium
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company
from zhol.runtime_config import DeploymentConfigError, validate_process_environment

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
ORIGIN = "http://127.0.0.1:9"


class _Pages:
    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.url = ""
        self.opened: list[str] = []
        self._zhol_headed = False
        self.quit_called = False
        self.cleared = False

    def set_page_load_timeout(self, _seconds: float) -> None:
        return None

    def get(self, url: str) -> None:
        if "yandex." in url or "google." in url or "2gis." in url:
            raise AssertionError(url)
        self.opened.append(url)
        self.url = url

    @property
    def current_url(self) -> str:
        return self.url

    def execute_script(self, script: str, *_args: object):
        text = script or ""
        page = self.pages.get(self.url, {})
        if "zhol-card-extract" in text:
            return page.get("card")
        if "scrollTop" in text:
            return True
        if text.startswith("return !!"):
            return True
        if "pageNumbers" in text or 'role="feed"' in text:
            return page.get("search", {"items": [], "pageNumbers": [], "duplicates": 0, "done": False})
        if "data-access" in text:
            if page.get("captcha") and not self.cleared:
                return {"text": "Подтвердите, что вы не робот", "access": "captcha"}
            if page.get("restricted"):
                return {"text": "Доступ ограничен", "access": "denied"}
            return {"text": "Салон красоты Алматы", "access": ""}
        return None

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", ORIGIN)
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_YANDEX_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_GOOGLE_BROWSER_GRANT", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _search(platform: str, query: str, page: int = 1, *, city: str = "Алматы") -> str:
    adapter = yandex_selenium if platform == "yandex" else google_selenium
    looked = google_search_text(query, city) if platform == "google" else query
    return adapter.search_page_url(ORIGIN, looked, page, city=city)


def _run(factory, folder: Path, platform: str, driver, **kwargs) -> int:
    return run_maps_batch(
        factory,
        platform=platform,
        city="Алматы",
        category=kwargs.pop("category", "Салон красоты"),
        limit=kwargs.pop("limit"),
        out=folder / "out.txt",
        driver_factory=lambda: driver,
        run_dir=folder / "run",
        delay_range=(0.0, 0.0),
        resume=kwargs.pop("resume", None),
        on_open=kwargs.pop("on_open", None),
        operator_sleep=kwargs.pop("operator_sleep", None),
        verification_wait_seconds=kwargs.pop("verification_wait_seconds", 1),
    )


def test_yandex_two_local_pages_are_both_read_and_one_page_without_an_end_is_not_complete() -> None:
    first = _search("yandex", "две", 1)
    second = _search("yandex", "две", 2)
    driver = _Pages(
        {
            first: {"search": {"items": [{"externalId": "101", "href": "/maps/org/salon/101/", "listName": "Один"}], "pageNumbers": [2], "duplicates": 0, "done": False}},
            second: {"search": {"items": [{"externalId": "102", "href": "/maps/org/salon/102/", "listName": "Два"}], "pageNumbers": [], "duplicates": 0, "done": False}},
        }
    )
    found = _discover(
        driver,
        yandex_selenium,
        origin=ORIGIN,
        platform="yandex",
        query="две",
        limit=5,
        known=set(),
        reserved=set(),
        stopped=lambda: False,
    )
    assert [item["external_id"] for item in found["items"]] == ["101", "102"]
    assert found["pages_loaded"] == 2
    assert found["stop_reason"] == "confirmed_end"
    assert found["complete"] is True
    assert all(url.startswith(ORIGIN) for url in driver.opened)

    only = _search("yandex", "обрыв", 1)
    open_driver = _Pages({only: {"search": {"items": [{"externalId": "201", "href": "/maps/org/salon/201/", "listName": "Обрыв"}], "pageNumbers": [], "duplicates": 0, "done": False}}})
    stalled = _discover(
        open_driver,
        yandex_selenium,
        origin=ORIGIN,
        platform="yandex",
        query="обрыв",
        limit=5,
        known=set(),
        reserved=set(),
        stopped=lambda: False,
    )
    assert stalled["pages_loaded"] == 1
    assert stalled["stop_reason"] == "stall"
    assert stalled["complete"] is False
    assert len(open_driver.opened) == 1

    ended = _search("yandex", "конец", 1)
    end_driver = _Pages({ended: {"search": {"items": [{"externalId": "301", "href": "/maps/org/salon/301/", "listName": "Конец"}], "pageNumbers": [], "duplicates": 0, "done": True}}})
    complete = _discover(
        end_driver,
        yandex_selenium,
        origin=ORIGIN,
        platform="yandex",
        query="конец",
        limit=5,
        known=set(),
        reserved=set(),
        stopped=lambda: False,
    )
    assert complete["stop_reason"] == "confirmed_end"
    assert complete["pages_loaded"] == 1


def test_saved_yandex_reader_does_not_treat_a_missing_pager_as_the_end() -> None:
    script = yandex_selenium.READ_SEARCH_JS
    assert "a.search-pagination__page[href]" in script
    assert "data-results-complete" in script
    assert "done: false" in script
    source = (ROOT / "tests" / "fixtures" / "maps-selenium" / "server.py").read_text(encoding="utf-8")
    assert 'data-results-complete="1"' in source
    assert "лента-застой" in source


def test_google_stall_is_interrupted_and_resume_does_not_duplicate(factory, tmp_path: Path) -> None:
    query = "лента"
    search = _search("google", query)
    card = ORIGIN + "/maps/place/data=!1sChIJAlmatySalon01"
    driver = _Pages(
        {
            search: {
                "search": {
                    "items": [{"externalId": "ChIJAlmatySalon01", "href": "/maps/place/data=!1sChIJAlmatySalon01", "listName": "Салон"}],
                    "duplicates": 0,
                    "done": False,
                    "end": "open",
                }
            },
            card: {
                "card": {
                    "name": "Салон Алматы",
                    "address": "Алматы, улица Абая, 1",
                    "phones": [],
                    "text": "Салон Алматы",
                    "hrefs": [],
                    "rating": "4.6",
                    "reviews": "12",
                    "category": "Салон красоты",
                    "website": "",
                }
            },
        }
    )
    batch_id = _run(factory, tmp_path, "google", driver, category=query, limit=5)
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "interrupted"
        assert batch.progress["stop_reason"] == "stall"
        assert batch.progress["link_collection_complete"] is False
        assert db.query(Company).count() == 1
        assert db.query(CardSnapshot).count() == 1
        checkpoint = batch.progress["discovery_checkpoint"]
    again = _Pages(driver.pages)
    _run(factory, tmp_path / "resume", "google", again, category=query, limit=5, resume=batch_id)
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "interrupted"
        assert db.query(Company).count() == 1
        assert db.query(CardSnapshot).count() == 1
        assert batch.progress["discovery_checkpoint"]["stop_reason"] == "stall"
    assert checkpoint["stop_reason"] == "stall"
    assert "if (!feed) return {items: [], duplicates: 0, done: true}" not in google_selenium.READ_SEARCH_JS
    assert "data-feed-end" in google_selenium.READ_SEARCH_JS


def test_google_confirmed_end_and_limit_are_distinct(factory, tmp_path: Path) -> None:
    ended = _search("google", "конец")
    end_driver = _Pages({ended: {"search": {"items": [], "duplicates": 0, "done": True, "end": "confirmed"}}})
    end_id = _run(factory, tmp_path / "end", "google", end_driver, category="конец", limit=3)
    with factory() as db:
        batch = db.get(CollectionBatch, end_id)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.progress["stop_reason"] == "confirmed_end"
        assert batch.progress["link_collection_complete"] is True

    limited = _search("google", "лимит")
    card = ORIGIN + "/maps/place/data=!1sChIJAlmatySalon01"
    limit_driver = _Pages(
        {
            limited: {
                "search": {
                    "items": [
                        {"externalId": "ChIJAlmatySalon01", "href": "/maps/place/data=!1sChIJAlmatySalon01", "listName": "Салон"},
                        {"externalId": "ChIJAlmatySalon02", "href": "/maps/place/data=!1sChIJAlmatySalon02", "listName": "Второй"},
                    ],
                    "duplicates": 0,
                    "done": False,
                }
            },
            card: {
                "card": {
                    "name": "Салон Алматы",
                    "address": "Алматы, улица Абая, 1",
                    "phones": [],
                    "text": "Салон Алматы",
                    "hrefs": [],
                    "rating": "",
                    "reviews": "",
                    "category": "Салон красоты",
                    "website": "",
                }
            },
        }
    )
    limit_id = _run(factory, tmp_path / "limit", "google", limit_driver, category="лимит", limit=1)
    with factory() as db:
        batch = db.get(CollectionBatch, limit_id)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.progress["stop_reason"] == "limit"
        assert batch.progress["link_collection_complete"] is False
        assert db.query(Company).count() == 1
        timings = batch.progress["timings"]
        metrics = batch.progress["metrics"]
    assert timings["persistence_scope"] == "commit"
    assert timings["cards_persisted"] == 1
    assert metrics["persistence_samples"] == 1
    assert metrics["persistence_ms_total"] == pytest.approx(timings["persistence_ms"], abs=0.01)
    assert "link_discovery_ms" not in timings
    with TestClient(app) as client:
        ended_view = client.get(f"/api/v1/admin/scout/batches/{end_id}", headers=HEADERS).json()
        limit_view = client.get(f"/api/v1/admin/scout/batches/{limit_id}", headers=HEADERS).json()
    assert ended_view["display_status"] == "completed"
    assert ended_view["result"]["search_exhausted"] is True
    assert limit_view["display_status"] == "limit_reached"
    assert limit_view["result"]["search_exhausted"] is False
    assert limit_view["result"]["warnings"] == []
    assert limit_view["collection_mode"]["code"] == "local"
    assert limit_view["timings"]["average_persistence_ms"] >= 0


def test_headless_captcha_is_unavailable_and_does_not_resume(factory, tmp_path: Path) -> None:
    search = _search("yandex", "капча")
    driver = _Pages({search: {"captcha": True, "search": {"items": [], "pageNumbers": [], "duplicates": 0, "done": False}}})
    batch_id = _run(factory, tmp_path, "yandex", driver, category="капча", limit=2)
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "interrupted"
        record = batch.progress["manual_verification"]
        assert record["outcome"] == "unavailable"
        assert "недоступна" in record["message"]
        assert batch.progress["interruption"]["retryable"] is False
    assert driver.quit_called is True
    calls: list[object] = []
    again = run_maps_batch(
        factory,
        platform="yandex",
        city="Алматы",
        category="капча",
        limit=2,
        out=tmp_path / "resume.txt",
        resume=batch_id,
        driver_factory=lambda: calls.append("opened"),
        run_dir=tmp_path / "resume-run",
    )
    assert again == batch_id
    assert calls == []
    with factory() as session:
        with pytest.raises(AdminJobError, match="недоступна"):
            resume_batch(session, batch_id)


def test_headed_captcha_waits_on_the_same_session(factory, tmp_path: Path) -> None:
    search = _search("yandex", "проверка")
    driver = _Pages({search: {"captcha": True, "search": {"items": [], "pageNumbers": [], "duplicates": 0, "done": True}}})
    driver._zhol_headed = True
    holder: dict[str, int] = {}

    def sleep(_seconds: float) -> None:
        assert driver.quit_called is False
        driver.cleared = True
        with factory() as session, session.begin():
            signal_batch(session, holder["id"], VERIFY_PASSED)

    batch_id = _run(
        factory,
        tmp_path,
        "yandex",
        driver,
        category="проверка",
        limit=1,
        on_open=lambda batch: holder.__setitem__("id", batch),
        operator_sleep=sleep,
        verification_wait_seconds=5,
    )
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.progress["manual_verification"]["outcome"] == "passed"
        assert batch.progress["stop_reason"] == "confirmed_end"
    assert driver.opened
    assert all(url.startswith(ORIGIN) for url in driver.opened)
    assert driver.quit_called is True


def test_access_restriction_and_browser_failure_are_not_success(factory, tmp_path: Path) -> None:
    search = _search("google", "запрет")
    restricted = _Pages({search: {"restricted": True}})
    blocked = _run(factory, tmp_path / "blocked", "google", restricted, category="запрет", limit=2)
    with factory() as db:
        batch = db.get(CollectionBatch, blocked)
        assert batch is not None
        assert batch.status == "interrupted"
        assert batch.progress["interruption"]["classification"] == "access_restriction"
        assert batch.progress["interruption"]["retryable"] is False

    class _Crash(_Pages):
        def get(self, url: str) -> None:
            raise WebDriverException("tab crashed")

    with pytest.raises(WebDriverException, match="tab crashed"):
        _run(factory, tmp_path / "crash", "google", _Crash({}), category="обрыв", limit=2)
    with factory() as db:
        batch = db.query(CollectionBatch).filter(CollectionBatch.category == "обрыв").one()
        assert batch.status == "interrupted"
        assert batch.progress["interruption"]["classification"] == "browser_session"
        assert batch.progress["interruption"]["retryable"] is True


def test_partial_batch_is_not_labeled_completed(factory) -> None:
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city="Алматы",
            category="Салон",
            platforms=["google"],
            item_limit=2,
            source_mode="browser_google",
            status="completed",
            out_path="/tmp/zhol-partial-out",
            catalog_path="browser_google",
            progress={
                "metrics": {"failed_records": 1, "companies_committed": 1},
                "link_collection_complete": True,
                "stop_reason": "confirmed_end",
            },
        )
        session.add(batch)
        session.flush()
        session.add(
            CollectionBatchItem(
                batch_id=batch.id,
                id_2gis="ChIJPartial",
                status="blocked",
                attempts=1,
                detail={"reason": "invalid", "classification": "invalid_card", "retryable": False},
            )
        )
        batch_id = batch.id
    with TestClient(app) as client:
        body = client.get(f"/api/v1/admin/scout/batches/{batch_id}", headers=HEADERS)
    assert body.status_code == 200
    payload = body.json()
    assert payload["status"] == "completed"
    assert payload["display_status"] == "partial"
    assert payload["result"]["label"] == "Частичный результат"
    assert payload["result"]["warnings"] == ["Не прочитано карточек: 1."]
    assert payload["card_states"]["rejected"] == 1
    assert collection_display_status("stopped", {}, {}) == "stopped"
    assert collection_display_status("interrupted", {}, {}) == "interrupted"
    assert collection_display_status("failed", {}, {}) == "failed"
    assert collection_display_status("completed", {"link_collection_complete": True, "stop_reason": "confirmed_end"}, {"rejected": 0}) == "completed"


def test_partial_enqueue_is_not_a_full_success(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.setenv("ZHOL_BROWSER_ENGINE", "1")
    monkeypatch.setenv("ZHOL_2GIS_BROWSER_GRANT", GRANT_LOCAL)
    monkeypatch.delenv("ZHOL_GOOGLE_BROWSER_GRANT", raising=False)
    page = (ROOT / "web" / "src" / "collect.tsx").read_text(encoding="utf-8")
    assert "if (problems.length > 0)" in page
    assert "throw new Error" in page
    assert "completed_with_warnings" in page
    assert 'className="ok"' not in page.split("problems.length > 0", 1)[1].split("return lead", 1)[0]
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Салон красоты"],
                "platforms": ["2gis", "google"],
                "limit": 5,
                "source_mode": "browser_2gis",
                "source_modes": ["browser_2gis", "browser_google"],
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["jobs"]
    assert body["errors"]
    assert body["outcome"] == "completed_with_warnings"


def test_process_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DeploymentConfigError, match="DATABASE_URL"):
        validate_process_environment()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://collector:secret@127.0.0.1:1/zhol")
    validate_process_environment()


def test_compose_config_rejects_missing_database_password(tmp_path: Path) -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert ":-zhol" not in compose
    assert "written-browser-retention-local}" not in compose
    assert compose.count("${ZHOL_2GIS_BROWSER_GRANT:-}") == 2
    target = tmp_path / "docker-compose.yml"
    shutil.copy(ROOT / "docker-compose.yml", target)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("POSTGRES_", "ZHOL_", "COMPOSE_")) and key != "DATABASE_URL"}
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(tmp_path), "config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "POSTGRES_PASSWORD" in result.stderr
