"""Versioned collection API. Authentication matches the local admin API."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from zhol.api.admin import accept_admin, get_admin_session
from zhol.api.admin_actions import get_write_session
from zhol.integration import live
from zhol.integration.schema import (
    COLLECTION_LIMIT_DEFAULT,
    COLLECTION_LIMIT_MAX,
    COLLECTION_LIMIT_MIN,
    LIVE_EXPORT_NOTES,
    IntegrationError,
    error_body,
    export_bytes,
    iter_capped,
    normalize_idempotency_key,
    parse_page,
)

router = APIRouter(prefix="/api/v1", tags=["integration"])

_MESSAGES = {
    "authentication required": "Нужна аутентификация. Передайте токен Bearer.",
    "admin api is disabled": "API выключен: не задан токен администратора.",
    "missing admin request header": "Для запроса из браузера нужен заголовок X-Zhol-Request.",
}


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["2gis", "google", "yandex"]
    city: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=COLLECTION_LIMIT_DEFAULT, ge=COLLECTION_LIMIT_MIN, le=COLLECTION_LIMIT_MAX)
    idempotency_key: str | None = Field(default=None, max_length=80)


def integration_path(path: str) -> bool:
    return path == "/api/v1/meta" or path.startswith("/api/v1/jobs") or path.startswith("/api/v1/companies") or path == "/api/v1/export"


def register(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(IntegrationError, _integration_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


async def _integration_error(_request: Request, exc: Exception) -> JSONResponse:
    error = exc if isinstance(exc, IntegrationError) else IntegrationError(500, "rejected", "Запрос не выполнен.")
    return JSONResponse(error_body(error.code, error.message), status_code=error.status)


async def _http_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, HTTPException) or not integration_path(request.url.path):
        return await http_exception_handler(request, exc)  # type: ignore[arg-type]
    message = exc.detail if isinstance(exc.detail, str) else "Запрос отклонён."
    message = _MESSAGES.get(message, message)
    code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 503: "unavailable"}.get(exc.status_code, "rejected")
    return JSONResponse(error_body(code, message), status_code=exc.status_code, headers=exc.headers)


async def _validation_error(request: Request, exc: Exception) -> Response:
    if not integration_path(request.url.path) or not isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)  # type: ignore[arg-type]
    return JSONResponse(error_body("validation_error", "Проверьте площадку, город, запрос и лимит."), status_code=422)


def require_read(request: Request, authorization: str | None = Header(default=None)) -> None:
    accept_admin(authorization, request)


def require_write(
    request: Request,
    authorization: str | None = Header(default=None),
    x_zhol_request: str | None = Header(default=None),
) -> None:
    bearer = bool(authorization and authorization.startswith("Bearer ") and os.environ.get("ZHOL_ADMIN_TOKEN", "").strip())
    accept_admin(authorization, request)
    if not bearer and x_zhol_request != "admin":
        raise HTTPException(status_code=403, detail="missing admin request header")


def _read_session() -> Iterator[Session]:
    yield from get_admin_session()


def _write_session() -> Iterator[Session]:
    yield from get_write_session()


@router.get("/meta")
def get_meta() -> dict[str, Any]:
    return live.meta()


@router.post("/jobs", status_code=201)
def post_job(
    body: JobCreate,
    _auth: None = Depends(require_write),
    session: Session = Depends(_write_session),
) -> JSONResponse:
    try:
        key = normalize_idempotency_key(body.idempotency_key)
    except IntegrationError as exc:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status)
    spec = body.model_dump()
    spec["idempotency_key"] = key
    try:
        document, duplicate = live.create_job(session, spec)
    except IntegrationError as exc:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status)
    return JSONResponse(document, status_code=200 if duplicate else 201)


@router.get("/jobs")
def get_jobs(
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> dict[str, Any]:
    page_limit, page_offset = parse_page(limit, offset)
    return live.list_jobs(session, page_limit, page_offset)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> dict[str, Any]:
    return live.job_view(session, job_id)


@router.post("/jobs/{job_id}/pause")
def post_pause(
    job_id: int,
    _auth: None = Depends(require_write),
    session: Session = Depends(_write_session),
) -> dict[str, Any]:
    return live.control_job(session, job_id, "pause")


@router.post("/jobs/{job_id}/resume")
def post_resume(
    job_id: int,
    _auth: None = Depends(require_write),
    session: Session = Depends(_write_session),
) -> dict[str, Any]:
    return live.control_job(session, job_id, "resume")


@router.post("/jobs/{job_id}/stop")
def post_stop(
    job_id: int,
    _auth: None = Depends(require_write),
    session: Session = Depends(_write_session),
) -> dict[str, Any]:
    return live.control_job(session, job_id, "stop")


@router.get("/jobs/{job_id}/results")
def get_results(
    job_id: int,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> dict[str, Any]:
    page_limit, page_offset = parse_page(limit, offset)
    return live.job_results(session, job_id, page_limit, page_offset)


@router.get("/jobs/{job_id}/export")
def get_export(
    job_id: int,
    format: Literal["xlsx", "csv", "json"] = Query(default="xlsx"),
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> Response:
    payload, media, filename = export_bytes(format, iter_capped(live.iter_job_records(session, job_id)), notes=LIVE_EXPORT_NOTES)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(payload, media_type=media, headers=headers)


@router.get("/companies")
def get_companies(
    q: str = Query(default=""),
    platform: str | None = Query(default=None),
    city: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> dict[str, Any]:
    page_limit, page_offset = parse_page(limit, offset)
    return live.list_companies(session, q=q, platform=platform, city=city, limit=page_limit, offset=page_offset)


@router.get("/companies/{company_id}")
def get_company(
    company_id: int,
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> dict[str, Any]:
    return live.company_public(session, company_id)


@router.get("/export")
def get_company_export(
    format: Literal["xlsx", "csv", "json"] = Query(default="xlsx"),
    q: str = Query(default=""),
    platform: str | None = Query(default=None),
    city: str | None = Query(default=None),
    _auth: None = Depends(require_read),
    session: Session = Depends(_read_session),
) -> Response:
    rows = live.iter_companies(session, q=q, platform=platform, city=city)
    payload, media, filename = export_bytes(format, iter_capped(rows), notes=LIVE_EXPORT_NOTES)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(payload, media_type=media, headers=headers)
