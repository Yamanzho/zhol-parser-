"""Live collection jobs. Writes go through the existing scout queue only."""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from zhol.agents.scout.batch import progress_counts
from zhol.agents.scout.browser_2gis import BrowserCollectionRefused, build_browser_plans
from zhol.agents.scout.browser_google import build_browser_plans as build_google_plans
from zhol.agents.scout.browser_yandex import build_browser_plans as build_yandex_plans
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.live_2gis import CatalogError
from zhol.agents.scout.setup import SetupError
from zhol.api.admin_actions import _enqueue_browser, _enqueue_maps
from zhol.api.admin_data import _platform_ids_for, _snapshots_for
from zhol.api.admin_jobs import JOB_TYPE, AdminJobError, resume_batch, signal_batch
from zhol.api.company_view import list_item
from zhol.browser.collector import panel_browser_modes
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, CompanyPlatformId, Job
from zhol.integration.schema import (
    COLLECTION_LIMIT_DEFAULT,
    COLLECTION_LIMIT_MAX,
    COLLECTION_LIMIT_MIN,
    LIVE_EXPORT_NOTES,
    PLATFORM_LABEL,
    IntegrationError,
    blank,
    collection_limit,
    company_record,
    job_document,
    page_body,
)
from zhol.jobs.queue import JobDeferred

_MODE_PLATFORM = {"browser_2gis": "2gis", "browser_google": "google", "browser_yandex": "yandex"}


def _plans(platform: str, *, city: str, query: str, limit: int) -> list[dict[str, Any]]:
    if platform == "2gis":
        return build_browser_plans(city=city, categories=[], queries=[query], platforms=["2gis"], limit=limit)
    if platform == "google":
        return build_google_plans(city=city, categories=[], queries=[query], platforms=["google"], limit=limit)
    return build_yandex_plans(city=city, categories=[], queries=[query], platforms=["yandex"], limit=limit)


def idempotency_dedup(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"admin.scout_batch:idem:{digest}"


def meta() -> dict[str, Any]:
    platforms = []
    for mode in panel_browser_modes():
        platform = _MODE_PLATFORM.get(str(mode.get("id")))
        if platform is None:
            continue
        available = mode.get("available") is True and mode.get("collection_state", "available") == "available"
        note = "" if available else str(mode.get("note") or "Сбор с этой площадки сейчас закрыт.")
        cities = [str(name) for name in mode.get("cities") or []]
        platforms.append(
            {
                "id": platform,
                "label": PLATFORM_LABEL[platform],
                "available": available,
                "note": note,
                "cities": cities,
            }
        )
    return {
        "service": "zhol",
        "api_version": "v1",
        "mode": "live",
        "demo": False,
        "label": "Рабочий режим",
        "bind": "127.0.0.1",
        "platforms": platforms,
        "limit": {"min": COLLECTION_LIMIT_MIN, "max": COLLECTION_LIMIT_MAX, "default": COLLECTION_LIMIT_DEFAULT},
        "export_formats": ["xlsx", "csv", "json"],
        "export_notes": list(LIVE_EXPORT_NOTES),
        "docs": "/docs",
    }


def create_job(session: Session, spec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    platform = str(spec["platform"])
    city = " ".join(str(spec["city"]).split())
    query = " ".join(str(spec["query"]).split())
    limit = collection_limit(int(spec["limit"]))
    key = spec.get("idempotency_key")
    if not city or len(city) > 80:
        raise IntegrationError(422, "validation_error", "Укажите город не длиннее 80 символов.")
    if not query or len(query) > 120:
        raise IntegrationError(422, "validation_error", "Укажите запрос не длиннее 120 символов.")
    if platform not in {"2gis", "google", "yandex"}:
        raise IntegrationError(422, "validation_error", "Площадка: 2gis, google или yandex.")
    if key:
        existing = session.scalar(select(Job).where(Job.dedup_key == idempotency_dedup(str(key))))
        if existing is not None:
            if not _same_request(existing, platform, city, query, limit):
                raise IntegrationError(409, "conflict", "Ключ идемпотентности уже использован для другого задания.")
            body = job_view(session, existing.id)
            body["duplicate"] = True
            return body, True
    try:
        plans = _plans(platform, city=city, query=query, limit=limit)
    except BrowserCollectionRefused as exc:
        raise IntegrationError(403, "forbidden", str(exc)) from exc
    except (BatchError, SetupError, CatalogError, ValueError) as exc:
        raise IntegrationError(400, "rejected", str(exc)) from exc
    if len(plans) != 1:
        raise IntegrationError(400, "rejected", "Один запрос должен создавать одно задание.")
    plan = plans[0]
    try:
        if platform == "2gis":
            stored = _enqueue_browser(session, plan, idempotency_key=key)
        else:
            stored = _enqueue_maps(session, plan, idempotency_key=key)
    except AdminJobError as exc:
        raise IntegrationError(400, "rejected", str(exc)) from exc
    body = job_view(session, int(stored["id"]))
    body["duplicate"] = bool(stored.get("duplicate"))
    return body, bool(stored.get("duplicate"))


def list_jobs(session: Session, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(Job).where(Job.type == JOB_TYPE)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.scalars(stmt.order_by(Job.id.desc()).limit(limit).offset(offset)).all()
    return page_body([job_view(session, row.id) for row in rows], total, limit, offset)


def job_view(session: Session, job_id: int) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None or job.type != JOB_TYPE:
        raise IntegrationError(404, "not_found", "Задание не найдено.")
    payload = job.payload if isinstance(job.payload, dict) else {}
    batch = _batch_for(session, payload)
    status, message, error, collected, progress = _status(session, job, payload, batch)
    platform = _platform_of(payload)
    return job_document(
        {
            "id": job.id,
            "status": status,
            "platform": platform,
            "city": payload.get("city"),
            "query": payload.get("category"),
            "limit": payload.get("limit") if payload.get("limit") is not None else (None if batch is None else batch.item_limit),
            "collected": collected,
            "progress": progress,
            "message": message,
            "error": error or (job.last_error if status == "failed" else None),
            "duplicate": False,
            "batch_id": None if batch is None else batch.id,
        }
    )


def control_job(session: Session, job_id: int, action: str) -> dict[str, Any]:
    job = session.execute(select(Job).where(Job.id == job_id, Job.type == JOB_TYPE).with_for_update()).scalar_one_or_none()
    if job is None:
        raise IntegrationError(404, "not_found", "Задание не найдено.")
    payload = dict(job.payload or {})
    batch = _batch_for(session, payload)
    if action == "pause":
        _pause(session, job, payload, batch)
    elif action == "stop":
        _stop(session, job, payload, batch)
    elif action == "resume":
        _resume(session, job, payload, batch)
    else:
        raise IntegrationError(400, "rejected", "Неизвестное действие.")
    return job_view(session, job.id)


def job_results(session: Session, job_id: int, limit: int, offset: int) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None or job.type != JOB_TYPE:
        raise IntegrationError(404, "not_found", "Задание не найдено.")
    payload = job.payload if isinstance(job.payload, dict) else {}
    batch = _batch_for(session, payload)
    if batch is None:
        return page_body([], 0, limit, offset)
    stmt = select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch.id)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.scalars(stmt.order_by(CollectionBatchItem.id).limit(limit).offset(offset)).all()
    return page_body(_records_for_items(session, rows, _platform_of(payload)), total, limit, offset)


def iter_job_records(session: Session, job_id: int) -> Iterator[dict[str, Any]]:
    _stable_read(session)
    job = session.get(Job, job_id)
    if job is None or job.type != JOB_TYPE:
        raise IntegrationError(404, "not_found", "Задание не найдено.")
    payload = job.payload if isinstance(job.payload, dict) else {}
    batch = _batch_for(session, payload)
    if batch is None:
        return
    platform = _platform_of(payload)
    batch_id = batch.id
    last_id = 0
    while True:
        rows = session.scalars(
            select(CollectionBatchItem)
            .where(CollectionBatchItem.batch_id == batch_id, CollectionBatchItem.id > last_id)
            .order_by(CollectionBatchItem.id)
            .limit(200)
        ).all()
        if not rows:
            return
        last_id = rows[-1].id
        yield from _records_for_items(session, rows, platform)
        session.expunge_all()


def _company_filters(q: str, platform: str | None, city: str | None) -> list:
    filters = []
    term = q.strip()
    if term:
        like = _like(term)
        filters.append(
            or_(
                Company.name.ilike(like, escape="\\"),
                Company.city.ilike(like, escape="\\"),
                Company.niche.ilike(like, escape="\\"),
                Company.whatsapp.ilike(like, escape="\\"),
            )
        )
    if city and city.strip():
        filters.append(func.lower(Company.city) == city.strip().lower())
    if platform:
        by_identity = select(CompanyPlatformId.company_id).where(CompanyPlatformId.platform == platform)
        by_snapshot = select(CardSnapshot.company_id).where(CardSnapshot.platform == platform)
        filters.append(or_(Company.id.in_(by_identity), Company.id.in_(by_snapshot)))
    return filters


def list_companies(
    session: Session,
    *,
    q: str,
    platform: str | None,
    city: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    if platform is not None and platform not in PLATFORM_LABEL:
        raise IntegrationError(422, "validation_error", "Фильтр площадки: 2gis, google или yandex.")
    stmt = select(Company)
    for item in _company_filters(q, platform, city):
        stmt = stmt.where(item)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.scalars(stmt.order_by(Company.id.desc()).limit(limit).offset(offset)).all()
    return page_body(_records_for_companies(session, rows, platform), total, limit, offset)


def iter_companies(session: Session, *, q: str, platform: str | None, city: str | None) -> Iterator[dict[str, Any]]:
    if platform is not None and platform not in PLATFORM_LABEL:
        raise IntegrationError(422, "validation_error", "Фильтр площадки: 2gis, google или yandex.")
    _stable_read(session)
    last_id: int | None = None
    while True:
        stmt = select(Company).order_by(Company.id.desc()).limit(200)
        for item in _company_filters(q, platform, city):
            stmt = stmt.where(item)
        if last_id is not None:
            stmt = stmt.where(Company.id < last_id)
        rows = list(session.scalars(stmt).all())
        if not rows:
            return
        last_id = rows[-1].id
        yield from _records_for_companies(session, rows, platform)
        session.expunge_all()


def _stable_read(session: Session) -> None:
    """One snapshot for the whole export, so pages neither skip nor repeat rows."""
    session.info["zhol_repeatable_read"] = True
    connection = session.connection()
    if session.info.get("zhol_admin_readonly") or session.info.get("zhol_isolation_set"):
        return
    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    session.info["zhol_isolation_set"] = True


def company_public(session: Session, company_id: int) -> dict[str, Any]:
    company = session.get(Company, company_id)
    if company is None:
        raise IntegrationError(404, "not_found", "Компания не найдена.")
    record = _records_for_companies(session, [company], None)[0]
    facts = [
        {"label": "Телефон", "text": record["phone"] or "Нет данных"},
        {"label": "Адрес", "text": record["address"] or "Нет данных"},
        {"label": "Категория", "text": record["category"] or "Нет данных"},
        {"label": "Город", "text": record["city"] or "Нет данных"},
        {"label": "Площадка", "text": record["platform_label"]},
        {"label": "Источник", "text": record["source_label"]},
    ]
    return {"company": record, "facts": facts, "note": record["review_note"]}


def _pause(session: Session, job: Job, payload: dict[str, Any], batch: CollectionBatch | None) -> None:
    if batch is not None:
        try:
            signal_batch(session, batch.id, "pause")
        except AdminJobError as exc:
            raise _signal_error(exc) from exc
        return
    if job.status not in {"queued", "failed", "running"}:
        raise IntegrationError(409, "conflict", "Это задание уже нельзя поставить на паузу.")
    _remember(job, payload, "pause")


def _stop(session: Session, job: Job, payload: dict[str, Any], batch: CollectionBatch | None) -> None:
    if batch is not None:
        try:
            signal_batch(session, batch.id, "stop")
        except AdminJobError as exc:
            raise _signal_error(exc) from exc
        return
    if job.status in {"queued", "failed"}:
        _remember(job, payload, "stop")
        job.status = "done"
        from zhol.jobs.queue import _release_dedup

        _release_dedup(job)
        return
    if job.status == "running":
        _remember(job, payload, "stop")
        return
    raise IntegrationError(409, "conflict", "Это задание уже нельзя остановить.")


def _resume(session: Session, job: Job, payload: dict[str, Any], batch: CollectionBatch | None) -> None:
    if batch is not None:
        try:
            resume_batch(session, batch.id)
        except AdminJobError as exc:
            raise _signal_error(exc) from exc
        return
    if payload.get("admin_signal") != "pause":
        raise IntegrationError(409, "conflict", "Продолжить можно только задание на паузе.")
    _remember(job, payload, None)


def _remember(job: Job, payload: dict[str, Any], signal: str | None) -> None:
    if signal is None:
        payload.pop("admin_signal", None)
    else:
        payload["admin_signal"] = signal
    job.payload = payload
    flag_modified(job, "payload")


def _signal_error(exc: AdminJobError) -> IntegrationError:
    text = str(exc)
    status = 404 if "not found" in text else 409
    return IntegrationError(status, "not_found" if status == 404 else "conflict", text)


def _same_request(job: Job, platform: str, city: str, query: str, limit: int) -> bool:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return (
        _platform_of(payload) == platform
        and str(payload.get("city") or "").strip().casefold() == city.casefold()
        and str(payload.get("category") or "").strip().casefold() == query.casefold()
        and int(payload.get("limit") or 0) == limit
    )


def _batch_for(session: Session, payload: dict[str, Any]) -> CollectionBatch | None:
    raw = payload.get("batch_id") or payload.get("resume")
    if not isinstance(raw, int):
        return None
    return session.get(CollectionBatch, raw)


def _platform_of(payload: dict[str, Any]) -> str | None:
    platforms = payload.get("platforms")
    if isinstance(platforms, list) and platforms:
        name = str(platforms[0])
        if name in PLATFORM_LABEL:
            return name
    mode = str(payload.get("source_mode") or "")
    if mode == "browser_2gis":
        return "2gis"
    if mode == "browser_google":
        return "google"
    if mode == "browser_yandex":
        return "yandex"
    return None


def _status(
    session: Session,
    job: Job,
    payload: dict[str, Any],
    batch: CollectionBatch | None,
) -> tuple[str, str | None, str | None, int, float | None]:
    signal = payload.get("admin_signal")
    if batch is None:
        if job.status == "done" and signal == "stop":
            return "stopped", "Остановка запрошена до начала сбора.", None, 0, None
        if signal == "stop" and job.status == "running":
            return "running", "Остановка запрошена.", None, 0, None
        if signal == "pause" and job.status in {"queued", "failed", "running"}:
            return "paused", "Пауза до начала сбора.", None, 0, None
        if job.status in {"queued", "failed"}:
            return "queued", "Задание в очереди.", None, 0, None
        if job.status == "running":
            return "running", "Сбор запускается.", None, 0, None
        if job.status == "dead":
            return "failed", None, job.last_error or "Сбор завершился ошибкой.", 0, None
        if job.status == "done":
            return "completed", "Задание завершено.", None, 0, 1.0
        return "failed", None, job.last_error, 0, None
    counts = progress_counts(session, batch.id)
    collected = int(counts.get("completed") or 0)
    target = batch.item_limit or payload.get("limit") or counts.get("total") or 0
    progress = None if not target else round(min(collected / int(target), 1), 4)
    stored = batch.progress if isinstance(batch.progress, dict) else {}
    error = stored.get("error") if isinstance(stored.get("error"), str) else None
    status = batch.status
    if status == "completed":
        return "completed", "Сбор завершён.", error, collected, 1.0
    if status == "stopped":
        return "stopped", "Сбор остановлен. Уже записанные компании сохранены.", error, collected, progress
    if status == "paused":
        return "paused", "Сбор на паузе.", error, collected, progress
    if status == "interrupted":
        return "failed", None, error or "Сбор прерван.", collected, progress
    if status == "awaiting_manual_verification":
        return "awaiting_manual_verification", "Сбор ждёт проверку в браузере.", error, collected, progress
    if status == "running":
        return "running", "Сбор выполняется.", error, collected, progress
    return status, None, error, collected, progress


def _records_for_items(session: Session, rows: list[CollectionBatchItem], platform: str | None) -> list[dict[str, Any]]:
    company_ids = [row.company_id for row in rows if row.company_id is not None]
    companies = {}
    if company_ids:
        companies = {company.id: company for company in session.scalars(select(Company).where(Company.id.in_(company_ids))).all()}
    found = _records_for_companies(session, list(companies.values()), platform)
    by_id = {item["id"]: item for item in found}
    ordered = []
    for row in rows:
        if row.company_id is not None and row.company_id in by_id:
            ordered.append(by_id[row.company_id])
            continue
        detail = row.detail if isinstance(row.detail, dict) else {}
        name = blank(detail.get("name")) or row.id_2gis
        ordered.append(
            company_record(
                company_id=row.company_id or row.id,
                name=str(name),
                phone=blank(detail.get("phone")),
                address=blank(detail.get("address")),
                category=blank(detail.get("category")),
                city=blank(detail.get("city")),
                platform=platform or "2gis",
                source=blank(detail.get("source")) or "collection",
            )
        )
    return ordered


def _records_for_companies(session: Session, companies: list[Company], platform: str | None) -> list[dict[str, Any]]:
    if not companies:
        return []
    ids = [company.id for company in companies]
    grouped = _snapshots_for(session, ids)
    platforms = _platform_ids_for(session, ids)
    records = []
    for company in companies:
        item = list_item(company, grouped.get(company.id, []), platforms.get(company.id, []))
        source_platform = str(item.get("source_platform") or platform or "2gis")
        if source_platform not in PLATFORM_LABEL:
            source_platform = platform or "2gis"
        records.append(
            company_record(
                company_id=company.id,
                name=company.name,
                phone=blank(item.get("phone")),
                address=blank(item.get("address")),
                category=blank(item.get("category") or company.niche),
                city=blank(item.get("city") or company.city),
                platform=source_platform,
                source=blank(item.get("source")) or company.source,
                review_needed=bool(item.get("review_needed")),
                review_note=blank(item.get("review_note")),
            )
        )
    return records


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def raise_if_paused_before_start(signal: str | None) -> None:
    """Used by the existing worker. A pause before the batch exists does not open a browser."""
    if signal == "pause":
        raise JobDeferred(15)
