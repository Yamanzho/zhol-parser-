"""Browser collection stays closed. These tests do not open a browser or a website."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.browser_2gis import BLOCKER, BrowserCollectionRefused, refuse_browser_collection
from zhol.agents.scout.setup import collection_setup
from zhol.api.admin_jobs import handle_scout_batch
from zhol.api.main import app
from zhol.db.models import CollectionBatch, Company, Job
from zhol.guards import saved_discovery_only

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
CATALOG = ROOT / "tests" / "fixtures" / "catalog.yaml"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}


def test_browser_mode_is_hidden_until_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_ENGINE", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_HTML_ROOT", raising=False)
    monkeypatch.delenv("ZHOL_CHROME_CONTAINER", raising=False)
    monkeypatch.delenv("ZHOL_CHROME_BINARY", raising=False)
    body = collection_setup(CATALOG)
    modes = {item["id"]: item for item in body["collection_modes"]}
    assert modes["browser_2gis"]["available"] is False
    assert modes["browser_2gis"]["kind"] == "browser"
    assert "Алматы" in modes["browser_2gis"]["cities"]
    assert body["browser_sources"][0] == modes["browser_2gis"]
    assert [item["id"] for item in body["browser_sources"]] == [
        "browser_2gis",
        "browser_yandex",
        "browser_google",
    ]
    assert modes["browser_yandex"]["available"] is False
    assert modes["browser_google"]["available"] is False
    assert "выключен" in body["browser_note"]
    assert "не подтверждено" in body["browser_note"]
    assert modes["fixture"]["available"] is True
    assert modes["fixture"]["label"] == "Тестовый каталог"
    assert modes["live_2gis"]["label"] == "Официальный Catalog API, 2ГИС"
    source = (ROOT / "zhol" / "agents" / "scout" / "browser_2gis.py").read_text(encoding="utf-8")
    assert "selenium" not in source
    assert "webdriver" not in source
    assert "urlopen" not in source
    with pytest.raises(BrowserCollectionRefused, match="выключен"):
        refuse_browser_collection()
    with pytest.raises(PermissionError, match="not permitted"):
        saved_discovery_only("live", "2gis")


def test_collect_page_hides_an_unauthorized_browser_method() -> None:
    page = (ROOT / "web" / "src" / "collect.tsx").read_text(encoding="utf-8")
    assert 'name="collection-mode"' in page
    assert "Начать сбор" in page
    assert "selected.available" in page
    assert "disabled={!item.available}" in page
    assert 'kind !== "browser" || item.available' not in page
    assert 'source_mode: live || browser ? mode : "fixture"' in page
    assert "Новые компании" in page
    assert "Уже были в базе" in page
    assert "Ошибки" in page
    assert "Текущий запрос" in page
    assert "Остановить сбор" in page
    assert "Продолжить сбор" in page
    assert "Живой сбор: нет." in page
    assert "Живой сбор не выполнялся." in page
    assert "browser_note" in page
    assert "Живой сбор: доступен." in page
    assert "Живой сбор: недоступен." in page
    assert "live_note" in page
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("ZHOL_2GIS_BROWSER_GRANT: ${ZHOL_2GIS_BROWSER_GRANT:-}") == 2
    assert "POSTGRES_PASSWORD:-zhol" not in compose
    assert "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}" in compose
    assert compose.count("ZHOL_YANDEX_BROWSER_GRANT: ${ZHOL_YANDEX_BROWSER_GRANT:-}") == 2
    assert compose.count("ZHOL_GOOGLE_BROWSER_GRANT: ${ZHOL_GOOGLE_BROWSER_GRANT:-}") == 2
    assert "ZHOL_GOOGLE_BROWSER_GRANT:-written-browser-retention-local" not in compose
    assert "written-browser-retention-commercial" not in compose


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def test_api_rejects_browser_collection_before_a_job(factory) -> None:
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Косметология"],
                "platforms": ["2gis"],
                "limit": 20,
                "source_mode": "browser_2gis",
            },
        )
    assert denied.status_code == 400
    assert denied.json()["detail"] == BLOCKER
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0


def test_worker_refuses_browser_collection_without_opening_a_browser(factory) -> None:
    payload = {
        "source_mode": "browser_2gis",
        "city": "Алматы",
        "category": "Косметология",
        "platforms": ["2gis"],
        "limit": 20,
        "dedup_key": "browser-refused",
        "out": "",
    }
    with factory() as db, db.begin():
        db.add(
            Job(
                type="admin.scout_batch",
                queue="main",
                status="running",
                attempts=1,
                payload=payload,
                dedup_key="browser-refused",
            )
        )
    with pytest.raises(BrowserCollectionRefused):
        handle_scout_batch(factory, payload)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0
