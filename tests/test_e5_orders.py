"""E5 local order intake. No Bek, payment, browser, or listing edit."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.api.local_orders import get_session
from zhol.api.main import app
from zhol.cli import main
from zhol.db.models import (
    ChangeItem,
    ChangeLog,
    ChangePackage,
    Company,
    CompanyPlatformId,
    CompanyProfile,
    Credential,
    Deal,
    EventOut,
    Intake,
    Invoice,
    Job,
    Order,
)
from zhol.orders.intake import (
    DuplicateOrderError,
    OrderIntakeError,
    check_readiness,
    create_test_order,
    require_execution_allowed,
)
from zhol.transitions import IllegalTransition, allowed, transition

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
SPARE_PHONE = "+7 700 000 00 00"


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _spec(**changes: object) -> dict:
    spec: dict = {
        "key": "ready-1",
        "company": {"id_2gis": "70000000000000001", "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": "2GIS",
                "action": "update",
                "external_id": "70000000000000001",
                "owner_authorization": "verified",
            }
        ],
        "messages": [
            {
                "id": "m1",
                "text": (
                    "Адрес: ул. Абая 10. Телефон +7 701 111 22 33. "
                    f"Запасной {SPARE_PHONE} не использовать. Часы 10:00-20:00. Услуга Стрижка 8000."
                ),
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
    spec.update(changes)
    return spec


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_valid_test_order(session: Session) -> None:
    order = create_test_order(session, _spec())
    assert order.stage == "created"
    assert order.amount_kzt == 30_000
    assert order.kind == "setup"
    company = session.get(Company, order.company_id)
    assert company is not None
    assert company.id_2gis == "70000000000000001"
    assert company.whatsapp is None
    assert company.instagram is None

    profile = session.scalar(select(CompanyProfile).where(CompanyProfile.company_id == company.id))
    assert profile is not None
    stored = json.dumps(profile.data, ensure_ascii=False)
    assert SPARE_PHONE not in stored
    assert profile.data["address"]["value"] == "ул. Абая 10"
    assert profile.data["address"]["needs_confirmation"] is False
    assert profile.data["phones"]["value"] == ["+7 701 111 22 33"]
    assert profile.data["services"][0]["name"] == "Стрижка"
    assert profile.data["services"][0]["needs_confirmation"] is False

    platform = session.get(CompanyPlatformId, (company.id, "2gis"))
    assert platform is not None
    assert platform.external_id == "70000000000000001"
    assert platform.access_status == "verified"
    item = session.scalar(select(ChangeItem))
    assert item is not None
    assert item.field == "hours"
    assert item.before is None
    assert item.after == "10:00-20:00"
    assert item.is_nap is False
    package = session.get(ChangePackage, item.package_id)
    assert package is not None
    assert package.status == "draft"
    assert "—" not in (package.summary_for_client or "")

    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "ready"
    assert report.missing_company_information == ()
    assert report.missing_materials == ()
    assert report.missing_owner_permissions == ()
    assert report.unresolved_ambiguities == ()
    require_execution_allowed(session, order.id)
    assert order.stage == "created"
    assert _count(session, ChangeLog) == 0
    assert _count(session, Invoice) == 0
    assert _count(session, Job) == 0

    again = _spec(key="ready-2")
    again.pop("company")
    again["company_id"] = company.id
    again["messages"] = [{"id": "m2", "text": again["messages"][0]["text"]}]
    for fact in again["facts"]:
        fact["source_ref"] = "msg:m2"
    again["changes"][0]["source_ref"] = "msg:m2"
    again["materials"][0]["ref"] = "logo2"
    again["materials"][1]["ref"] = "hall2"
    second = create_test_order(session, again)
    assert second.company_id == company.id
    assert second.stage == "created"
    assert _count(session, Company) == 1
    assert _count(session, Order) == 2


def test_missing_permissions(session: Session) -> None:
    spec = _spec(key="perm-1", platforms=[{
        "platform": "Google Maps",
        "action": "update",
        "external_id": "g-1",
        "owner_authorization": "invited",
    }])
    spec["changes"][0]["platform"] = "Google Maps"
    order = create_test_order(session, spec)
    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "not_ready"
    assert report.missing_owner_permissions == ("google",)
    with pytest.raises(PermissionError):
        require_execution_allowed(session, order.id)
    assert order.stage == "created"
    assert _count(session, ChangeLog) == 0


def test_missing_materials(session: Session) -> None:
    spec = _spec(key="mat-1")
    spec["materials"] = []
    order = create_test_order(session, spec)
    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "not_ready"
    assert report.missing_materials == ("logo", "photos")
    with pytest.raises(PermissionError):
        require_execution_allowed(session, order.id)


def test_missing_company_information(session: Session) -> None:
    spec = _spec(key="info-1")
    spec["facts"] = [fact for fact in spec["facts"] if fact["field"] != "address"]
    order = create_test_order(session, spec)
    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "not_ready"
    assert "address" in report.missing_company_information
    assert "address" not in report.unresolved_ambiguities


def test_unverified_fact_is_an_ambiguity(session: Session) -> None:
    spec = _spec(key="fact-1")
    for fact in spec["facts"]:
        if fact["field"] == "address":
            fact["approved"] = False
    order = create_test_order(session, spec)
    profile = session.scalar(select(CompanyProfile).where(CompanyProfile.company_id == order.company_id))
    assert profile is not None
    assert profile.data["address"]["needs_confirmation"] is True
    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "not_ready"
    assert "address" in report.unresolved_ambiguities
    assert "address" not in report.missing_company_information


def test_create_versus_update(session: Session) -> None:
    created = create_test_order(
        session,
        {
            "key": "create-1",
            "company": {"id_2gis": "create-company", "name": "Create Salon", "city": "Алматы"},
            "platforms": [{"platform": "Google Maps", "action": "create", "owner_authorization": "none"}],
        },
    )
    created_row = session.get(CompanyPlatformId, (created.company_id, "google"))
    assert created_row is not None
    assert created_row.external_id is None
    assert created_row.public_url is None
    intake = session.get(Intake, created.id)
    assert intake is not None
    assert intake.checklist["platforms"]["google"]["action"] == "create"

    updated = create_test_order(
        session,
        {
            "key": "update-1",
            "company": {"id_2gis": "update-company", "name": "Update Salon", "city": "Алматы"},
            "platforms": [
                {
                    "platform": "Yandex Maps",
                    "action": "update",
                    "public_url": "https://yandex.kz/maps/org/1",
                    "owner_authorization": "invited",
                }
            ],
        },
    )
    updated_row = session.get(CompanyPlatformId, (updated.company_id, "yandex"))
    assert updated_row is not None
    assert updated_row.external_id is None
    assert updated_row.public_url == "https://yandex.kz/maps/org/1"
    assert updated_row.access_status == "invited"

    before = _count(session, Company)
    with pytest.raises(OrderIntakeError, match="update needs a listing id"):
        create_test_order(
            session,
            {
                "key": "update-blank",
                "company": {"id_2gis": "update-blank", "name": "Blank", "city": "Алматы"},
                "platforms": [{"platform": "2gis", "action": "update"}],
            },
        )
    with pytest.raises(OrderIntakeError, match="create does not take an existing listing"):
        create_test_order(
            session,
            {
                "key": "create-with-id",
                "company": {"id_2gis": "create-with-id", "name": "Has Id", "city": "Алматы"},
                "platforms": [{"platform": "2gis", "action": "create", "external_id": "999"}],
            },
        )
    assert _count(session, Company) == before


def test_duplicate_prevention(session: Session) -> None:
    create_test_order(session, _spec(key="dup-1"))
    second = _spec(key="dup-1")
    second["company"] = {"id_2gis": "70000000000000099", "name": "Other", "city": "Алматы"}
    second["messages"][0]["id"] = "m-dup"
    for fact in second["facts"]:
        fact["source_ref"] = "msg:m-dup"
    second["changes"][0]["source_ref"] = "msg:m-dup"
    with pytest.raises(DuplicateOrderError, match="dup-1"):
        create_test_order(session, second)
    assert _count(session, Order) == 1
    assert _count(session, Deal) == 1


def test_transaction_rollback(session: Session) -> None:
    first = create_test_order(session, _spec(key="roll-1"))
    second = _spec(key="roll-1")
    second["company"] = {"id_2gis": "rolled-back-company", "name": "Rolled", "city": "Алматы"}
    second["messages"][0]["id"] = "m-roll"
    for fact in second["facts"]:
        fact["source_ref"] = "msg:m-roll"
    second["changes"][0]["source_ref"] = "msg:m-roll"
    with pytest.raises(DuplicateOrderError):
        create_test_order(session, second)
    assert session.scalar(select(Company).where(Company.id_2gis == "rolled-back-company")) is None
    assert session.get(Company, first.company_id) is not None
    assert _count(session, Intake) == 1
    assert _count(session, Order) == 1


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ({"key": "bad-name", "company": {"id_2gis": "x", "name": "  ", "city": "Алматы"}, "platforms": [{"platform": "2gis", "action": "create"}]}, "company name"),
        ({"key": "bad-city", "company": {"id_2gis": "x", "name": "Salon", "city": ""}, "platforms": [{"platform": "2gis", "action": "create"}]}, "city"),
        ({"key": "bad-address", "company": {"id_2gis": "x", "name": "Salon", "city": "Алматы", "address": "ул. Абая 1"}, "platforms": [{"platform": "2gis", "action": "create"}]}, "client facts"),
        ({"key": "no-company", "platforms": [{"platform": "2gis", "action": "create"}]}, "company identity"),
        ({"key": "no-work", "company": {"id_2gis": "x", "name": "Salon", "city": "Алматы"}}, "requested work"),
        ({"key": "bek", "whatsapp": "+7700", "company": {"id_2gis": "x", "name": "Salon", "city": "Алматы"}, "platforms": [{"platform": "2gis", "action": "create"}]}, "out of scope"),
    ],
)
def test_invalid_company_information(session: Session, spec: dict, match: str) -> None:
    before = _count(session, Company)
    with pytest.raises(OrderIntakeError, match=match):
        create_test_order(session, spec)
    assert _count(session, Company) == before


def test_company_name_is_not_a_service(session: Session) -> None:
    spec = {
        "key": "name-service",
        "company": {"id_2gis": "name-service", "name": "Salon Test", "city": "Алматы"},
        "platforms": [{"platform": "2gis", "action": "create"}],
        "messages": [{"id": "m1", "text": "Услуга Salon Test"}],
        "facts": [{"field": "services", "value": [{"name": "Salon Test"}], "source_ref": "msg:m1", "approved": True}],
    }
    with pytest.raises(OrderIntakeError, match="not a service"):
        create_test_order(session, spec)
    assert _count(session, Company) == 0


def test_unknown_company_is_rejected(session: Session) -> None:
    spec = _spec(key="missing-co", company_id=999, company=None)
    with pytest.raises(OrderIntakeError, match="company not found"):
        create_test_order(session, spec)
    assert _count(session, Order) == 0


def test_state_transition_compatibility(session: Session) -> None:
    order = create_test_order(session, _spec(key="stage-1"))
    assert order.stage == "created"
    assert allowed(order, "created", "prepay_invoiced") is True
    assert allowed(order, "created", "intake") is False
    assert allowed(order, "created", "applying") is False
    with pytest.raises(IllegalTransition):
        transition(order, "intake", session)
    assert order.stage == "created"
    assert _count(session, Invoice) == 0
    assert _count(session, EventOut) == 0
    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "ready"
    assert order.stage == "created"


def test_nap_without_consent_stays_unresolved(session: Session) -> None:
    spec = _spec(key="nap-1")
    spec["changes"] = [
        {"platform": "2GIS", "field": "address", "after": "ул. Абая 10", "source_ref": "msg:m1"}
    ]
    order = create_test_order(session, spec)
    item = session.scalar(select(ChangeItem))
    assert item is not None
    assert item.is_nap is True
    assert item.nap_consent_ref is None
    report = check_readiness(session, order.id)
    assert report is not None
    assert report.status == "not_ready"
    assert "nap consent missing: 2gis.address" in report.unresolved_ambiguities


def test_password_is_not_stored(session: Session) -> None:
    spec = _spec(key="secret-1")
    spec["credentials"] = [{"platform": "2gis", "password": "secret-value"}]
    with pytest.raises(OrderIntakeError, match="passwords are not accepted"):
        create_test_order(session, spec)
    assert _count(session, Credential) == 0

    import base64

    spec = _spec(key="cipher-1")
    spec["credentials"] = [
        {"platform": "2gis", "login_hint": "o***", "ciphertext_b64": base64.b64encode(b"sealed").decode()}
    ]
    order = create_test_order(session, spec)
    row = session.scalar(select(Credential).where(Credential.company_id == order.company_id))
    assert row is not None
    assert row.ciphertext == b"sealed"
    assert row.login_hint == "o***"
    assert b"password" not in row.ciphertext


def test_cli_commands(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    path = tmp_path / "order.json"
    path.write_text(json.dumps(_spec(key="cli-1")), encoding="utf-8")
    assert main(["create-test-order", "--spec", str(path)], factory=factory) == 0
    created = capsys.readouterr().out.strip()
    assert created.startswith("order ")
    assert created.endswith("stage created")
    order_id = created.split()[1]

    assert main(["order-status", "--order-id", order_id], factory=factory) == 0
    status = capsys.readouterr().out
    assert "stage created" in status
    assert "platform 2gis update verified" in status
    assert "30 000 ₸" in status

    assert main(["check-order-readiness", "--order-id", order_id], factory=factory) == 0
    assert "status ready" in capsys.readouterr().out

    thin = _spec(key="cli-2")
    thin["materials"] = []
    thin["company"] = {"id_2gis": "cli-2-company", "name": "Thin", "city": "Алматы"}
    thin["platforms"] = [
        {
            "platform": "2GIS",
            "action": "update",
            "external_id": "cli-2-company",
            "owner_authorization": "verified",
        }
    ]
    thin["messages"] = [{"id": "m-cli", "text": thin["messages"][0]["text"]}]
    for fact in thin["facts"]:
        fact["source_ref"] = "msg:m-cli"
    thin["changes"][0]["source_ref"] = "msg:m-cli"
    path.write_text(json.dumps(thin), encoding="utf-8")
    assert main(["create-test-order", "--spec", str(path)], factory=factory) == 0
    thin_id = capsys.readouterr().out.strip().split()[1]
    assert main(["check-order-readiness", "--order-id", thin_id], factory=factory) == 1
    assert "missing materials: logo, photos" in capsys.readouterr().out


def test_local_api(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "local-order-test-token"
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", token)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)

    def override() -> Iterator[Session]:
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            anonymous = client.post("/api/v1/local/orders", json=_spec(key="api-open"))
            assert anonymous.status_code == 401
            assert client.get("/api/v1/local/orders/1").status_code == 401
            assert client.get("/api/v1/local/orders/1/readiness").status_code == 401
            missing = client.post("/api/v1/local/orders", headers=headers, json={"key": "api-bad"})
            assert missing.status_code == 400
            created = client.post("/api/v1/local/orders", headers=headers, json=_spec(key="api-1"))
            assert created.status_code == 200
            body = created.json()
            assert body["stage"] == "created"
            assert body["readiness"]["status"] == "ready"
            order_id = body["order_id"]
            status = client.get(f"/api/v1/local/orders/{order_id}", headers=headers)
            assert status.status_code == 200
            assert status.json()["platforms"][0]["owner_authorization"] == "verified"
            ready = client.get(f"/api/v1/local/orders/{order_id}/readiness", headers=headers)
            assert ready.status_code == 200
            assert ready.json()["status"] == "ready"
            duplicate_spec = _spec(key="api-1")
            duplicate_spec["company"] = {"id_2gis": "api-other", "name": "Other", "city": "Алматы"}
            duplicate = client.post("/api/v1/local/orders", headers=headers, json=duplicate_spec)
            assert duplicate.status_code == 409
    finally:
        app.dependency_overrides.clear()


def _iter_api_routes(routes: list[object]) -> Iterator[APIRoute]:
    """Yield API routes from a flat app and from Starlette included routers."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            yield from _iter_api_routes(nested)


def test_local_routes_are_not_the_bek_api() -> None:
    paths = {route.path for route in _iter_api_routes(app.routes)}
    assert "/api/v1/local/orders" in paths
    assert "/api/v1/local/orders/{order_id}" in paths
    assert "/api/v1/local/orders/{order_id}/readiness" in paths
    assert "/api/v1/orders" not in paths
    assert "/health" in paths


def test_intake_module_does_not_call_browser_or_llm() -> None:
    tree = ast.parse((ROOT / "zhol" / "orders" / "intake.py").read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    joined = " ".join(modules)
    assert "browser" not in joined
    assert "llm" not in joined
    assert "playwright" not in joined

