"""In-memory demo jobs. Companies never leave this process."""

from __future__ import annotations

import threading
from typing import Any

from zhol.demo.catalog import CATALOG, CITIES, query_hit, same_job
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
    job_document,
    normalize_idempotency_key,
    page_body,
)


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
        platforms = [
            {
                "id": platform,
                "label": PLATFORM_LABEL[platform],
                "available": True,
                "note": "В демонстрации площадка имитируется. Запросы на карты не отправляются.",
                "cities": list(CITIES),
            }
            for platform in PLATFORMS
        ]
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
                if not same_job(current, platform, city, query, limit):
                    raise IntegrationError(409, "conflict", "Ключ идемпотентности уже использован для другого задания.")
                body = self._document(current)
                body["duplicate"] = True
                return body, True
            matches = [row for row in CATALOG if row["platform"] == platform and row["city"] == city and query_hit(row, query)]
            job_id = self.next_job
            self.next_job += 1
            pending = []
            for row in matches[:limit]:
                company_id = self.next_company
                self.next_company += 1
                pending.append(
                    company_record(
                        company_id=company_id,
                        name=str(row["name"]),
                        phone=row["phone"] if isinstance(row["phone"], str) else None,
                        address=str(row["address"]),
                        category=str(row["category"]),
                        city=str(row["city"]),
                        platform=str(row["platform"]),
                        source="demo",
                        source_label="Демонстрация",
                    )
                )
            job: dict[str, Any] = {
                "id": job_id,
                "status": "running" if pending else "completed",
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
            status = str(job["status"])
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
            found = list(self._require(job_id)["found"])
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
            if city and str(row.get("city") or "").casefold() != city.strip().casefold():
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
                    continue
                job["found"].append(pending.pop(0))
                self.collected.append(job["found"][-1])
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
