"""E9.6 local session. Production bearer checks stay closed without the local flag."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from zhol.api.admin import accept_admin
from zhol.api.local_access import COOKIE, local_access_enabled, reset_local_sessions
from zhol.api.main import app

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
GATE = "test-local-gate"
ENGLISH = (
    "Sign out",
    "Download Excel",
    "Start batch",
    "Nothing stored",
    "Loading.",
    "Live publication",
    "Human review",
    "Create local order",
    "Back to companies",
    "Scout batches",
)


@pytest.fixture
def local_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_LOCAL_ACCESS", "1")
    monkeypatch.setenv("ZHOL_LOCAL_GATE", GATE)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    reset_local_sessions()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client
    reset_local_sessions()


def _headers(host: str = "127.0.0.1", gate: str = GATE, csrf: bool = True) -> dict[str, str]:
    headers = {"Host": host}
    if csrf:
        headers["X-Zhol-Request"] = "admin"
    if gate:
        headers["X-Zhol-Local-Gate"] = gate
    return headers


def _request(cookie: str, host: str = "127.0.0.1", peer: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/admin/summary",
        "raw_path": b"/api/v1/admin/summary",
        "query_string": b"",
        "headers": [(b"host", host.encode()), (b"cookie", f"{COOKIE}={cookie}".encode())],
        "client": (peer, 5000),
        "server": ("127.0.0.1", 80),
    }
    return Request(scope)


def test_production_mode_hides_local_session(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_LOCAL_ACCESS", raising=False)
    monkeypatch.delenv("ZHOL_LOCAL_GATE", raising=False)
    assert local_access_enabled() is False
    hidden = local_client.post("/api/v1/admin/local-session", headers=_headers())
    assert hidden.status_code == 404
    accept_admin(f"Bearer {TOKEN}")
    with pytest.raises(HTTPException) as denied:
        accept_admin(None, _request("stolen-cookie"))
    assert denied.value.status_code == 401


def test_empty_gate_stays_closed(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHOL_LOCAL_GATE", " ")
    assert local_client.post("/api/v1/admin/local-session", headers=_headers(gate=" ")).status_code == 404


def test_local_session_rejects_remote_peers_and_keeps_csrf(local_client: TestClient) -> None:
    missing = local_client.post("/api/v1/admin/local-session", headers=_headers(csrf=False))
    assert missing.status_code == 403
    wrong_gate = local_client.post("/api/v1/admin/local-session", headers=_headers(gate="other-gate"))
    assert wrong_gate.status_code == 403
    remote = TestClient(app, client=("203.0.113.8", 4000))
    spoofed = remote.post("/api/v1/admin/local-session", headers=_headers(host="api"))
    assert spoofed.status_code == 403
    public_host = remote.post("/api/v1/admin/local-session", headers=_headers(host="203.0.113.8"))
    assert public_host.status_code == 403


def test_local_session_cookie_is_not_the_admin_token(local_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = local_client.post("/api/v1/admin/local-session", headers=_headers())
    assert opened.status_code == 200
    assert opened.json() == {"local_access": True}
    issued = opened.headers.get("set-cookie", "")
    assert "httponly" in issued.lower()
    assert "samesite=strict" in issued.lower()
    cookie = issued.split(";", 1)[0].split("=", 1)[1]
    assert cookie != TOKEN
    assert cookie != GATE
    assert TOKEN not in opened.text
    assert GATE not in opened.text
    accept_admin(None, _request(cookie))
    with pytest.raises(HTTPException) as wrong_bearer:
        accept_admin("Bearer wrong-token", _request(cookie))
    assert wrong_bearer.value.status_code == 401
    monkeypatch.delenv("ZHOL_LOCAL_ACCESS", raising=False)
    with pytest.raises(HTTPException) as closed:
        accept_admin(None, _request(cookie))
    assert closed.value.status_code == 401


def test_compose_keeps_local_access_off_the_production_file() -> None:
    main = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    offline = yaml.safe_load((ROOT / "docker-compose.offline.yml").read_text(encoding="utf-8"))
    api_env = main["services"]["api"]["environment"]
    assert "ZHOL_LOCAL_ACCESS" not in api_env
    assert "ZHOL_LOCAL_GATE" not in api_env
    assert offline["services"]["api"]["environment"]["ZHOL_LOCAL_ACCESS"] == "1"
    assert offline["services"]["api"]["environment"]["ZHOL_LOCAL_GATE"] == "${ZHOL_LOCAL_GATE:-}"
    assert offline["services"]["web"]["environment"]["ZHOL_LOCAL_GATE"] == "${ZHOL_LOCAL_GATE:-}"
    assert main["services"]["api"]["ports"] == ["127.0.0.1:${API_PORT:-18080}:8000"]
    assert main["services"]["web"]["ports"] == ["127.0.0.1:${WEB_PORT:-15173}:5173"]
    published = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ZHOL_SCOUT_LIVE" not in published
    assert "0.0.0.0:" not in published


def test_operator_screens_are_russian() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "web" / "src").glob("*.tsx"))
    for phrase in ENGLISH:
        assert phrase not in sources
    assert "Офлайн-симуляция" in sources
    assert "Реальная публикация" in sources
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'lang="ru"' in page


def test_launchers_do_not_delete_volumes_or_print_secrets() -> None:
    start = (ROOT / "Запустить Zhol.command").read_text(encoding="utf-8")
    stop = (ROOT / "Остановить Zhol.command").read_text(encoding="utf-8")
    for script in (start, stop):
        assert "down -v" not in script
        assert "volume rm" not in script
        assert "echo \"$ZHOL_ADMIN_TOKEN\"" not in script
        assert "echo \"$ZHOL_LOCAL_GATE\"" not in script
    assert "docker-compose.offline.yml" in start
    assert "alembic upgrade head" in start
    assert "127.0.0.1" in start
    assert "Docker Desktop не запущен" in start
    assert "compose stop" in stop or " stop " in stop
    guide = (ROOT / "docs" / "USER_GUIDE_RU.md").read_text(encoding="utf-8")
    assert "Запустить Zhol.command" in guide
    assert "Остановить Zhol.command" in guide
    assert "офлайн" in guide.casefold()
