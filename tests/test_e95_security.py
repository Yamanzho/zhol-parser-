"""E9.5 local security regressions. No live cabinets, no paid model calls."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from fastapi.routing import APIRoute

from zhol.api.admin import accept_admin, require_admin
from zhol.api.admin_actions import require_action_header
from zhol.api.main import app
from zhol.browser.live import live_enabled
from zhol.db.base import Base
from zhol.db.models import Credential
from zhol.llm.config import ConfigError, check_startup
from zhol.redact import redact, strip_secrets

ROOT = Path(__file__).resolve().parents[1]
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
LIVE_FLAGS = ("ZHOL_SCOUT_LIVE", "ZHOL_2GIS_CABINET", "ZHOL_YANDEX_CABINET", "ZHOL_GOOGLE_CABINET")
PLAINTEXT_COLUMNS = {"password", "passwd", "otp", "sms_code", "auth_code", "storage_state"}


def _calls(route: APIRoute, target: object) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is target:
            return True
        stack.extend(dep.dependencies)
    return False


def test_replaced_admin_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = "previous-admin-token"
    current = "current-admin-token"
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", previous)
    accept_admin(f"Bearer {previous}")
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", current)
    with pytest.raises(HTTPException) as rejected:
        accept_admin(f"Bearer {previous}")
    assert rejected.value.status_code == 401
    accept_admin(f"Bearer {current}")
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", "  ")
    with pytest.raises(HTTPException) as disabled:
        accept_admin(f"Bearer {current}")
    assert disabled.value.status_code == 503


def _iter_api_routes(routes: list[object]) -> list[APIRoute]:
    """Yield API routes from a flat app and from Starlette included routers."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            found.extend(_iter_api_routes(nested))
    return found


def test_admin_routes_require_auth_and_mutations_require_the_header() -> None:
    routes = _iter_api_routes(list(app.routes))
    admin_routes = [route for route in routes if route.path.startswith("/api/v1/admin")]
    assert admin_routes
    mutations = [route for route in admin_routes if route.methods & MUTATING]
    assert mutations
    local_paths = {"/api/v1/admin/local-session", "/api/v1/admin/local-session/logout"}
    for route in admin_routes:
        if route.path in local_paths:
            assert not _calls(route, require_admin), route.path
            continue
        assert _calls(route, require_admin), route.path
    for route in mutations:
        assert _calls(route, require_action_header), route.path
    health = next(route for route in routes if route.path == "/health")
    assert not _calls(health, require_admin)
    local_orders = [route for route in routes if route.path.startswith("/api/v1/local/orders")]
    assert {route.path for route in local_orders} == {
        "/api/v1/local/orders",
        "/api/v1/local/orders/{order_id}",
        "/api/v1/local/orders/{order_id}/readiness",
    }
    for route in local_orders:
        assert _calls(route, require_admin), route.path
    require_action_header(x_zhol_request="admin")
    with pytest.raises(HTTPException) as missing:
        require_action_header(x_zhol_request=None)
    assert missing.value.status_code == 403


def test_responses_and_errors_do_not_keep_secret_values() -> None:
    hidden = "super-secret-value"
    cleaned = strip_secrets(
        {
            "name": "Salon",
            "password": hidden,
            "token": hidden,
            "ciphertext": hidden,
            "note": f"Authorization: Bearer {hidden}",
        }
    )
    assert cleaned == {"name": "Salon", "note": "Authorization: Bearer [redacted]"}
    assert hidden not in redact(f"token={hidden}")
    assert hidden not in redact(f"password: {hidden}")


def test_owner_credentials_and_auth_codes_have_no_plaintext_columns() -> None:
    assert "ciphertext" in {column.name for column in Credential.__table__.columns}
    for table in Base.metadata.tables.values():
        names = {column.name.lower() for column in table.columns}
        assert names.isdisjoint(PLAINTEXT_COLUMNS), table.name


def test_production_r6_config_fail_closes() -> None:
    with pytest.raises(ConfigError) as exc:
        check_startup(config_dir=ROOT / "config", root=ROOT, env={})
    text = str(exc.value)
    assert "configuration is not usable" in text
    assert "decision R6" in text


def test_live_collection_and_cabinets_stay_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIVE_FLAGS:
        monkeypatch.delenv(name, raising=False)
    assert live_enabled() is False


def test_compose_keeps_data_and_publishes_only_localhost_admin_ports() -> None:
    main = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    offline = yaml.safe_load((ROOT / "docker-compose.offline.yml").read_text(encoding="utf-8"))
    services = main["services"]
    assert services["db"]["volumes"] == ["pgdata:/var/lib/postgresql/data"]
    assert main["volumes"]["pgdata"] is None
    assert "db" not in offline["services"]
    assert "pgdata" not in (offline.get("volumes") or {})
    assert services["api"]["ports"] == ["127.0.0.1:${API_PORT:-18080}:8000"]
    assert services["web"]["ports"] == ["127.0.0.1:${WEB_PORT:-15173}:5173"]
    for name in ("db", "worker", "browser-worker", "scheduler"):
        assert "ports" not in services[name]
    for name in ("api", "worker", "browser-worker"):
        assert services[name]["build"] == "."
    published = "\n".join(
        [
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
            (ROOT / "docker-compose.offline.yml").read_text(encoding="utf-8"),
        ]
    )
    for name in LIVE_FLAGS:
        assert name not in published
    for service in offline["services"].values():
        assert "ports" not in service
