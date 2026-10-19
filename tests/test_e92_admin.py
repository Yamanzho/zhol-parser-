"""E9.2 admin actions. Offline and fixture work only; no Bek, live cabinet, or paid model call."""

from __future__ import annotations

import io
import socket
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.jobs import MAIN_JOBS, handlers
from zhol.api.main import app
from zhol.db.base import get_session_factory
from zhol.worker import serve
from zhol.db.models import (
    Audit,
    ChangePackage,
    CollectionBatch,
    CollectionBatchItem,
    Company,
    ContentItem,
    ListingSubmission,
    Order,
    Report,
)

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}


def _spec(key: str = "e92-order") -> dict:
    return {
        "key": key,
        "company": {"id_2gis": "70000000000000921", "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": "2GIS",
                "action": "update",
                "external_id": "70000000000000921",
                "owner_authorization": "verified",
            }
        ],
        "messages": [{"id": "m1", "text": "Адрес: ул. Абая 10. Телефон +7 701 111 22 33. Часы 10:00-20:00. Услуга Стрижка 8000."}],
        "facts": [
            {"field": "address", "value": "ул. Абая 10", "source_ref": "msg:m1", "approved": True},
            {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "approved": True},
            {"field": "hours", "value": "10:00-20:00", "source_ref": "msg:m1", "approved": True},
            {"field": "services", "value": [{"name": "Стрижка", "price": 8000}], "source_ref": "msg:m1", "approved": True},
        ],
        "materials": [{"kind": "logo", "path": "materials/logo.png", "ref": "logo", "original_name": "logo.png"}],
        "not_applicable": ["videos"],
        "changes": [{"platform": "2GIS", "field": "hours", "after": "10:00-20:00", "source_ref": "msg:m1"}],
    }


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    stop = threading.Event()
    worker = threading.Thread(
        target=serve,
        args=(get_session_factory(), "main"),
        kwargs={"notifier": lambda _message: None, "stop": stop.is_set, "poll_seconds": 0.05},
        daemon=True,
    )
    worker.start()
    try:
        with TestClient(app) as test_client:
            yield test_client, factory
    finally:
        stop.set()
        worker.join(20)


def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("zhol.llm.providers.default_providers", lambda: (_ for _ in ()).throw(AssertionError("paid provider")))


def _batch_body(limit: int = 1) -> dict:
    return {
        "source_mode": "fixture",
        "city": "Алматы",
        "category": "Салон красоты",
        "platforms": ["2gis"],
        "limit": limit,
        "catalog": "catalog.yaml",
    }


def _wait_job(test_client: TestClient, job_id: int) -> dict:
    last = {}
    for _ in range(200):
        response = test_client.get(f"/api/v1/admin/jobs/{job_id}", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        last = response.json()
        if last["status"] in {"done", "dead"}:
            return last
        threading.Event().wait(0.05)
    return last


def test_admin_job_is_claimed_by_the_main_worker() -> None:
    assert "admin.scout_batch" in MAIN_JOBS
    assert "admin.scout_batch" in handlers(sessionmaker())


def test_actions_require_a_token_and_a_request_header(client) -> None:
    test_client, _factory = client
    missing = test_client.post("/api/v1/admin/orders", json=_spec("e92-auth"))
    assert missing.status_code == 401
    wrong = test_client.post("/api/v1/admin/orders", json=_spec("e92-auth"), headers={"Authorization": "Bearer wrong", "X-Zhol-Request": "admin"})
    assert wrong.status_code == 401
    no_header = test_client.post(
        "/api/v1/admin/orders",
        json=_spec("e92-auth"),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert no_header.status_code == 403


def test_order_create_returns_readiness(client) -> None:
    test_client, factory = client
    created = test_client.post("/api/v1/admin/orders", json=_spec(), headers=HEADERS)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["bek_contacted"] is False
    assert body["live_publication"] is False
    assert body["readiness"]["status"] in {"ready", "not_ready", "blocked"}
    assert "missing_owner_permissions" in body["readiness"]
    detail = test_client.get(f"/api/v1/admin/orders/{body['order_id']}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert detail.status_code == 200
    repeat = dict(_spec())
    repeat.pop("company")
    repeat["company_id"] = detail.json()["company_id"]
    again = test_client.post("/api/v1/admin/orders", json=repeat, headers=HEADERS)
    assert again.status_code == 409
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(Report)) == 0


def test_audit_upload_validation_and_import(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    test_client, factory = client
    invalid = io.BytesIO()
    book = Workbook()
    book.active.append(["comment"])
    book.save(invalid)
    rejected = test_client.post(
        "/api/v1/admin/audits/validate",
        headers=HEADERS,
        files={"file": ("notes.xlsx", invalid.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert rejected.status_code == 400
    escaped = test_client.post(
        "/api/v1/admin/audits/validate",
        headers=HEADERS,
        files={"file": ("../evil.xlsx", b"PK\x03\x04not-a-workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert escaped.status_code == 400
    assert not (tmp_path / "evil.xlsx").exists()
    too_big = test_client.post(
        "/api/v1/admin/audits/validate",
        headers=HEADERS,
        files={"file": ("big.xlsx", b"PK\x03\x04" + b"a" * (8 * 1024 * 1024), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert too_big.status_code == 400
    with factory() as session, session.begin():
        session.add(Company(id_2gis="70000000000000921", name="Salon Test", city="Алматы", source="e92"))
        before = session.scalar(select(func.count()).select_from(Audit))
    path = _audit_workbook(tmp_path / "audit.xlsx")
    checked = test_client.post(
        "/api/v1/admin/audits/validate",
        headers=HEADERS,
        files={"file": ("audit.xlsx", path.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["imported"] is False
    assert checked.json()["bek_contacted"] is False
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Audit)) == before
    imported = test_client.post(
        "/api/v1/admin/audits/import",
        headers=HEADERS,
        files={"file": ("audit.xlsx", path.read_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] is True
    assert imported.json()["plans"] > 0
    assert imported.json()["bek_contacted"] is False
    listed = test_client.get("/api/v1/admin/audits", headers={"Authorization": f"Bearer {TOKEN}"})
    assert listed.status_code == 200
    assert listed.json()["total"] > 0


def test_fixture_batch_pause_resume_stop_and_export(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, factory = client
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow(db_factory, item_id, _store, _cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            assert release.wait(10)
        with db_factory() as session, session.begin():
            item = session.get(CollectionBatchItem, item_id)
            if item is not None:
                item.status = "completed"

    monkeypatch.setattr("zhol.agents.scout.batch._process_item", slow)
    try:
        first = test_client.post("/api/v1/admin/scout/batches", json=_batch_body(2), headers=HEADERS)
        assert first.status_code == 200, first.text
        assert first.json()["duplicate"] is False
        assert first.json()["source_mode"] == "fixture"
        assert started.wait(10)
        second = test_client.post("/api/v1/admin/scout/batches", json=_batch_body(2), headers=HEADERS)
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["id"] == first.json()["id"]
        job = _wait_batch_id(test_client, first.json()["id"])
        paused = test_client.post(f"/api/v1/admin/scout/batches/{job['batch_id']}/pause", headers=HEADERS)
        assert paused.status_code == 200, paused.text
    finally:
        release.set()
    finished = _wait_job(test_client, first.json()["id"])
    assert finished["status"] == "done"
    with factory() as session:
        batch = session.get(CollectionBatch, job["batch_id"])
        assert batch is not None
        assert batch.status == "paused"
        assert batch.source_mode == "fixture"
        pending = session.scalar(
            select(func.count()).select_from(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == batch.id,
                CollectionBatchItem.status == "pending",
            )
        )
        assert pending == 1
    resumed = test_client.post(f"/api/v1/admin/scout/batches/{job['batch_id']}/resume", headers=HEADERS)
    assert resumed.status_code == 200, resumed.text
    assert _wait_job(test_client, resumed.json()["id"])["status"] == "done"
    with factory() as session:
        batch = session.get(CollectionBatch, job["batch_id"])
        assert batch is not None
        assert batch.status == "completed"
    exported = test_client.post(f"/api/v1/admin/scout/batches/{job['batch_id']}/export", headers=HEADERS)
    assert exported.status_code == 200, exported.text
    download = test_client.get(exported.json()["download"], headers={"Authorization": f"Bearer {TOKEN}"})
    assert download.status_code == 200
    assert download.content.startswith(b"PK")
    with factory() as session:
        batch = session.get(CollectionBatch, job["batch_id"])
        assert batch is not None
        batch.out_path = "/etc/passwd"
        session.commit()
    blocked = test_client.get(f"/api/v1/admin/scout/batches/{job['batch_id']}/export.xlsx", headers={"Authorization": f"Bearer {TOKEN}"})
    assert blocked.status_code == 404
    assert b"root:" not in blocked.content


def test_stop_blocks_resume(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, factory = client
    started = threading.Event()
    release = threading.Event()

    def slow(db_factory, item_id, _store, _cfg):
        started.set()
        assert release.wait(10)
        with db_factory() as session, session.begin():
            item = session.get(CollectionBatchItem, item_id)
            if item is not None:
                item.status = "completed"

    monkeypatch.setattr("zhol.agents.scout.batch._process_item", slow)
    try:
        created = test_client.post("/api/v1/admin/scout/batches", json=_batch_body(2), headers=HEADERS)
        assert created.status_code == 200, created.text
        assert started.wait(10)
        job = _wait_batch_id(test_client, created.json()["id"])
        stopped = test_client.post(f"/api/v1/admin/scout/batches/{job['batch_id']}/stop", headers=HEADERS)
        assert stopped.status_code == 200
        release.set()
        assert _wait_job(test_client, created.json()["id"])["status"] == "done"
        with factory() as session:
            batch = session.get(CollectionBatch, job["batch_id"])
            assert batch is not None and batch.status == "stopped"
        resume = test_client.post(f"/api/v1/admin/scout/batches/{job['batch_id']}/resume", headers=HEADERS)
        assert resume.status_code == 409
    finally:
        release.set()


def test_live_catalog_and_urls_are_rejected(client) -> None:
    test_client, _factory = client
    live = test_client.post("/api/v1/admin/scout/batches", json={**_batch_body(), "source_mode": "live"}, headers=HEADERS)
    assert live.status_code == 422
    remote = test_client.post("/api/v1/admin/scout/batches", json={**_batch_body(), "catalog": "https://example.com/catalog.yaml"}, headers=HEADERS)
    assert remote.status_code == 400
    escape = test_client.post("/api/v1/admin/scout/batches", json={**_batch_body(), "catalog": "../catalog.yaml"}, headers=HEADERS)
    assert escape.status_code == 400


def test_content_approval_uses_the_offline_provider(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    test_client, factory = client
    created = test_client.post("/api/v1/admin/orders", json=_spec("e92-content"), headers=HEADERS)
    assert created.status_code == 200, created.text
    order_id = created.json()["order_id"]
    prepared = test_client.post(f"/api/v1/admin/orders/{order_id}/content/prepare", headers=HEADERS)
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["provider"] == "offline"
    assert prepared.json()["publication"] == "not_published"
    with factory() as session:
        row = session.scalars(select(ContentItem).where(ContentItem.order_id == order_id, ContentItem.status == "done")).first()
        assert row is not None
        content_id = row.id
        version = int(row.brief["version"])
    mismatch = test_client.post(f"/api/v1/admin/content/{content_id}/approve", json={"version": version + 1}, headers=HEADERS)
    assert mismatch.status_code == 409
    approved = test_client.post(f"/api/v1/admin/content/{content_id}/approve", json={"version": version}, headers=HEADERS)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "checked"
    assert approved.json()["publication"] == "not_published"
    again = test_client.post(f"/api/v1/admin/content/{content_id}/approve", json={"version": version}, headers=HEADERS)
    assert again.status_code == 200
    with factory() as session:
        stored = session.get(ContentItem, content_id)
        assert stored is not None and stored.status == "checked"


def test_listing_dry_run_and_offline_simulation_do_not_publish(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    test_client, factory = client
    created = test_client.post("/api/v1/admin/orders", json=_spec("e92-listing"), headers=HEADERS)
    assert created.status_code == 200, created.text
    order_id = created.json()["order_id"]
    dry = test_client.post(f"/api/v1/admin/orders/{order_id}/listings/dry-run", headers=HEADERS)
    assert dry.status_code == 200, dry.text
    assert dry.json()["mode"] == "dry_run"
    assert dry.json()["published"] is False
    assert dry.json()["live_publication"] is False
    assert "http://" not in dry.text and "https://" not in dry.text
    with factory() as session:
        package = session.scalar(select(ChangePackage).where(ChangePackage.order_id == order_id))
        assert package is not None
        package_id, version = package.id, package.version
    mismatch = test_client.post(
        f"/api/v1/admin/orders/{order_id}/listings/approve",
        headers=HEADERS,
        data={"package_id": str(package_id), "version": str(version + 9), "platform": "2gis", "operation": "update", "mode": "offline"},
        files={"file": ("page.html", b"<html><body>saved</body></html>", "text/html")},
    )
    assert mismatch.status_code == 409
    live = test_client.post(
        f"/api/v1/admin/orders/{order_id}/listings/simulate",
        headers=HEADERS,
        data={
            "package_id": str(package_id),
            "version": str(version),
            "platform": "2gis",
            "operation": "update",
            "mode": "live",
            "page_url": "https://business.example/edit",
        },
        files={"file": ("page.html", b"<html><body>saved</body></html>", "text/html")},
    )
    assert live.status_code == 400
    simulated = test_client.post(
        f"/api/v1/admin/orders/{order_id}/listings/simulate",
        headers=HEADERS,
        data={"package_id": str(package_id), "version": str(version), "platform": "2gis", "operation": "update", "mode": "offline"},
        files={"file": ("page.html", b"<html><body>saved offline page</body></html>", "text/html")},
    )
    assert simulated.status_code == 200, simulated.text
    assert simulated.json()["published"] is False
    assert simulated.json()["live_publication"] is False
    assert simulated.json()["mode"] == "offline"
    with factory() as session:
        rows = session.scalars(select(ListingSubmission).where(ListingSubmission.order_id == order_id)).all()
        assert all(row.status != "published" for row in rows)


def test_completion_report_download_stays_inside_the_admin_directory(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _offline(monkeypatch)
    test_client, factory = client
    created = test_client.post("/api/v1/admin/orders", json=_spec("e92-report"), headers=HEADERS)
    assert created.status_code == 200, created.text
    order_id = created.json()["order_id"]
    report = test_client.post(f"/api/v1/admin/orders/{order_id}/reports/completion", headers=HEADERS)
    assert report.status_code == 200, report.text
    assert report.json()["live_verified"] is False
    assert report.json()["live_publication"] is False
    again = test_client.post(f"/api/v1/admin/orders/{order_id}/reports/completion", headers=HEADERS)
    assert again.status_code == 200
    assert again.json()["id"] == report.json()["id"]
    downloaded = test_client.get(report.json()["downloads"]["json"], headers={"Authorization": f"Bearer {TOKEN}"})
    assert downloaded.status_code == 200
    assert b"live_verified" in downloaded.content
    html = test_client.get(report.json()["downloads"]["html"], headers={"Authorization": f"Bearer {TOKEN}"})
    assert html.status_code == 200
    assert "Живая проверка" in html.text
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Report)) == 1
        row = session.get(Report, report.json()["id"])
        assert row is not None
        row.order_id = order_id
        session.commit()
    missing = test_client.get("/api/v1/admin/reports/999999/completion.json", headers={"Authorization": f"Bearer {TOKEN}"})
    assert missing.status_code == 404


def test_company_export_and_mailing_download(client) -> None:
    test_client, _factory = client
    missing = test_client.get("/api/v1/admin/scout/export.xlsx", headers={"Authorization": f"Bearer {TOKEN}"})
    assert missing.status_code == 404
    exported = test_client.post("/api/v1/admin/scout/export", headers=HEADERS)
    assert exported.status_code == 200, exported.text
    download = test_client.get(exported.json()["download"], headers={"Authorization": f"Bearer {TOKEN}"})
    assert download.status_code == 200
    assert download.content.startswith(b"PK")
    assert ".." not in exported.json()["download"]


def _wait_batch_id(test_client: TestClient, job_id: int) -> dict:
    for _ in range(200):
        response = test_client.get(f"/api/v1/admin/jobs/{job_id}", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        body = response.json()
        if body.get("batch_id"):
            return body
        if body["status"] in {"done", "dead"}:
            return body
        threading.Event().wait(0.05)
    raise AssertionError("batch id was not stored")


def _audit_workbook(path: Path) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = "карточки"
    sheet.append(["id_2gis", "название", "слой", "url_2gis", "дата_просмотра", "балл_итого", "город", "адрес_2гис"])
    sheet.append(
        [
            "70000000000000921",
            "Salon Test",
            "середина",
            "https://2gis.kz/firm/70000000000000921",
            "2026-09-30",
            "40",
            "Алматы",
            "ул. Абая 10",
        ]
    )
    book.save(path)
    return path
