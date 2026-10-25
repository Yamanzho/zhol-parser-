"""Local read-only admin API. Disabled until ZHOL_ADMIN_TOKEN is set."""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import event
from sqlalchemy.orm import Session

from zhol.agents.scout.setup import CATALOG_NAME, collection_setup
from zhol.api.admin_data import (
    audit_detail,
    batch_detail,
    company_detail,
    content_page,
    list_audits,
    list_batches,
    list_companies,
    list_listings,
    list_orders,
    list_reports,
    list_tasks,
    order_detail,
    report_detail,
    summary,
)
from zhol.api.filesafe import AdminPathError, resolve_source
from zhol.db.base import get_session_factory


def _readonly_begin(session: Session, _transaction: object, connection: object) -> None:
    if session.info.get("zhol_admin_readonly"):
        if session.info.get("zhol_repeatable_read"):
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")  # type: ignore[attr-defined]
        else:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")  # type: ignore[attr-defined]


if not event.contains(Session, "after_begin", _readonly_begin):
    event.listen(Session, "after_begin", _readonly_begin)


def accept_admin(authorization: str | None, request: Request | None = None) -> None:
    expected = os.environ.get("ZHOL_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="admin api is disabled")
    if authorization:
        if len(authorization) > 500 or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="authentication required", headers={"WWW-Authenticate": "Bearer"})
        token = authorization.removeprefix("Bearer ").strip()
        if token and len(token) <= 400 and _matches(token, expected):
            return
        raise HTTPException(status_code=401, detail="authentication required", headers={"WWW-Authenticate": "Bearer"})
    from zhol.api.local_access import cookie_authenticates

    if cookie_authenticates(request):
        return
    raise HTTPException(status_code=401, detail="authentication required", headers={"WWW-Authenticate": "Bearer"})


def require_admin(request: Request, authorization: str | None = Header(default=None)) -> None:
    accept_admin(authorization, request)


def _matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(supplied.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def get_admin_session() -> Iterator[Session]:
    session = get_session_factory()()
    session.info["zhol_admin_readonly"] = True
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def page_args(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> tuple[int, int]:
    return limit, offset


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/summary")
def get_summary(session: Session = Depends(get_admin_session)) -> dict:
    return summary(session)


@router.get("/companies")
def get_companies(
    q: str = Query(default="", max_length=200),
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_companies(session, q=q, limit=limit, offset=offset)


@router.get("/companies/{company_id}")
def get_company(company_id: int, session: Session = Depends(get_admin_session)) -> dict:
    body = company_detail(session, company_id)
    if body is None:
        raise HTTPException(status_code=404, detail="company not found")
    return body


@router.get("/audits")
def get_audits(
    company_id: int | None = None,
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_audits(session, company_id=company_id, limit=limit, offset=offset)


@router.get("/audits/{audit_id}")
def get_audit(audit_id: int, session: Session = Depends(get_admin_session)) -> dict:
    body = audit_detail(session, audit_id)
    if body is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return body


@router.get("/scout/setup")
def get_scout_setup() -> dict:
    try:
        catalog = resolve_source(CATALOG_NAME)
    except AdminPathError:
        catalog = None
    return collection_setup(catalog)


@router.get("/scout/platforms/{platform}")
def get_platform_rows(
    platform: str,
    q: str = Query(default="", max_length=200),
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    from zhol.agents.scout.platform_rows import list_platform_rows

    if platform not in {"2gis", "google"}:
        raise HTTPException(status_code=404, detail="platform not found")
    limit, offset = paging
    return list_platform_rows(session, platform, q=q, limit=limit, offset=offset)


@router.get("/scout/batches")
def get_batches(
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_batches(session, limit=limit, offset=offset)


@router.get("/scout/batches/{batch_id}")
def get_batch(
    batch_id: int,
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    body = batch_detail(session, batch_id, limit=limit, offset=offset)
    if body is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return body


@router.get("/orders")
def get_orders(
    stage: str | None = Query(default=None, max_length=80),
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_orders(session, stage=stage, limit=limit, offset=offset)


@router.get("/orders/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_admin_session)) -> dict:
    body = order_detail(session, order_id)
    if body is None:
        raise HTTPException(status_code=404, detail="order not found")
    return body


@router.get("/content")
def get_content(
    order_id: int | None = None,
    status: str | None = Query(default=None, max_length=40),
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    body = content_page(session, order_id=order_id, status=status, limit=limit, offset=offset)
    if body.get("missing") == "order":
        raise HTTPException(status_code=404, detail="order not found")
    return body


@router.get("/listings")
def get_listings(
    order_id: int | None = None,
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_listings(session, order_id=order_id, limit=limit, offset=offset)


@router.get("/tasks")
def get_tasks(
    status: str | None = Query(default=None, max_length=40),
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_tasks(session, status=status, limit=limit, offset=offset)


@router.get("/reports")
def get_reports(
    paging: tuple[int, int] = Depends(page_args),
    session: Session = Depends(get_admin_session),
) -> dict:
    limit, offset = paging
    return list_reports(session, limit=limit, offset=offset)


@router.get("/reports/{report_id}")
def get_report(report_id: int, session: Session = Depends(get_admin_session)) -> dict:
    body = report_detail(session, report_id)
    if body is None:
        raise HTTPException(status_code=404, detail="report not found")
    return body
