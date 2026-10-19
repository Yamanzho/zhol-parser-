"""Collector queue end to end. Fixtures and a mock session only. No live 2GIS."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parser_service import _chrome_options
from tests.test_selenium_collector import HEADERS, NORTH, SOUTH, ScriptedDriver, _grant
from zhol.agents.scout.jobs import handlers
from zhol.api.admin_jobs import BatchBusy, acquire_batch_owner, signal_batch
from zhol.api.main import app
from zhol.db.models import Audit, CardSnapshot, CollectionBatch, Company, HumanTask, Job
from zhol.jobs import enqueue, run_one
from zhol.timeutil import reset_clock, set_now
from zhol.worker import _touch_heartbeat
from zhol.worker_health import main as health_main

STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def test_migration_allows_browser_batches_on_the_test_database(migrated_engine) -> None:
    with migrated_engine.connect() as conn:
        definition = conn.execute(
            text(
                "select pg_get_constraintdef(oid) from pg_constraint "
                "where conname = 'ck_collection_batches_source_mode'"
            )
        ).scalar_one()
    assert "browser_2gis" in definition


def test_container_chrome_options_are_not_a_stealth_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZHOL_CHROME_PROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("ZHOL_CHROME_BINARY", "/usr/bin/chromium")
    monkeypatch.delenv("ZHOL_CHROME_CONTAINER", raising=False)
    plain = _chrome_options(False)
    assert "--no-sandbox" not in plain.arguments
    assert "--user-agent" not in " ".join(plain.arguments).lower()
    monkeypatch.setenv("ZHOL_CHROME_CONTAINER", "1")
    contained = _chrome_options(True)
    joined = " ".join(contained.arguments).lower()
    assert "--no-sandbox" in contained.arguments
    assert "--disable-dev-shm-usage" in contained.arguments
    assert "stealth" not in joined
    assert "user-agent" not in joined
    assert contained.binary_location == "/usr/bin/chromium"


def test_heartbeat_file_reports_liveness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    monkeypatch.setenv("ZHOL_WORKER_HEARTBEAT", str(path))
    assert health_main() == 1
    _touch_heartbeat()
    assert health_main() == 0


def test_form_job_collector_and_company_page(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    drivers: list[ScriptedDriver] = []

    def starter(_headless: bool) -> ScriptedDriver:
        driver = ScriptedDriver()
        drivers.append(driver)
        return driver

    monkeypatch.setattr("parser_service._start_chrome", starter)
    audits_before = _count(factory, Audit)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "categories": ["Салон красоты"],
                "platforms": ["2gis"],
                "limit": 1,
                "source_mode": "browser_2gis",
            },
        )
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["data_source"] == "selenium_public_cards"
        assert body["live_mass_discovery"] is False
        with factory() as db:
            job = db.scalar(select(Job))
            assert job is not None
            assert job.queue == "collector"
            assert job.status == "queued"
        assert run_one(factory, "main", handlers(factory), notifier=lambda _text: None) is None
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
        detail_id = _company_id(factory, NORTH)
        detail = client.get(f"/api/v1/admin/companies/{detail_id}", headers=HEADERS)
        again = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Север"],
                "platforms": ["2gis"],
                "limit": 1,
                "source_mode": "browser_2gis",
            },
        )
        assert again.status_code == 200
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
        with factory() as db:
            second = db.scalar(select(CollectionBatch).where(CollectionBatch.category == "Север"))
            assert second is not None
            second_id = second.id
        listing = client.get(f"/api/v1/admin/scout/batches/{second_id}", headers=HEADERS)
    assert detail.status_code == 200
    company = detail.json()["company"]
    assert company["id_2gis"] == NORTH
    assert company["name"] == "North Beauty"
    assert company["city"] == "Алматы"
    assert company["source"] == "browser_2gis"
    snapshot = detail.json()["snapshots"][0]
    assert snapshot["fields"]["rating"] is None
    assert snapshot["fields"]["photos_count"] is None
    assert snapshot["fields"]["address"]
    assert snapshot["field_sources"]["name"] == "sourced"
    assert snapshot["purpose"] == "prospect"
    assert snapshot["verification_status"] == "collected"
    with factory() as db:
        stored = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id == detail_id))
        assert stored is not None
        assert stored.fields["rating"] is None
        assert stored.field_sources["rating"]["reason"] == "not_shown"
        assert stored.field_sources["name"]["source"] == "2gis.kz"
        assert stored.field_sources["name"]["read_by"] == "code"
        assert stored.field_sources["_collection"]["kind"] == "selenium_public_card"
    listed = listing.json()
    assert listed["active_query"] == "Север"
    # Limit 1 opens one card. North Beauty is already stored, so it spends that slot.
    assert listed["outcome"]["added"] == 0
    assert listed["outcome"]["already_present"] == 1
    assert listed["outcome"]["failed"] == 0
    assert listed["status"] == "completed"
    assert all(driver.closed for driver in drivers)
    assert len(drivers) == 2
    assert any(NORTH in url for url in drivers[1].gets)
    assert _count(factory, Company) == 1
    assert _snapshots(factory, NORTH) == 2
    assert _count(factory, Audit) == audits_before


def test_pause_and_resume_keep_the_same_batch(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    drivers: list[ScriptedDriver] = []

    def pause_after_first(count: int) -> None:
        if count != 1:
            return
        with factory() as db, db.begin():
            batch_id = db.scalar(select(CollectionBatch.id))
            signal_batch(db, int(batch_id), "pause")
        time.sleep(0.5)

    def starter(_headless: bool) -> ScriptedDriver:
        driver = ScriptedDriver(on_card=pause_after_first if not drivers else None)
        drivers.append(driver)
        return driver

    monkeypatch.setattr("parser_service._start_chrome", starter)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "categories": ["Салон красоты"],
                "platforms": ["2gis"],
                "limit": 2,
                "source_mode": "browser_2gis",
            },
        )
        assert accepted.status_code == 200
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
        with factory() as db:
            batch = db.scalar(select(CollectionBatch))
            assert batch is not None
            assert batch.status == "paused"
            batch_id = batch.id
        resumed = client.post(f"/api/v1/admin/scout/batches/{batch_id}/resume", headers=HEADERS)
        assert resumed.status_code == 200
        resumed_job = resumed.json()
        assert resumed_job["status"] == "queued"
        assert run_one(factory, "main", handlers(factory), notifier=lambda _text: None) is None
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
        listing = client.get(f"/api/v1/admin/scout/batches/{batch_id}", headers=HEADERS)
    assert listing.json()["status"] == "completed"
    assert listing.json()["active_query"] == "Салон красоты"
    assert all(driver.closed for driver in drivers)
    assert len(drivers) == 2
    assert not any(NORTH in url for url in drivers[1].gets)
    assert any(SOUTH in url for url in drivers[1].gets)
    assert _count(factory, Company) == 2
    assert _snapshots(factory, NORTH) == 1


def test_main_queue_does_not_open_chrome_for_a_browser_batch(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    opened: list[bool] = []

    def starter(_headless: bool) -> ScriptedDriver:
        opened.append(True)
        return ScriptedDriver()

    monkeypatch.setattr("parser_service._start_chrome", starter)
    with factory() as db, db.begin():
        enqueue(
            db,
            "admin.scout_batch",
            {
                "source_mode": "browser_2gis",
                "city": "Алматы",
                "category": "Салон красоты",
                "platforms": ["2gis"],
                "limit": 1,
                "out": "",
                "catalog": "browser_2gis",
            },
            queue="main",
            dedup_key="main-must-not-collect",
        )
    assert run_one(factory, "main", handlers(factory), notifier=lambda _text: None) is not None
    assert opened == []
    with factory() as db:
        job = db.scalar(select(Job).where(Job.dedup_key == "main-must-not-collect"))
        assert job is not None
        assert job.status == "failed"
        assert "collector" in (job.last_error or "")
        assert _count(factory, Company) == 0


def test_a_second_running_job_cannot_own_the_batch(factory, tmp_path: Path) -> None:
    with factory() as db, db.begin():
        batch = CollectionBatch(
            city="Алматы",
            category="Салон красоты",
            platforms=["2gis"],
            item_limit=1,
            source_mode="browser_2gis",
            status="running",
            out_path=str(tmp_path / "out.txt"),
            catalog_path="browser_2gis",
            progress={},
        )
        db.add(batch)
        db.flush()
        first = Job(
            type="admin.scout_batch",
            queue="collector",
            status="running",
            attempts=1,
            payload={"batch_id": batch.id, "source_mode": "browser_2gis"},
            dedup_key="owner-a",
        )
        second = Job(
            type="admin.scout_batch",
            queue="collector",
            status="queued",
            attempts=0,
            payload={"batch_id": batch.id, "resume": batch.id, "source_mode": "browser_2gis"},
            dedup_key="owner-b",
        )
        db.add_all([first, second])
        db.flush()
        ids = (batch.id, first.id, second.id)
    acquire_batch_owner(factory, ids[0], ids[1])
    with factory() as db, db.begin():
        job = db.get(Job, ids[2])
        assert job is not None
        job.status = "running"
    with pytest.raises(BatchBusy):
        acquire_batch_owner(factory, ids[0], ids[2])


def test_crash_retries_then_stops_and_closes_the_browser(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    set_now(T0)
    drivers: list[ScriptedDriver] = []

    def starter(_headless: bool) -> ScriptedDriver:
        driver = ScriptedDriver(crash=True)
        drivers.append(driver)
        return driver

    monkeypatch.setattr("parser_service._start_chrome", starter)
    try:
        with factory() as db, db.begin():
            enqueue(
                db,
                "admin.scout_batch",
                {
                    "source_mode": "browser_2gis",
                    "city": "Алматы",
                    "category": "Салон красоты",
                    "platforms": ["2gis"],
                    "limit": 1,
                    "out": "",
                    "catalog": "browser_2gis",
                },
                queue="collector",
                dedup_key="collector-crash",
            )
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
        set_now(T0 + timedelta(minutes=1))
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
        set_now(T0 + timedelta(minutes=6))
        assert run_one(factory, "collector", handlers(factory), notifier=lambda _text: None) is not None
    finally:
        reset_clock()
    assert len(drivers) == 3
    assert all(driver.closed for driver in drivers)
    with factory() as db:
        job = db.scalar(select(Job).where(Job.type == "admin.scout_batch", Job.dedup_key.is_(None)))
        assert job is not None
        assert job.status == "dead"
        assert job.attempts == 3
        assert db.scalar(select(func.count()).select_from(HumanTask)) == 1
        assert _count(factory, Company) == 0
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"


def _count(factory, model: type) -> int:
    with factory() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _company_id(factory, firm_id: str) -> int:
    with factory() as db:
        company = db.scalar(select(Company).where(Company.id_2gis == firm_id))
        assert company is not None
        return int(company.id)


def _snapshots(factory, firm_id: str) -> int:
    with factory() as db:
        company_id = db.scalar(select(Company.id).where(Company.id_2gis == firm_id))
        return int(
            db.scalar(select(func.count()).select_from(CardSnapshot).where(CardSnapshot.company_id == company_id)) or 0
        )
