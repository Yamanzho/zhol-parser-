"""City, category and collection-plan checks. They do not open a map site."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.collection_plan import (
    active_categories,
    build_collection_plan,
    category_groups,
    normalize_queries,
)
from zhol.agents.scout.localities import all_localities, google_search_text, supports, twogis_slug, yandex_place
from zhol.agents.scout.plan_run import run_source_plan
from zhol.api.main import app
from zhol.browser.profile_gate import exclusive_chrome_profile
from zhol.db.models import CollectionBatch, Job

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
READY = {"2gis": True, "google": True, "yandex": True}
NOTES = {"yandex": "HTTP 429 останавливает сбор и не считается успехом."}


def _plan(**kwargs):
    base = dict(
        cities=["Алматы"],
        all_cities=False,
        categories=["Салон красоты"],
        queries=[],
        all_categories=False,
        platforms=["2gis"],
        limit=5,
        combination_limit=None,
        confirm=False,
        skip_unsupported=False,
        ready=READY,
        blocked_note=NOTES,
    )
    base.update(kwargs)
    return build_collection_plan(**base)


def test_single_city_selection() -> None:
    built = _plan(cities=["алма-ата"])
    combo = built["plans"][0]["combinations"]
    assert [item["city"] for item in combo] == ["Алматы"]
    assert combo[0]["city_id"] == "almaty"


def test_multiple_cities_stay_separate() -> None:
    built = _plan(cities=["Алматы", "Астана"], confirm=True)
    assert [item["city"] for item in built["plans"][0]["combinations"]] == ["Алматы", "Астана"]


def test_all_supported_cities_mean_the_local_catalog() -> None:
    built = _plan(cities=[], all_cities=True, confirm=True, skip_unsupported=True, platforms=["google"])
    names = [item["city"] for item in built["plans"][0]["combinations"]]
    assert names == [item.name for item in all_localities()]
    assert len(all_localities()) >= 30
    assert built["preview"]["all_cities_meaning"] == "every validated city in the local catalog"
    assert built["preview"]["estimated_upper_bound"] == 5


def test_unsupported_city_is_not_enqueued_and_not_rewritten_to_almaty() -> None:
    with pytest.raises(ValueError, match="не переключается на Алматы"):
        twogis_slug("Конаев")
    with pytest.raises(ValueError, match="не переключается на Алматы"):
        yandex_place("Астана")
    with pytest.raises(Exception, match="неподдержан"):
        _plan(cities=["Конаев"], platforms=["2gis"])
    built = _plan(cities=["Конаев", "Алматы"], platforms=["2gis"], skip_unsupported=True)
    assert [item["city"] for item in built["plans"][0]["combinations"]] == ["Алматы"]
    assert built["skipped"][0]["city"] == "Конаев"


def test_single_and_multiple_categories() -> None:
    one = _plan(categories=["Парикмахерская"])
    assert one["plans"][0]["combinations"][0]["query"] == "Парикмахерская"
    many = _plan(categories=["Салон красоты", "Ресторан"])
    assert [item["query"] for item in many["plans"][0]["combinations"]] == ["Салон красоты", "Ресторан"]


def test_all_configured_categories_are_the_catalog_not_the_word_all() -> None:
    rows = active_categories()
    groups = category_groups(rows)
    assert len(rows) == 7
    assert {item["sector"] for item in groups} == {"Красота", "Спорт", "Питание", "Медицина"}
    built = _plan(categories=[], all_categories=True, confirm=True)
    phrases = [item["query"] for item in built["plans"][0]["combinations"]]
    assert "Все" not in phrases
    assert phrases == [row["label"] for row in rows]


def test_manual_queries_drop_duplicates() -> None:
    assert normalize_queries(["  Салон   красоты ", "салон красоты", "Барбершоп", ""]) == ["Салон красоты", "Барбершоп"]
    built = _plan(categories=[], queries=["Кофейня", "кофейня", "  Кофейня  "])
    assert [item["query"] for item in built["plans"][0]["combinations"]] == ["Кофейня"]
    assert built["plans"][0]["combinations"][0]["kind"] == "query"


def test_two_sources_keep_one_cap_each() -> None:
    built = _plan(
        cities=["Алматы", "Астана"],
        platforms=["2gis", "google"],
        categories=["Салон красоты", "Ресторан"],
        confirm=True,
    )
    assert [plan["platform"] for plan in built["plans"]] == ["2gis", "google"]
    assert all(plan["source_cap"] == 5 for plan in built["plans"])
    assert all(len(plan["combinations"]) == 4 for plan in built["plans"])
    assert built["preview"]["estimated_upper_bound"] == 10


def test_yandex_outside_almaty_is_restricted_not_success() -> None:
    assert supports("Алматы", "yandex") is True
    assert supports("Астана", "yandex") is False
    built = _plan(
        cities=["Астана"],
        platforms=["yandex", "google"],
        skip_unsupported=True,
        ready={"2gis": True, "google": True, "yandex": False},
        blocked_note={"yandex": "HTTP 429 не является успешным сбором."},
    )
    assert [plan["platform"] for plan in built["plans"]] == ["google"]
    assert built["errors"][0]["platform"] == "yandex"
    assert "429" in built["errors"][0]["detail"]


def test_large_plan_requires_confirmation() -> None:
    with pytest.raises(Exception, match="подтвердить"):
        _plan(all_cities=True, all_categories=True, cities=[], categories=[], platforms=["google"], skip_unsupported=True)
    built = _plan(
        all_cities=True,
        all_categories=True,
        cities=[],
        categories=[],
        confirm=True,
        skip_unsupported=True,
        platforms=["google"],
    )
    assert built["preview"]["combinations"] == len(all_localities()) * len(active_categories())
    assert built["preview"]["estimated_upper_bound"] == 5


def test_google_search_text_does_not_replace_the_stored_query() -> None:
    assert google_search_text("Салон красоты", "Астана") == "Салон красоты Астана"
    assert google_search_text("Салон красоты Астана", "Астана") == "Салон красоты Астана"


def test_plan_walks_combinations_and_stops_on_pause(migrated_engine) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    calls: list[str] = []

    def run_one(**kwargs):
        calls.append(kwargs["city"])
        with factory() as session, session.begin():
            batch = session.get(CollectionBatch, kwargs["resume"])
            assert batch is not None
            assert batch.item_limit == 5
            batch.status = "paused" if kwargs["city"] == "Алматы" else "running"
        return kwargs["resume"]

    plan = _plan(cities=["Алматы", "Астана"], confirm=True)["plans"][0]
    from zhol.browser.discovery.types import RunControl

    batch_id = run_source_plan(
        factory,
        plan=plan,
        platform="2gis",
        source_mode="browser_2gis",
        limit=5,
        out=Path("/tmp/zhol-plan-test.xlsx"),
        resume=None,
        control=RunControl(),
        on_open=None,
        run_one=run_one,
    )
    assert calls == ["Алматы"]
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        assert batch is not None
        stored = batch.progress["plan"]
        assert stored["combinations"][0]["status"] == "paused"
        assert stored["combinations"][1]["status"] == "pending"
        assert batch.status == "paused"


def test_plan_resume_does_not_repeat_a_finished_combination(migrated_engine) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    seen: list[str] = []

    def run_one(**kwargs):
        seen.append(kwargs["category"])
        with factory() as session, session.begin():
            batch = session.get(CollectionBatch, kwargs["resume"])
            assert batch is not None
            batch.status = "running" if kwargs["hold_completion"] else "completed"
        return kwargs["resume"]

    plan = _plan(categories=["Салон красоты", "Ресторан"])["plans"][0]
    from zhol.browser.discovery.types import RunControl

    batch_id = run_source_plan(
        factory,
        plan=plan,
        platform="2gis",
        source_mode="browser_2gis",
        limit=5,
        out=Path("/tmp/zhol-plan-resume.xlsx"),
        resume=None,
        control=RunControl(),
        on_open=None,
        run_one=run_one,
    )
    assert seen == ["Салон красоты", "Ресторан"]
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "completed"
        assert [item["status"] for item in batch.progress["plan"]["combinations"]] == ["completed", "completed"]


def test_chrome_profile_lock_still_rejects_a_second_opener(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("parser_service.chrome_profile_dir", lambda: tmp_path)
    from zhol.jobs.queue import JobDeferred

    with exclusive_chrome_profile():
        with pytest.raises(JobDeferred):
            with exclusive_chrome_profile():
                pass


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_HTML_ROOT", raising=False)
    monkeypatch.setenv("ZHOL_BROWSER_ENGINE", "1")
    monkeypatch.setenv("ZHOL_2GIS_BROWSER_GRANT", "written-browser-retention-local")
    monkeypatch.setenv("ZHOL_GOOGLE_BROWSER_GRANT", "written-browser-retention-local")
    monkeypatch.delenv("ZHOL_YANDEX_BROWSER_GRANT", raising=False)
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with TestClient(app) as test_client:
        yield test_client, factory


def test_api_enqueues_one_job_per_source_with_the_shared_cap(client) -> None:
    test_client, factory = client
    response = test_client.post(
        "/api/v1/admin/scout/collect",
        headers=HEADERS,
        json={
            "city": "Алматы",
            "cities": ["Алматы", "Астана"],
            "categories": ["Салон красоты"],
            "platforms": ["2gis", "google"],
            "limit": 5,
            "source_mode": "browser_2gis",
            "confirm": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit_scope"] == "per_source"
    assert body["estimated_upper_bound"] == 10
    assert len(body["jobs"]) == 2
    with factory() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 2
        assert {job.queue for job in jobs} == {"collector"}
        for job in jobs:
            assert job.payload["limit"] == 5
            assert len(job.payload["plan"]["combinations"]) == 2
            assert job.payload["plan"]["source_cap"] == 5
