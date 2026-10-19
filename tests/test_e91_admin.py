from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from zhol.api.admin import get_admin_session, router as admin_router
from zhol.api.main import app
from zhol.audit.bek import PROMPT
from zhol.db.models import (
    Audit,
    CardSnapshot,
    ChangePackage,
    CollectionBatch,
    CollectionBatchItem,
    Company,
    ContentItem,
    Credential,
    HumanTask,
    ListingImprovementPlan,
    ListingSubmission,
    Order,
    Report,
)
from zhol.orders.intake import create_test_order
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
SENTINEL = "TOKEN-SENTINEL-91"
LOGIN_SENTINEL = "LOGIN-HINT-SENTINEL"
FORBIDDEN_KEYS = {
    "password",
    "passwd",
    "token",
    "secret",
    "ciphertext",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "login_hint",
    "credential",
    "credentials",
    "storage_state",
    "bearer",
    "cabinet_url",
}


def _spec() -> dict:
    return {
        "key": "e91-order",
        "company": {"id_2gis": "70000000000000091", "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": "2GIS",
                "action": "update",
                "external_id": "70000000000000091",
                "owner_authorization": "verified",
            }
        ],
        "messages": [
            {
                "id": "m1",
                "text": "Адрес: ул. Абая 10. Телефон +7 701 111 22 33. Часы 10:00-20:00. Услуга Стрижка 8000.",
            }
        ],
        "facts": [
            {"field": "address", "value": "ул. Абая 10", "source_ref": "msg:m1", "approved": True},
            {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "approved": True},
            {"field": "hours", "value": "10:00-20:00", "source_ref": "msg:m1", "approved": True},
            {
                "field": "services",
                "value": [{"name": "Стрижка", "price": 8000}],
                "source_ref": "msg:m1",
                "approved": True,
            },
        ],
        "materials": [
            {"kind": "logo", "path": "materials/logo.png", "ref": "logo", "original_name": "logo.png"},
            {"kind": "photo_real", "path": "materials/hall.png", "ref": "hall", "original_name": "hall.png"},
        ],
        "not_applicable": ["videos"],
        "changes": [{"platform": "2GIS", "field": "hours", "after": "10:00-20:00", "source_ref": "msg:m1"}],
    }


def _keys(value: object, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key).lower())
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with TestClient(app) as test_client:
        yield test_client, factory


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as db, db.begin():
        db.add(Company(id_2gis="e91-aaa", name="Aaa Salon", city="Алматы", source="e91"))
        db.add(Company(id_2gis="e91-bbb", name="Bbb Salon", city="Алматы", source="e91"))
        order = create_test_order(db, _spec())
        company = db.get(Company, order.company_id)
        assert company is not None
        company.whatsapp = "77010000000"
        package = db.scalar(select(ChangePackage).where(ChangePackage.order_id == order.id))
        assert package is not None
        seen = now()
        snapshot = CardSnapshot(
            company_id=company.id,
            platform="2gis",
            match_status="found",
            url="https://2gis.kz/firm/70000000000000091",
            fields={"name": "Salon Test", "rating": 4.5, "password": SENTINEL},
            field_sources={"name": {"url": "https://2gis.kz/firm/70000000000000091", "seen_at": "2026-10-01"}},
            purpose="prospect",
            collected_at=seen,
            collected_by="bek_audit",
            status="collected",
        )
        db.add(snapshot)
        db.flush()
        audit = Audit(
            company_id=company.id,
            snapshot_ids=[snapshot.id],
            score_total=40,
            score_groups={"cards": 40},
            derived={},
            discrepancies=[],
            gaps=[],
            observations=["вкладка цен пустая"],
            status="draft",
            prompt_version=PROMPT,
            model=None,
        )
        db.add(audit)
        db.flush()
        plan_body = {"findings": [], "platforms": {"2gis": {"match_status": "found", "url": snapshot.url}}}
        db.add(
            ListingImprovementPlan(
                company_id=company.id,
                audit_id=audit.id,
                source_key="e91",
                plan=plan_body,
            )
        )
        db.add(
            Credential(
                company_id=company.id,
                platform="2gis",
                login_hint=LOGIN_SENTINEL,
                ciphertext=b"ciphertext-bytes",
            )
        )
        db.add(
            HumanTask(
                reason="e91_review",
                object_type="order",
                object_id=order.id,
                status="open",
                details={"password": SENTINEL, "note": "check the card"},
            )
        )
        db.add(
            ContentItem(
                order_id=order.id,
                kind="company_description",
                platform="yandex",
                status="done",
                brief={"version": 1, "sources": ["msg:m1"]},
            )
        )
        db.add(
            ContentItem(
                order_id=order.id,
                kind="cover",
                status="checked",
                brief={"illustrative": True, "origin": "offline-illustration", "approval": {"at": "2026-10-12"}, "version": 1},
            )
        )
        for platform, operation, status in (
            ("2gis", "update", "prepared"),
            ("yandex", "update", "submitted"),
            ("google", "update", "moderation_pending"),
            ("2gis", "create", "published"),
        ):
            db.add(
                ListingSubmission(
                    order_id=order.id,
                    package_id=package.id,
                    platform=platform,
                    operation=operation,
                    status=status,
                    idempotency_key=f"e91-{platform}-{operation}",
                    fields={"password": SENTINEL},
                    evidence={"mode": "offline", "password": SENTINEL},
                    moderation_status="pending" if status == "moderation_pending" else None,
                )
            )
        db.add(
            Report(
                order_id=order.id,
                period="setup",
                status="draft",
                summary="stored report",
                data={
                    "live_verified": False,
                    "verification_mode": "stored_offline",
                    "completed": False,
                    "outcome": "incomplete",
                    "password": SENTINEL,
                },
            )
        )
        fixture = CollectionBatch(
            city="Алматы",
            category="салон",
            platforms=["2gis"],
            source_mode="fixture",
            status="completed",
            out_path="data/export/mailing.xlsx",
            catalog_path="tests/fixtures/catalog.yaml",
        )
        db.add(fixture)
        db.flush()
        db.add(CollectionBatchItem(batch_id=fixture.id, id_2gis=company.id_2gis, company_id=company.id, status="completed"))
        db.add(CollectionBatchItem(batch_id=fixture.id, id_2gis="e91-bbb", status="pending"))
        db.add(
            CollectionBatch(
                city="Алматы",
                category="салон",
                platforms=["2gis"],
                source_mode="live",
                status="stopped",
                out_path="data/export/live.xlsx",
                catalog_path="tests/fixtures/catalog.yaml",
            )
        )
        db.flush()
        return {
            "order_id": order.id,
            "company_id": company.id,
            "audit_id": audit.id,
            "batch_id": fixture.id,
            "plan": plan_body,
        }


def test_admin_reads_stored_records(client) -> None:
    test_client, factory = client
    seeded = _seed(factory)
    headers = _auth()
    before = _counts(factory)

    health = test_client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert test_client.get("/api/v1/admin/summary").status_code == 401
    assert test_client.get("/api/v1/admin/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401

    summary = test_client.get("/api/v1/admin/summary", headers=headers)
    assert summary.status_code == 200
    body = summary.json()
    assert body["counts"]["companies"] == 3
    assert body["counts"]["orders"] == 1
    assert body["live_publication"] is False
    assert body["live_mass_discovery"] is False
    assert body["bek_connected"] is False
    assert body["scout_source_modes"]["fixture"] == 1
    assert body["listing_phases"]["draft"] == 1
    assert body["listing_phases"]["submitted"] == 1
    assert body["listing_phases"]["moderation"] == 1
    assert body["listing_phases"]["verified"] == 1

    assert test_client.get("/api/v1/admin/companies", headers=headers, params={"limit": 0}).status_code == 422
    assert test_client.get("/api/v1/admin/companies", headers=headers, params={"limit": 101}).status_code == 422
    assert test_client.get("/api/v1/admin/companies", headers=headers, params={"offset": 10001}).status_code == 422

    page = test_client.get("/api/v1/admin/companies", headers=headers, params={"limit": 1, "offset": 1})
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 1
    found = test_client.get("/api/v1/admin/companies", headers=headers, params={"q": "Bbb"})
    assert found.json()["total"] == 1
    assert found.json()["items"][0]["name"] == "Bbb Salon"
    assert test_client.get("/api/v1/admin/companies", headers=headers, params={"q": "%"}).json()["total"] == 0

    company = test_client.get(f"/api/v1/admin/companies/{seeded['company_id']}", headers=headers)
    assert company.status_code == 200
    detail = company.json()
    assert detail["company"]["whatsapp"] == "77010000000"
    assert detail["platforms"][0]["access_status"] == "verified"
    assert "cabinet_url" not in detail["platforms"][0]
    snapshot = detail["snapshots"][0]
    assert snapshot["collector"] == "bek_audit"
    assert snapshot["verification_status"] == "collected"
    assert snapshot["fields"]["name"] == "Salon Test"
    assert "password" not in snapshot["fields"]
    assert snapshot["field_sources"]["name"] == "sourced"

    audits = test_client.get("/api/v1/admin/audits", headers=headers, params={"company_id": seeded["company_id"]})
    assert audits.json()["total"] == 1
    assert audits.json()["items"][0]["source"] == "bek_workbook"
    audit = test_client.get(f"/api/v1/admin/audits/{seeded['audit_id']}", headers=headers)
    assert audit.status_code == 200
    assert audit.json()["plan"]["listing_modified"] is False
    assert audit.json()["plan"]["source"] == "bek_audit"

    batches = test_client.get("/api/v1/admin/scout/batches", headers=headers)
    assert batches.json()["total"] == 2
    assert all(item["live_mass_discovery"] is False for item in batches.json()["items"])
    batch = test_client.get(f"/api/v1/admin/scout/batches/{seeded['batch_id']}", headers=headers)
    assert batch.json()["recorded_source_mode"] == "fixture"
    assert batch.json()["progress"]["completed"] == 1
    assert batch.json()["progress"]["pending"] == 1
    assert batch.json()["progress"]["total"] == 2

    orders = test_client.get("/api/v1/admin/orders", headers=headers, params={"stage": "created"})
    assert orders.json()["total"] == 1
    assert orders.json()["items"][0]["readiness"] == "ready"
    assert test_client.get("/api/v1/admin/orders", headers=headers, params={"stage": "missing"}).json()["total"] == 0
    order = test_client.get(f"/api/v1/admin/orders/{seeded['order_id']}", headers=headers)
    assert order.status_code == 200
    assert order.json()["readiness"]["status"] == "ready"
    assert order.json()["status"]["company_name"] == "Salon Test"
    assert order.json()["amount_kzt"] == 30000
    assert order.json()["content"]["publication"] == "not_published"

    content = test_client.get("/api/v1/admin/content", headers=headers, params={"status": "checked"})
    assert content.json()["total"] == 1
    assert content.json()["items"][0]["approval_status"] == "approved"
    assert content.json()["items"][0]["illustrative"] is True
    assert content.json()["items"][0]["publication"] == "not_published"

    listings = test_client.get("/api/v1/admin/listings", headers=headers, params={"order_id": seeded["order_id"]})
    phases = {row["phase"]: row["phase_label"] for item in listings.json()["items"] for row in item["submissions"]}
    assert phases["draft"].startswith("Draft")
    assert "Not a live publication" in phases["submitted"]
    assert phases["moderation"].startswith("Moderation")
    assert "Not a live publication" in phases["verified"]
    assert all(row["live_publication"] is False for item in listings.json()["items"] for row in item["submissions"])
    assert all(row["evidence_mode"] == "offline" for item in listings.json()["items"] for row in item["submissions"])

    tasks = test_client.get("/api/v1/admin/tasks", headers=headers, params={"status": "open"})
    assert tasks.json()["total"] == 1
    assert tasks.json()["items"][0]["details"] == {"note": "check the card"}

    reports = test_client.get("/api/v1/admin/reports", headers=headers)
    assert reports.json()["items"][0]["stored_live_verified"] is False
    assert reports.json()["items"][0]["verification_mode"] == "stored_offline"
    assert reports.json()["items"][0]["live_publication"] is False
    report_id = reports.json()["items"][0]["id"]
    report = test_client.get(f"/api/v1/admin/reports/{report_id}", headers=headers)
    assert report.json()["recomputed"] is False

    empty_company = test_client.get("/api/v1/admin/companies/999999", headers=headers)
    assert empty_company.status_code == 404
    posted = test_client.post("/api/v1/admin/companies", headers=headers, json={"name": "nope"})
    assert posted.status_code == 405

    blob = " ".join(
        response.text
        for response in (
            summary,
            company,
            audit,
            batch,
            order,
            content,
            listings,
            tasks,
            reports,
            report,
        )
    )
    assert SENTINEL not in blob
    assert LOGIN_SENTINEL not in blob
    keys: set[str] = set()
    for response in (summary, company, audit, batch, order, content, listings, tasks, reports, report):
        _keys(response.json(), keys)
    assert keys.isdisjoint(FORBIDDEN_KEYS)

    after = _counts(factory)
    assert after == before
    with factory() as db:
        stored = db.scalar(select(ListingImprovementPlan.plan).where(ListingImprovementPlan.company_id == seeded["company_id"]))
    assert stored == seeded["plan"]


def test_empty_database(client) -> None:
    test_client, _factory = client
    headers = _auth()
    summary = test_client.get("/api/v1/admin/summary", headers=headers)
    assert summary.status_code == 200
    assert all(value == 0 for value in summary.json()["counts"].values())
    assert test_client.get("/api/v1/admin/companies", headers=headers).json()["items"] == []
    assert test_client.get("/api/v1/admin/orders", headers=headers).json()["items"] == []
    assert test_client.get("/api/v1/admin/reports", headers=headers).json()["items"] == []


def test_admin_disabled_without_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, _factory = client
    monkeypatch.delenv("ZHOL_ADMIN_TOKEN", raising=False)
    response = test_client.get("/api/v1/admin/summary", headers=_auth())
    assert response.status_code == 503
    assert test_client.get("/health").status_code == 200


def test_admin_session_is_read_only(migrated_engine) -> None:
    del migrated_engine
    generator = get_admin_session()
    session = next(generator)
    try:
        session.add(Company(id_2gis="e91-write", name="Write", city="Алматы", source="e91"))
        with pytest.raises(Exception, match="read-only|read only"):
            session.flush()
    finally:
        generator.close()


def test_admin_routes_do_not_write() -> None:
    methods: set[str] = set()
    for route in admin_router.routes:
        methods.update(getattr(route, "methods", set()))
    assert "GET" in methods
    assert "POST" not in methods
    assert "PUT" not in methods
    assert "PATCH" not in methods
    assert "DELETE" not in methods


def test_compose_keeps_the_database_volume() -> None:
    main = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    offline = yaml.safe_load((ROOT / "docker-compose.offline.yml").read_text(encoding="utf-8"))
    assert main["volumes"]["pgdata"] is None
    assert main["services"]["db"]["volumes"] == ["pgdata:/var/lib/postgresql/data"]
    assert main["services"]["api"]["ports"] == ["127.0.0.1:${API_PORT:-18080}:8000"]
    assert main["services"]["web"]["ports"] == ["127.0.0.1:${WEB_PORT:-15173}:5173"]
    assert "pgdata" not in offline.get("volumes", {})
    assert offline["services"]["api"]["environment"]["ZHOL_CONFIG_DIR"] == "/offline-config"
    assert "./tests/fixtures:/app/tests/fixtures:ro" in offline["services"]["worker"]["volumes"]
    assert "./data/admin:/app/data/admin" in offline["services"]["worker"]["volumes"]


def test_frontend_build() -> None:
    web = ROOT / "web"
    subprocess.run(["npm", "ci"], cwd=web, check=True)
    subprocess.run(["npm", "run", "build"], cwd=web, check=True)
    page = (web / "dist" / "index.html").read_text(encoding="utf-8")
    assert 'id="root"' in page


def _counts(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as db:
        return {
            name: int(db.scalar(select(func.count()).select_from(model)) or 0)
            for name, model in {
                "companies": Company,
                "orders": Order,
                "audits": Audit,
                "plans": ListingImprovementPlan,
                "content": ContentItem,
                "submissions": ListingSubmission,
                "tasks": HumanTask,
                "reports": Report,
                "credentials": Credential,
            }.items()
        }

