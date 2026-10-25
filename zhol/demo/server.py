"""Local demo. Fixture companies only, loopback only, no production database."""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from zhol.demo.store import DemoStore
from zhol.integration.schema import DEMO_EXPORT_NOTES, IntegrationError, error_body, export_bytes, parse_page

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "demo" / "static"
LOOPBACK = {"127.0.0.1", "::1"}
OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Zhol demo", "version": "v1"},
    "paths": {
        "/api/v1/jobs": {"post": {"summary": "Create a simulated collection job"}},
        "/api/v1/jobs/{job_id}": {"get": {"summary": "Job status"}},
        "/api/v1/jobs/{job_id}/pause": {"post": {"summary": "Pause"}},
        "/api/v1/jobs/{job_id}/resume": {"post": {"summary": "Resume"}},
        "/api/v1/jobs/{job_id}/stop": {"post": {"summary": "Stop"}},
        "/api/v1/jobs/{job_id}/results": {"get": {"summary": "Collected companies"}},
        "/api/v1/jobs/{job_id}/export": {"get": {"summary": "Excel, CSV, or JSON"}},
        "/api/v1/companies": {"get": {"summary": "Search collected demo companies"}},
    },
}
FALLBACK_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Жол, демонстрация</title>
<body style="font-family: sans-serif; margin: 40px">
<h1>Демонстрационный режим</h1>
<p>Интерфейс ещё не собран. API демонстрации уже отвечает на этом компьютере.</p>
<p>Сбор идёт по учебным компаниям. Карты и рабочая база не используются.</p>
</body>
"""


def require_loopback(host: str) -> str:
    if host not in LOOPBACK:
        raise IntegrationError(400, "rejected", "Демонстрация слушает только localhost и не открывается в сети.")
    return host


def _job_id(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 4 or parts[:3] != ["api", "v1", "jobs"]:
        return None
    if not parts[3].isdigit():
        return None
    return int(parts[3])


def _optional_int(query: dict[str, list[str]], name: str) -> int | None:
    raw = query.get(name)
    if not raw or raw[0] == "":
        return None
    if not raw[0].lstrip("-").isdigit():
        raise IntegrationError(422, "validation_error", "Номер страницы должен быть целым числом.")
    return int(raw[0])


def _optional_text(query: dict[str, list[str]], name: str) -> str | None:
    raw = query.get(name)
    if not raw:
        return None
    text = raw[0].strip()
    return text or None


def _handler(store: DemoStore, static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def _route(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query)
                if path.startswith("/api/") or path in {"/health", "/openapi.json"}:
                    self._api(method, path, query)
                    return
                self._static(path)
            except IntegrationError as exc:
                self._send(exc.status, error_body(exc.code, exc.message))
            except Exception:
                self._send(500, error_body("rejected", "Запрос не выполнен."))

        def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
            if method == "GET" and path == "/health":
                self._send(200, {"status": "ok", "mode": "demo"})
                return
            if method == "GET" and path == "/openapi.json":
                self._send(200, OPENAPI)
                return
            if method == "GET" and path == "/api/v1/meta":
                self._send(200, store.meta())
                return
            if method == "POST" and path == "/api/v1/jobs":
                document, duplicate = store.create(self._json())
                self._send(200 if duplicate else 201, document)
                return
            if method == "GET" and path == "/api/v1/jobs":
                limit, offset = parse_page(_optional_int(query, "limit"), _optional_int(query, "offset"))
                self._send(200, store.jobs_page(limit, offset))
                return
            job_id = _job_id(path)
            if job_id is not None and method == "GET" and path.endswith("/results"):
                limit, offset = parse_page(_optional_int(query, "limit"), _optional_int(query, "offset"))
                self._send(200, store.results(job_id, limit, offset))
                return
            if job_id is not None and method == "GET" and path.endswith("/export"):
                fmt = (query.get("format") or ["xlsx"])[0]
                payload, media, filename = export_bytes(fmt, store.export_rows(job_id, "", None, None), notes=DEMO_EXPORT_NOTES)
                self._file(200, payload, media, filename)
                return
            if job_id is not None and method == "GET" and path == f"/api/v1/jobs/{job_id}":
                self._send(200, store.job(job_id))
                return
            if job_id is not None and method == "POST":
                for action in ("pause", "resume", "stop"):
                    if path.endswith("/" + action):
                        self._send(200, store.control(job_id, action))
                        return
            if method == "GET" and path == "/api/v1/companies":
                limit, offset = parse_page(_optional_int(query, "limit"), _optional_int(query, "offset"))
                self._send(
                    200,
                    store.companies(
                        _optional_text(query, "q") or "",
                        _optional_text(query, "platform"),
                        _optional_text(query, "city"),
                        limit,
                        offset,
                    ),
                )
                return
            if method == "GET" and path.startswith("/api/v1/companies/"):
                raw = path.removeprefix("/api/v1/companies/")
                if not raw.isdigit():
                    raise IntegrationError(404, "not_found", "Компания не найдена.")
                self._send(200, store.company(int(raw)))
                return
            if method == "GET" and path == "/api/v1/export":
                fmt = (query.get("format") or ["xlsx"])[0]
                rows = store.export_rows(None, _optional_text(query, "q") or "", _optional_text(query, "platform"), _optional_text(query, "city"))
                payload, media, filename = export_bytes(fmt, rows, notes=DEMO_EXPORT_NOTES)
                self._file(200, payload, media, filename)
                return
            raise IntegrationError(404, "not_found", "Такого адреса в API нет.")

        def _static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            if not relative or ".." in Path(relative).parts:
                raise IntegrationError(404, "not_found", "Файл не найден.")
            root = static_dir.resolve()
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise IntegrationError(404, "not_found", "Файл не найден.")
            if not target.is_file():
                fallback = root / "index.html"
                if fallback.is_file():
                    target = fallback
                else:
                    self._html(FALLBACK_HTML)
                    return
            media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > 65536:
                raise IntegrationError(400, "rejected", "Слишком большое тело запроса.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise IntegrationError(422, "validation_error", "Тело запроса должно быть JSON.") from exc
            if not isinstance(body, dict):
                raise IntegrationError(422, "validation_error", "Тело запроса должно быть объектом JSON.")
            return body

        def _send(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _file(self, status: int, payload: bytes, media: str, filename: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(payload)

        def _html(self, page: str) -> None:
            raw = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


class DemoServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, step: float = 0.35, static_dir: Path | None = None) -> None:
        self.host = require_loopback(host)
        self.store = DemoStore(step)
        self.static_dir = STATIC_DIR if static_dir is None else static_dir
        handler = _handler(self.store, self.static_dir)
        self.httpd = ThreadingHTTPServer((self.host, port), handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self._loop, name="zhol-demo", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def wait(self) -> None:
        try:
            while self.thread.is_alive():
                self.thread.join(0.5)
        except KeyboardInterrupt:
            self.close()

    def close(self) -> None:
        self.store.closed = True
        self.httpd.shutdown()
        self.httpd.server_close()

    def _loop(self) -> None:
        worker = threading.Thread(target=self._pump, name="zhol-demo-pump", daemon=True)
        worker.start()
        self.httpd.serve_forever(poll_interval=0.2)

    def _pump(self) -> None:
        if not self.store.step:
            return
        while not self.store.closed:
            threading.Event().wait(self.store.step)
            if self.store.closed:
                return
            self.store.pump()


def serve(host: str = "127.0.0.1", port: int = 8765, *, step: float = 0.35, open_browser: bool = False) -> None:
    server = DemoServer(host, port, step=step)
    server.start()
    if open_browser:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{server.port}/")
    print(f"Демонстрационный режим: http://127.0.0.1:{server.port}/", flush=True)
    server.wait()
