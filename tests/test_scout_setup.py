"""Visual collection setup maps onto the saved catalog. It does not open a map site."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.setup import SetupError, build_plans, collection_setup
from zhol.api.main import app
from zhol.db.models import CollectionBatch, CollectionBatchItem, Company, Job
from zhol.timeutil import ALMATY

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests" / "fixtures" / "catalog.yaml"
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}


def test_setup_lists_catalog_cities_and_only_enables_2gis() -> None:
    body = collection_setup(CATALOG)
    assert body["ready"] is True
    assert body["live_collection"] is False
    assert body["default_city"] == "Алматы"
    assert body["cities"] == ["Алматы", "Астана"]
    labels = {item["label"]: item for item in body["categories"]}
    assert labels["Салон красоты"]["counts"]["Алматы"] == 10
    assert labels["Парикмахерская"]["counts"]["Алматы"] == 1
    assert labels["Ресторан"]["counts"]["Астана"] == 2
    assert labels["Ресторан"]["counts"]["Алматы"] == 0
    assert labels["Стоматология"]["counts"]["Алматы"] == 0
    assert "Beauty salon" not in labels
    sources = {item["id"]: item for item in body["sources"]}
    assert sources["2gis"]["available"] is True
    assert sources["yandex"]["available"] is False
    assert sources["yandex"]["collection_state"] == "unauthorized"
    assert sources["yandex"]["live_available"] is False
    assert "некоммерческ" in sources["yandex"]["live_note"]
    assert "сохранённый HTML" not in sources["yandex"]["live_note"]
    assert "2ГИС" not in sources["yandex"]["live_note"]
    assert sources["google"]["available"] is False
    assert sources["google"]["collection_state"] == "unauthorized"
    assert sources["google"]["live_available"] is False
    assert "No Scraping" in sources["google"]["live_note"]
    assert "сохранённый HTML" not in sources["google"]["live_note"]
    assert "2ГИС" not in sources["google"]["live_note"]
    assert body["delay"]["supported"] is False
    assert body["stop"]["supported"] is True
    assert body["resume"]["supported"] is True
    assert "catalog.yaml" not in str(body)


def test_map_checkboxes_follow_each_source_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    from zhol.agents.scout.browser_google import browser_mode as google_mode
    from zhol.agents.scout.browser_maps import GRANT_LOCAL

    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_HTML_ROOT", raising=False)
    monkeypatch.setenv("ZHOL_BROWSER_ENGINE", "1")
    monkeypatch.setenv("ZHOL_GOOGLE_BROWSER_GRANT", GRANT_LOCAL)
    monkeypatch.delenv("ZHOL_YANDEX_BROWSER_GRANT", raising=False)
    sources = {item["id"]: item for item in collection_setup(CATALOG)["sources"]}
    assert sources["2gis"]["available"] is True
    assert sources["google"]["available"] is False
    assert sources["google"]["collection_state"] == "available"
    assert sources["google"]["live_available"] is True
    assert sources["yandex"]["collection_state"] == "unauthorized"
    assert "429" in sources["yandex"]["limit_note"]
    monkeypatch.setenv("ZHOL_BROWSER_ENGINE", "0")
    restricted = google_mode()
    assert restricted["available"] is False
    assert restricted["authorization_scope"] == "local"
    assert restricted["collection_state"] == "restricted"


def test_plans_use_the_existing_fixture_contract() -> None:
    plans = build_plans(
        city="алматы",
        categories=["Салон красоты", "Парикмахерская"],
        queries=["Салон красоты Алматы", "Beauty salon"],
        platforms=["2gis"],
        limit=20,
        catalog_file=CATALOG,
    )
    assert plans == [
        {
            "source_mode": "fixture",
            "city": "Алматы",
            "category": "Салон красоты",
            "platforms": ["2gis"],
            "limit": 20,
            "catalog": "catalog.yaml",
        },
        {
            "source_mode": "fixture",
            "city": "Алматы",
            "category": "Парикмахерская",
            "platforms": ["2gis"],
            "limit": 20,
            "catalog": "catalog.yaml",
        },
    ]


@pytest.mark.parametrize(
    ("city", "categories", "queries", "platforms", "message"),
    [
        ("Шымкент", ["Салон красоты"], [], ["2gis"], "не входит"),
        ("Алматы", [], [], ["2gis"], "хотя бы одну категорию"),
        ("Алматы", ["Салон красоты"], [], ["yandex"], "недоступен"),
        ("Алматы", ["Салон красоты"], [], ["google"], "недоступен"),
        ("Алматы", ["Косметология"], [], ["2gis"], "не поддерживается"),
        ("Алматы", [], ["Детские стоматологии"], ["2gis"], "не поддерживается"),
        ("Алматы", [], ["Косметология Алматы"], ["2gis"], "не поддерживается"),
        ("Алматы", ["Стоматология"], [], ["2gis"], "нет компаний"),
        ("Алматы", ["Ресторан"], [], ["2gis"], "нет компаний"),
        ("Алматы", [], ["Алматы"], ["2gis"], "только из названия города"),
    ],
)
def test_invalid_collection_is_rejected(city: str, categories: list[str], queries: list[str], platforms: list[str], message: str) -> None:
    with pytest.raises(SetupError, match=message):
        build_plans(
            city=city,
            categories=categories,
            queries=queries,
            platforms=platforms,
            limit=20,
            catalog_file=CATALOG,
        )


def test_collection_page_does_not_ask_for_yaml() -> None:
    page = (ROOT / "web" / "src" / "collect.tsx").read_text(encoding="utf-8")
    form = (ROOT / "web" / "src" / "pages.tsx").read_text(encoding="utf-8")
    assert "Начать сбор" in page
    assert "Дополнительные поисковые запросы" in page
    assert "Остановить сбор" in page
    assert "Продолжить сбор" in page
    assert 'placeholder="catalog.yaml"' not in page
    assert 'placeholder="catalog.yaml"' not in form
    assert "sessionStorage" in page


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with TestClient(app) as test_client:
        yield test_client, factory


def test_collect_enqueues_the_existing_job_without_inserting_companies(client) -> None:
    test_client, factory = client
    before = _counts(factory)
    denied = test_client.post(
        "/api/v1/admin/scout/collect",
        headers=HEADERS,
        json={"city": "Алматы", "categories": ["Салон красоты"], "queries": ["Детские стоматологии"], "platforms": ["2gis"], "limit": 5},
    )
    assert denied.status_code == 400
    assert "не поддерживается" in denied.json()["detail"]
    blocked = test_client.post(
        "/api/v1/admin/scout/collect",
        headers=HEADERS,
        json={"city": "Алматы", "categories": ["Салон красоты"], "platforms": ["yandex"], "limit": 5},
    )
    assert blocked.status_code == 400
    started = test_client.post(
        "/api/v1/admin/scout/collect",
        headers=HEADERS,
        json={
            "city": "Алматы",
            "categories": ["Салон красоты"],
            "queries": ["Парикмахерская"],
            "platforms": ["2gis"],
            "limit": 5,
        },
    )
    assert started.status_code == 200
    body = started.json()
    assert body["live_mass_discovery"] is False
    assert body["categories"] == ["Салон красоты", "Парикмахерская"]
    assert body["query_count"] == 2
    assert len(body["jobs"]) == 2
    assert all(job["source_mode"] == "fixture" for job in body["jobs"])
    assert all(job["status"] == "queued" for job in body["jobs"])
    again = test_client.post(
        "/api/v1/admin/scout/collect",
        headers=HEADERS,
        json={"city": "Алматы", "categories": ["Салон красоты"], "platforms": ["2gis"], "limit": 5},
    )
    assert again.status_code == 200
    assert again.json()["jobs"][0]["duplicate"] is True
    assert again.json()["jobs"][0]["id"] == body["jobs"][0]["id"]
    with factory() as db:
        jobs = db.scalars(select(Job).where(Job.type == "admin.scout_batch")).all()
        assert len(jobs) == 2
        for job in jobs:
            payload = job.payload or {}
            assert payload["source_mode"] == "fixture"
            assert payload["platforms"] == ["2gis"]
            assert str(payload["catalog"]).endswith("catalog.yaml")
            assert payload["city"] == "Алматы"
            assert "companies" not in payload
    assert _counts(factory) == before


def test_batch_outcome_counts_new_and_existing_companies(client) -> None:
    _test_client, factory = client
    started = datetime(2026, 10, 1, 12, 0, tzinfo=ALMATY)
    with factory() as db, db.begin():
        old = Company(id_2gis="setup-old", name="Старая", city="Алматы", source="test", created_at=started - timedelta(days=3))
        batch = CollectionBatch(
            city="Алматы",
            category="Салон красоты",
            platforms=["2gis"],
            source_mode="fixture",
            status="completed",
            out_path="batch-setup.xlsx",
            catalog_path="catalog.yaml",
            created_at=started,
            progress={"duplicates": 2, "skipped": [{"firm_id": "x", "reason": "reference"}]},
        )
        db.add_all([old, batch])
        db.flush()
        fresh = Company(id_2gis="setup-new", name="Новая", city="Алматы", source="test", created_at=started + timedelta(minutes=5))
        db.add(fresh)
        db.flush()
        db.add(CollectionBatchItem(batch_id=batch.id, id_2gis=old.id_2gis, company_id=old.id, status="completed"))
        db.add(CollectionBatchItem(batch_id=batch.id, id_2gis=fresh.id_2gis, company_id=fresh.id, status="failed"))
        batch_id = batch.id
    listed = _test_client.get(f"/api/v1/admin/scout/batches/{batch_id}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert listed.status_code == 200
    outcome = listed.json()["outcome"]
    assert outcome["discovered"] == 2
    assert outcome["added"] == 1
    assert outcome["already_present"] == 1
    assert outcome["failed"] == 1
    assert outcome["duplicates"] == 2
    assert outcome["skipped"] == 1
    assert _counts(factory)["companies"] == 2


def _counts(factory: sessionmaker) -> dict[str, int]:
    with factory() as db:
        return {
            "companies": int(db.scalar(select(func.count()).select_from(Company)) or 0),
            "batches": int(db.scalar(select(func.count()).select_from(CollectionBatch)) or 0),
        }
