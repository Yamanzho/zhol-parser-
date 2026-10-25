"""Local demo. Fixture companies only, loopback only, no production database."""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from zhol.demo.catalog import CATALOG, CITIES
from zhol.integration.schema import (
    COLLECTION_LIMIT_DEFAULT,
    COLLECTION_LIMIT_MAX,
    COLLECTION_LIMIT_MIN,
    DEMO_EXPORT_NOTES,
    PLATFORM_LABEL,
    PLATFORMS,
    IntegrationError,
    collection_limit,
    company_record,
    error_body,
    export_bytes,
    job_document,
    normalize_idempotency_key,
    page_body,
    parse_page,
)

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "demo" / "static"
LOOPBACK = {"127.0.0.1", "::1"}
_OPENAPI = {
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


def require_loopback(host: str) -> str:
    if host not in LOOPBACK:
        raise IntegrationError(400, "rejected", "Демонстрация слушает только localhost и не открывается в сети.")
    return host


class DemoStore:
    def __init__(self, step: float = 0.35) -> None:
        self.step = step
        self.lock = threading.Lock()
        self.jobs: dict[int, dict[str, Any]] = {}
        self.collected: list[dict[str, Any]] = []
        self.keys: dict[str, int] = {}
        self.next_job = 1
        self.next_company = 1
        self.closed = False

    def meta(self) -> dict[str, Any]:
        platforms = []
        for platform in PLATFORMS:
            platforms.append(
                {
                    "id": platform,
                    "label": PLATFORM_LABEL[platform],
                    "available": True,
                    "note": "В демонстрации площадка имитируется. Запросы на карты не отправляются.",
                    "cities": list(CITIES),
                }
            )
        return {
            "service": "zhol",
            "api_version": "v1",
            "mode": "demo",
            "demo": True,
            "label": "Демонстрационный режим",
            "bind": "127.0.0.1",
            "platforms": platforms,
            "limit": {"min": COLLECTION_LIMIT_MIN, "max": COLLECTION_LIMIT_MAX, "default": COLLECTION_LIMIT_DEFAULT},
            "export_formats": ["xlsx", "csv", "json"],
            "export_notes": list(DEMO_EXPORT_NOTES),
            "docs": "/openapi.json",
        }

    def create(self, spec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        platform = str(spec.get("platform") or "")
        city = " ".join(str(spec.get("city") or "").split())
        query = " ".join(str(spec.get("query") or "").split())
        raw_limit = spec.get("limit")
        limit = collection_limit(COLLECTION_LIMIT_DEFAULT if raw_limit is None else int(raw_limit))
        key = normalize_idempotency_key(spec.get("idempotency_key"))
        if platform not in PLATFORMS:
            raise IntegrationError(422, "validation_error", "Площадка: 2gis, google или yandex.")
        if not city or len(city) > 80:
            raise IntegrationError(422, "validation_error", "Укажите город не длиннее 80 символов.")
        if city not in CITIES:
            raise IntegrationError(400, "rejected", f"В демонстрации доступны города: {', '.join(CITIES)}.")
        if not query or len(query) > 120:
            raise IntegrationError(422, "validation_error", "Укажите запрос не длиннее 120 символов.")
        with self.lock:
            if key and key in self.keys:
                current = self.jobs[self.keys[key]]
                if not _same(current, platform, city, query, limit):
                    raise IntegrationError(409, "conflict", "Ключ идемпотентности уже использован для другого задания.")
                body = self._document(current)
                body["duplicate"] = True
                return body, True
            matches = [row for row in CATALOG if row["platform"] == platform and row["city"] == city and _query_hit(row, query)]
            chosen = matches[:limit]
            job_id = self.next_job
            self.next_job += 1
            pending = []
            for row in chosen:
                company_id = self.next_company
                self.next_company += 1
                pending.append(
                    company_record(
                        company_id=company_id,
                        name=row["name"],
                        phone=row["phone"],
                        address=row["address"],
                        category=row["category"],
                        city=row["city"],
                        platform=row["platform"],
                        source="demo",
                    )
                )
            job = {
                "id": job_id,
                "status": "running",
                "platform": platform,
                "city": city,
                "query": query,
                "limit": limit,
                "pending": pending,
                "found": [],
                "message": "Сбор выполняется." if pending else "В демонстрационном наборе нет компаний по этому запросу.",
                "error": None,
                "key": key,
            }
            if not pending:
                job["status"] = "completed"
                job["message"] = "В демонстрационном наборе нет компаний по этому запросу."
            self.jobs[job_id] = job
            if key:
                self.keys[key] = job_id
            return self._document(job), False

    def jobs_page(self, limit: int, offset: int) -> dict[str, Any]:
        with self.lock:
            rows = [self._document(job) for job in sorted(self.jobs.values(), key=lambda item: int(item["id"]), reverse=True)]
        return page_body(rows[offset : offset + limit], len(rows), limit, offset)

    def job(self, job_id: int) -> dict[str, Any]:
        with self.lock:
            return self._document(self._require(job_id))

    def control(self, job_id: int, action: str) -> dict[str, Any]:
        with self.lock:
            job = self._require(job_id)
            status = job["status"]
            if action == "pause":
                if status not in {"running", "queued"}:
                    raise IntegrationError(409, "conflict", "Это задание уже нельзя поставить на паузу.")
                job["status"] = "paused"
                job["message"] = "Сбор на паузе."
            elif action == "resume":
                if status != "paused":
                    raise IntegrationError(409, "conflict", "Продолжить можно только задание на паузе.")
                job["status"] = "running"
                job["message"] = "Сбор выполняется."
            elif action == "stop":
                if status in {"completed", "stopped", "failed"}:
                    raise IntegrationError(409, "conflict", "Это задание уже нельзя остановить.")
                job["status"] = "stopped"
                job["pending"] = []
                job["message"] = "Сбор остановлен. Уже показанные компании сохранены в демонстрации."
            else:
                raise IntegrationError(400, "rejected", "Неизвестное действие.")
            return self._document(job)

    def results(self, job_id: int, limit: int, offset: int) -> dict[str, Any]:
        with self.lock:
            job = self._require(job_id)
            found = list(job["found"])
        return page_body(found[offset : offset + limit], len(found), limit, offset)

    def companies(self, q: str, platform: str | None, city: str | None, limit: int, offset: int) -> dict[str, Any]:
        if platform is not None and platform not in PLATFORMS:
            raise IntegrationError(422, "validation_error", "Фильтр площадки: 2gis, google или yandex.")
        with self.lock:
            rows = list(self.collected)
        term = q.casefold().strip()
        filtered = []
        for row in rows:
            if platform and row["platform"] != platform:
                continue
            if city and str(row["city"] or "").casefold() != city.strip().casefold():
                continue
            haystack = " ".join(str(row.get(key) or "") for key in ("name", "phone", "address", "category", "city")).casefold()
            if term and term not in haystack:
                continue
            filtered.append(row)
        filtered.reverse()
        return page_body(filtered[offset : offset + limit], len(filtered), limit, offset)

    def company(self, company_id: int) -> dict[str, Any]:
        with self.lock:
            found = next((row for row in self.collected if row["id"] == company_id), None)
        if found is None:
            raise IntegrationError(404, "not_found", "Компания не найдена.")
        facts = [
            {"label": "Телефон", "text": found["phone"] or "Нет данных"},
            {"label": "Адрес", "text": found["address"] or "Нет данных"},
            {"label": "Категория", "text": found["category"] or "Нет данных"},
            {"label": "Город", "text": found["city"] or "Нет данных"},
            {"label": "Площадка", "text": found["platform_label"]},
            {"label": "Источник", "text": "Демонстрационные данные"},
        ]
        return {"company": found, "facts": facts, "note": "Демонстрационная карточка. Это не данные с карты."}

    def export_rows(self, job_id: int | None, q: str, platform: str | None, city: str | None) -> list[dict[str, Any]]:
        if job_id is not None:
            with self.lock:
                return list(self._require(job_id)["found"])
        body = self.companies(q, platform, city, limit=100, offset=0)
        rows = list(body["items"])
        offset = 100
        while offset < int(body["total"]):
            rows.extend(self.companies(q, platform, city, limit=100, offset=offset)["items"])
            offset += 100
        return rows

    def pump(self) -> None:
        with self.lock:
            if self.closed:
                return
            for job in self.jobs.values():
                if job["status"] != "running":
                    continue
                pending = job["pending"]
                if not pending:
                    job["status"] = "completed"
                    job["message"] = job["message"] or "Сбор завершён."
                    continue
                item = pending.pop(0)
                job["found"].append(item)
                self.collected.append(item)
                if not pending or len(job["found"]) >= int(job["limit"]):
                    job["status"] = "completed"
                    job["pending"] = []
                    job["message"] = "Сбор завершён."
                else:
                    job["message"] = "Сбор выполняется."

    def _require(self, job_id: int) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise IntegrationError(404, "not_found", "Задание не найдено.")
        return job

    def _document(self, job: dict[str, Any]) -> dict[str, Any]:
        collected = len(job["found"])
        limit = int(job["limit"])
        progress = 1.0 if job["status"] == "completed" else (None if not limit else round(collected / limit, 4))
        return job_document(
            {
                "id": job["id"],
                "status": job["status"],
                "platform": job["platform"],
                "city": job["city"],
                "query": job["query"],
                "limit": limit,
                "collected": collected,
                "progress": progress,
                "message": job["message"],
                "error": job["error"],
                "duplicate": False,
                "batch_id": None,
            }
        )


def _same(job: dict[str, Any], platform: str, city: str, query: str, limit: int) -> bool:
    return job["platform"] == platform and job["city"].casefold() == city.casefold() and job["query"].casefold() == query.casefold() and int(job["limit"]) == limit


def _query_hit(row: dict[str, Any], query: str) -> bool:
    folded = query.casefold()
    return folded in str(row["category"]).casefold() or folded in str(row["name"]).casefold()


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
        while not self.store.closed:
            if self.store.step:
                threading.Event().wait(self.store.step)
            if self.store.closed:
                return
            self.store.pump()


def _handler(store: DemoStore, static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

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
                self._send(200, _OPENAPI)
                return
            if method == "GET" and path == "/api/v1/meta":
                self._send(200, store.meta())
                return
            if method == "POST" and path == "/api/v1/jobs":
                document, duplicate = store.create(self._json())
                self._send(200 if duplicate else 201, document)
                return
            if method == "GET" and path == "/api/v1/jobs":
                limit, offset = parse_page(_int(query, "limit"), _int(query, "offset"))
                self._send(200, store.jobs_page(limit, offset))
                return
            job_id = _job_id(path)
            if job_id is not None and method == "GET" and path.endswith("/results"):
                limit, offset = parse_page(_int(query, "limit"), _int(query, "offset"))
                self._send(200, store.results(job_id, limit, offset))
                return
            if job_id is not None and method == "GET" and path.endswith("/export"):
                fmt = (query.get("format") or ["xlsx"])[0]
                payload, media, filename = export_bytes(fmt, store.export_rows(job_id, "", None, None), notes=DEMO_EXPORT_NOTES)
                self._bytes(200, payload, media, filename)
                return
            if job_id is not None and method == "GET" and path == f"/api/v1/jobs/{job_id}":
                self._send(200, store.job(job_id))
                return
            if job_id is not None and method == "POST" and path.endswith("/pause"):
                self._send(200, store.control(job_id, "pause"))
                return
            if job_id is not None and method == "POST" and path.endswith("/resume"):
                self._send(200, store.control(job_id, "resume"))
                return
            if job_id is not None and method == "POST" and path.endswith("/stop"):
                self._send(200, store.control(job_id, "stop"))
                return
            if method == "GET" and path == "/api/v1/companies":
                limit, offset = parse_page(_int(query, "limit"), _int(query, "offset"))
                platform = _text(query, "platform")
                city = _text(query, "city")
                self._send(200, store.companies(_text(query, "q") or "", platform, city, limit, offset))
                return
            if method == "GET" and path.startswith("/api/v1/companies/"):
                raw = path.removeprefix("/api/v1/companies/")
                if not raw.isdigit():
                    raise IntegrationError(404, "not_found", "Компания не найдена.")
                self._send(200, store.company(int(raw)))
                return
            if method == "GET" and path == "/api/v1/export":
                fmt = (query.get("format") or ["xlsx"])[0]
                rows = store.export_rows(None, _text(query, "q") or "", _text(query, "platform"), _text(query, "city"))
                payload, media, filename = export_bytes(fmt, rows, notes=DEMO_EXPORT_NOTES)
                self._bytes(200, payload, media, filename)
                return
            raise IntegrationError(404, "not_found", "Такого адреса в API нет.")

        def _static(self, path: str) -> None:
            if path in {"", "/"}:
                path = "/index.html"
            relative = path.lstrip("/")
            if not relative or ".." in Path(relative).parts:
                raise IntegrationError(404, "not_found", "Файл не найден.")
            root = static_dir.resolve()
            target = (root / relative).resolve()
            if root not in target.parents and target != root:
                raise IntegrationError(404, "not_found", "Файл не найден.")
            if not target.is_file():
                fallback = root / "index.html"
                if fallback.is_file() and path != "/index.html":
                    target = fallback
                else:
                    self._html(_FALLBACK_HTML)
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

        def _bytes(self, status: int, payload: bytes, media: str, filename: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("X-Zhol-Export-Note", DEMO_EXPORT_NOTES[0])
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


def _job_id(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 4 or parts[:3] != ["api", "v1", "jobs"]:
        return None
    if not parts[3].isdigit():
        return None
    return int(parts[3])


def _int(query: dict[str, list[str]], name: str) -> int | None:
    raw = query.get(name)
    if not raw or raw[0] == "":
        return None
    if not raw[0].isdigit():
        raise IntegrationError(422, "validation_error", "Номер страницы должен быть целым числом.")
    return int(raw[0])


def _text(query: dict[str, list[str]], name: str) -> str | None:
    raw = query.get(name)
    if not raw:
        return None
    text = raw[0].strip()
    return text or None


_FALLBACK_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Жол, демонстрация</title>
<body style="font-family: sans-serif; margin: 40px">
<h1>Демонстрационный режим</h1>
<p>Интерфейс ещё не собран. API демонстрации уже отвечает на этом компьютере.</p>
<p>Сбор идёт по учебным компаниям. Карты и рабочая база не используются.</p>
</body>
"""


def serve(host: str = "127.0.0.1", port: int = 8765, *, step: float = 0.35, open_browser: bool = False) -> None:
    server = DemoServer(host, port, step=step)
    server.start()
    if open_browser:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{server.port}/")
    print(f"Демонстрационный режим: http://127.0.0.1:{server.port}/", flush=True)
    server.wait()
