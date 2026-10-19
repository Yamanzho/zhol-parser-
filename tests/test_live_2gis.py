"""Catalog API adapter. These tests never open a socket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.live_2gis import (
    KEY_MISSING,
    CatalogDenied,
    CatalogError,
    HttpCatalogClient,
    build_live_plans,
    firm_from_item,
    run_live_batch,
)
from zhol.api.admin_jobs import handle_scout_batch
from zhol.api.main import app
from zhol.browser.discovery.types import RunControl
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, Job
from zhol.guards import saved_discovery_only
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
FIRM = "7000000100000091"


class ScriptedCatalog:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict) -> dict:
        self.calls.append((path, dict(params)))
        if path.endswith("/region/search"):
            return {"result": {"items": [{"id": "9001", "name": "Алматы", "type": "region", "uri_code": "almaty"}]}}
        page = int(params["page"])
        if page > len(self.pages):
            return {"result": {"total": 0, "items": []}}
        return self.pages[page - 1]


def _item(firm_id: str, name: str) -> dict:
    return {
        "id": firm_id,
        "type": "branch",
        "name": name,
        "address_name": "улица Абая, 10",
        "contact_groups": [
            {
                "contacts": [
                    {"type": "phone", "text": "+7 701 000 00 01", "value": "+77010000001"},
                    {"type": "whatsapp", "value": "+77010000001"},
                    {"type": "instagram", "url": "https://instagram.com/badi.salon"},
                    {"type": "email", "value": "badi@example.com"},
                    {"type": "website", "url": "https://badi.example"},
                ]
            }
        ],
        "reviews": {"general_rating": 4.8, "general_review_count": 12},
        "rubrics": [{"name": "Косметология"}],
        "org": {"branch_count": 1},
    }


def _page(*items: dict, total: int | None = None) -> dict:
    return {"result": {"total": len(items) if total is None else total, "items": list(items)}}


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


def test_missing_fields_stay_null_and_website_extraction_stays_closed() -> None:
    firm = firm_from_item({"id": FIRM, "type": "branch", "name": "Badi"}, city="Алматы", category="Косметология", slug=None)
    assert firm is not None
    assert firm.rating is None
    assert firm.reviews_count is None
    assert firm.phones == ()
    assert firm.contacts_seen is False
    assert firm.category is None
    assert firm.url is None
    with pytest.raises(PermissionError, match="not permitted"):
        saved_discovery_only("live", "2gis")


def test_catalog_item_uses_the_stable_branch_id() -> None:
    firm = firm_from_item(_item(FIRM, "Badi"), city="Алматы", category="Косметология", slug="almaty")
    assert firm is not None
    assert firm.firm_id == FIRM
    assert firm.address == "улица Абая, 10"
    assert firm.phones == ("77010000001",)
    assert firm.phone_display == ("+7 701 000 00 01",)
    assert firm.whatsapp == "77010000001"
    assert firm.instagram == "badi.salon"
    assert firm.email == "badi@example.com"
    assert firm.website == "https://badi.example"
    assert firm.rating == 4.8
    assert firm.reviews_count == 12
    assert firm.url == f"https://2gis.kz/almaty/firm/{FIRM}"


def test_live_plans_require_a_key_and_do_not_use_the_saved_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    with pytest.raises(CatalogDenied, match="DGIS_CATALOG_KEY"):
        build_live_plans(city="Алматы", categories=[], queries=["Косметология"], platforms=["2gis"], limit=20)
    monkeypatch.setenv("DGIS_CATALOG_KEY", "test-key")
    plans = build_live_plans(
        city="Алматы",
        categories=["Стоматология"],
        queries=["Косметология Алматы", "Косметология"],
        platforms=["2gis"],
        limit=20,
    )
    assert plans == [
        {
            "source_mode": "live_2gis",
            "city": "Алматы",
            "category": "Стоматология",
            "platforms": ["2gis"],
            "limit": 20,
            "catalog": "live_2gis",
        },
        {
            "source_mode": "live_2gis",
            "city": "Алматы",
            "category": "Косметология",
            "platforms": ["2gis"],
            "limit": 20,
            "catalog": "live_2gis",
        },
    ]
    with pytest.raises(CatalogError, match="Яндекс"):
        build_live_plans(city="Алматы", categories=[], queries=["Косметология"], platforms=["yandex"], limit=20)


def test_collection_deduplicates_and_keeps_old_snapshots(factory) -> None:
    started = now()
    with factory() as db, db.begin():
        old = Company(id_2gis=FIRM, name="Старая", city="Алматы", source="etalon", created_at=started)
        db.add(old)
        db.flush()
        snapshot = CardSnapshot(
            company_id=old.id,
            platform="2gis",
            match_status="found",
            url="https://2gis.kz/almaty/firm/" + FIRM,
            fields={"name": "Старая"},
            field_sources={},
            purpose="prospect",
            collected_at=started,
            collected_by="scout",
            status="verified",
        )
        db.add(snapshot)
        db.flush()
        old_snapshot = snapshot.id
    catalog = ScriptedCatalog([_page(_item(FIRM, "Новое имя"), _item("7000000100000092", "Новая"))])
    batch_id = run_live_batch(
        factory,
        city="Алматы",
        category="Косметология",
        limit=20,
        out=Path("ignored.xlsx"),
        transport=catalog,
        sleep=lambda _delay: None,
        clock=lambda: started,
    )
    with factory() as db:
        companies = db.scalars(select(Company).order_by(Company.id)).all()
        snapshots = db.scalars(select(CardSnapshot).order_by(CardSnapshot.id)).all()
        batch = db.get(CollectionBatch, batch_id)
        items = db.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
    assert [company.id_2gis for company in companies] == [FIRM, "7000000100000092"]
    assert companies[0].name == "Старая"
    assert companies[0].source == "etalon"
    assert companies[1].source == "live_2gis"
    assert companies[1].name == "Новая"
    assert [row.id for row in snapshots if row.company_id == companies[0].id] == [old_snapshot]
    assert snapshots[0].status == "verified"
    fresh = [row for row in snapshots if row.company_id == companies[1].id]
    assert len(fresh) == 1
    assert fresh[0].status == "collected"
    assert fresh[0].fields["rating"] == 4.8
    assert fresh[0].fields["reviews_count"] == 12
    assert fresh[0].fields["photos_count"] is None
    assert batch is not None and batch.status == "completed"
    assert batch.source_mode == "live_2gis"
    reasons = {item.id_2gis: (item.detail or {}).get("reason") for item in items}
    assert reasons[FIRM] == "already_present"
    assert reasons["7000000100000092"] is None
    assert "test-key" not in json.dumps(batch.progress)


def test_pause_and_resume_do_not_repeat_a_saved_company(factory) -> None:
    control = RunControl()
    sleeps = {"count": 0}

    def pause_before_second_page(_delay: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] >= 2:
            control.pause()

    first_page = [_item(f"7000000100001{index:03d}", f"Фирма {index}") for index in range(10)]
    catalog = ScriptedCatalog(
        [
            _page(*first_page, total=11),
            _page(_item("7000000100000094", "Вторая"), _item(first_page[0]["id"], "Фирма 0")),
        ]
    )
    moment = now()
    batch_id = run_live_batch(
        factory,
        city="Алматы",
        category="Косметология",
        limit=20,
        out=Path("ignored.xlsx"),
        control=control,
        transport=catalog,
        sleep=pause_before_second_page,
        clock=lambda: moment,
    )
    with factory() as db:
        paused = db.get(CollectionBatch, batch_id)
        assert paused is not None and paused.status == "paused"
        assert db.scalar(select(func.count()).select_from(Company)) == 10
    def item_pages() -> list[str]:
        return [params["page"] for path, params in catalog.calls if path.endswith("/items")]

    assert item_pages() == ["1"]
    run_live_batch(
        factory,
        city="Алматы",
        category="Косметология",
        limit=20,
        out=Path("ignored.xlsx"),
        resume=batch_id,
        transport=catalog,
        sleep=lambda _delay: None,
        clock=lambda: moment,
    )
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        companies = db.scalars(select(Company.id_2gis).order_by(Company.id_2gis)).all()
        snapshots = db.scalar(select(func.count()).select_from(CardSnapshot))
    assert batch is not None and batch.status == "completed"
    assert "7000000100000094" in companies
    assert len(companies) == 11
    assert snapshots == 11
    assert item_pages() == ["1", "2"]


def test_catalog_error_is_stored_and_does_not_invent_companies(factory) -> None:
    class Denied:
        def get(self, path: str, params: dict) -> dict:
            raise CatalogDenied("Каталог 2ГИС отклонил ключ.")

    with pytest.raises(CatalogDenied):
        run_live_batch(
            factory,
            city="Алматы",
            category="Косметология",
            limit=20,
            out=Path("ignored.xlsx"),
            transport=Denied(),
            sleep=lambda _delay: None,
        )
    with factory() as db:
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"
        assert "отклонил ключ" in str((batch.progress or {}).get("error"))
        assert db.scalar(select(func.count()).select_from(Company)) == 0


def test_http_client_retries_and_never_leaves_the_catalog_host() -> None:
    calls: list[str] = []

    class Response:
        def __init__(self, status: int, body: dict, url: str) -> None:
            self.status = status
            self._body = json.dumps(body).encode()
            self._url = url

        def read(self) -> bytes:
            return self._body

        def geturl(self) -> str:
            return self._url

    def opener(request, timeout=20):
        calls.append(request.full_url)
        if len(calls) == 1:
            return Response(429, {"meta": {"code": 429}}, request.full_url)
        return Response(200, {"meta": {"code": 200}, "result": {"items": []}}, request.full_url)

    client = HttpCatalogClient("test-key", opener=opener, sleep=lambda _delay: None)
    body = client.get("/3.0/items", {"q": "Косметология", "region_id": "9001"})
    assert body["meta"]["code"] == 200
    assert len(calls) == 2
    assert calls[0].startswith("https://catalog.api.2gis.com/3.0/items?")
    assert "key=test-key" in calls[0]

    def denied(_request, timeout=20):
        return Response(403, {"meta": {"code": 403, "error": {"message": "bad key=test-key"}}}, "https://catalog.api.2gis.com/3.0/items")

    blocked = HttpCatalogClient("test-key", opener=denied, sleep=lambda _delay: None)
    with pytest.raises(CatalogDenied) as caught:
        blocked.get("/3.0/items", {"q": "Косметология"})
    assert "test-key" not in str(caught.value)


def test_worker_refuses_live_catalog_without_a_key_and_does_not_open_the_network(factory) -> None:
    payload = {
        "source_mode": "live_2gis",
        "city": "Алматы",
        "category": "Косметология",
        "platforms": ["2gis"],
        "limit": 20,
        "catalog": "live_2gis",
        "dedup_key": "live-no-key",
        "out": "",
    }
    with factory() as db, db.begin():
        db.add(Job(type="admin.scout_batch", queue="main", status="running", attempts=1, payload=payload, dedup_key="live-no-key"))
    with pytest.raises(CatalogDenied, match="DGIS_CATALOG_KEY"):
        handle_scout_batch(factory, payload)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0


def test_collect_enqueues_catalog_api_without_calling_it(factory) -> None:
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={"city": "Алматы", "queries": ["Косметология"], "platforms": ["2gis"], "limit": 20, "source_mode": "live_2gis"},
        )
        assert denied.status_code == 400
        assert KEY_MISSING.split(".")[0] in denied.json()["detail"]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DGIS_CATALOG_KEY", "test-key")
    try:
        with TestClient(app) as client:
            started = client.post(
                "/api/v1/admin/scout/collect",
                headers=HEADERS,
                json={"city": "Алматы", "queries": ["Косметология"], "platforms": ["2gis"], "limit": 20, "source_mode": "live_2gis"},
            )
        assert started.status_code == 200
        body = started.json()
        assert body["data_source"] == "catalog_api"
        assert body["live_mass_discovery"] is False
        assert body["jobs"][0]["source_mode"] == "live_2gis"
        with factory() as db:
            job = db.scalar(select(Job))
            assert job is not None
            assert job.payload["category"] == "Косметология"
            assert job.payload["catalog"] == "live_2gis"
            assert "test-key" not in json.dumps(job.payload)
            assert db.scalar(select(func.count()).select_from(Company)) == 0
            assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0
    finally:
        monkeypatch.undo()
