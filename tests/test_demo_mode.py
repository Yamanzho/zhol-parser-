"""Demo API contract. No database and no map hosts."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from zhol.demo.server import DemoLaunchError, DemoServer, demo_is_running, require_loopback, serve
from zhol.demo.store import DemoStore
from zhol.integration.schema import IntegrationError

ROOT = Path(__file__).resolve().parents[1]


def test_a_running_demo_is_reused_and_a_foreign_port_is_reported() -> None:
    first = DemoServer("127.0.0.1", 0, step=0)
    first.start()
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not demo_is_running(first.port):
            time.sleep(0.02)
        assert demo_is_running(first.port) is True
        note = serve("127.0.0.1", first.port, step=0, open_browser=False)
        assert "уже запущена" in note
        with pytest.raises(DemoLaunchError) as caught:
            serve("127.0.0.1", holder.getsockname()[1], step=0, open_browser=False)
        assert "занят" in caught.value.message
        assert "не запущена" in caught.value.message
    finally:
        holder.close()
        first.close()


def test_a_zero_limit_is_rejected() -> None:
    server = DemoServer("127.0.0.1", 0, step=0)
    server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        response = _open(origin, "POST", "/api/v1/jobs", {"platform": "2gis", "city": "Алматы", "query": "салон", "limit": 0})
        assert response.status == 422
        body = json.loads(response.read().decode("utf-8"))
        assert body["error"]["code"] == "validation_error"
    finally:
        server.close()


def test_demo_refuses_a_public_bind() -> None:
    with pytest.raises(IntegrationError):
        require_loopback("0.0.0.0")


def test_demo_sources_do_not_name_a_database_or_a_map_site() -> None:
    for relative in ("zhol/demo/server.py", "zhol/demo/store.py", "zhol/demo/catalog.py"):
        text = (ROOT / relative).read_text(encoding="utf-8").casefold()
        for banned in ("sqlalchemy", "psycopg", "selenium", "2gis.kz", "yandex.ru", "google.com", "database_url"):
            assert banned not in text


def test_store_does_not_open_a_network_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    store = DemoStore(0)
    body, duplicate = store.create({"platform": "2gis", "city": "Алматы", "query": "салон красоты", "limit": 2})
    assert duplicate is False
    assert body["status"] == "running"
    store.pump()
    assert store.job(body["id"])["collected"] == 1


def test_demo_collect_search_and_export(tmp_path: Path) -> None:
    server = DemoServer("127.0.0.1", 0, step=0)
    server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        created = _json(origin, "POST", "/api/v1/jobs", {"platform": "2gis", "city": "Алматы", "query": "салон красоты", "limit": 2, "idempotency_key": "salon-1"})
        assert created["status"] == "running"
        again = _json(origin, "POST", "/api/v1/jobs", {"platform": "2gis", "city": "Алматы", "query": "салон красоты", "limit": 2, "idempotency_key": "salon-1"})
        assert again["duplicate"] is True
        assert again["id"] == created["id"]
        conflict = _open(origin, "POST", "/api/v1/jobs", {"platform": "google", "city": "Алматы", "query": "салон", "limit": 1, "idempotency_key": "salon-1"})
        assert conflict.status == 409
        server.store.pump()
        paused = _json(origin, "POST", f"/api/v1/jobs/{created['id']}/pause")
        assert paused["status"] == "paused"
        server.store.pump()
        held = _json(origin, "GET", f"/api/v1/jobs/{created['id']}")
        assert held["collected"] == 1
        resumed = _json(origin, "POST", f"/api/v1/jobs/{created['id']}/resume")
        assert resumed["status"] == "running"
        server.store.pump()
        done = _json(origin, "GET", f"/api/v1/jobs/{created['id']}")
        assert done["status"] == "completed"
        assert done["collected"] == 2
        results = _json(origin, "GET", f"/api/v1/jobs/{created['id']}/results?limit=1&offset=0")
        assert results["total"] == 2
        assert len(results["items"]) == 1
        assert results["next_offset"] == 1
        found = _json(origin, "GET", "/api/v1/companies?q=" + quote("Абая") + "&platform=2gis&city=" + quote("Алматы"))
        assert found["total"] == 1
        assert found["items"][0]["phone"] == "+7 701 111 22 33"
        meta = _json(origin, "GET", "/api/v1/meta")
        assert meta["demo"] is True
        assert meta["mode"] == "demo"
        sheet = _open(origin, "GET", f"/api/v1/jobs/{created['id']}/export?format=xlsx")
        assert sheet.status == 200
        book = zipfile.ZipFile(BytesIO(sheet.read()))
        assert "xl/worksheets/sheet1.xml" in book.namelist()
        assert "Салон Абая" in book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        external = subprocess.run(
            [sys.executable, str(ROOT / "examples" / "python_client.py"), "--api", origin, "results", str(created["id"])],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(external.stdout)
        assert payload["total"] == 2
    finally:
        server.close()


def test_stop_keeps_partial_demo_rows() -> None:
    server = DemoServer("127.0.0.1", 0, step=0)
    server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        created = _json(origin, "POST", "/api/v1/jobs", {"platform": "2gis", "city": "Алматы", "query": "салон красоты", "limit": 2})
        server.store.pump()
        stopped = _json(origin, "POST", f"/api/v1/jobs/{created['id']}/stop")
        assert stopped["status"] == "stopped"
        server.store.pump()
        assert _json(origin, "GET", f"/api/v1/jobs/{created['id']}")["collected"] == 1
    finally:
        server.close()


class _Response:
    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self._raw = raw

    def read(self) -> bytes:
        return self._raw


def _open(origin: str, method: str, path: str, body: dict | None = None) -> _Response:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    request = Request(origin + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return _Response(response.status, response.read())
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "read"):
            return _Response(int(exc.code), exc.read())
        raise


def _json(origin: str, method: str, path: str, body: dict | None = None) -> dict:
    response = _open(origin, method, path, body)
    raw = response.read()
    assert response.status < 400, raw
    parsed = json.loads(raw.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed
