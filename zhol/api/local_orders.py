"""Local order intake routes. These are not the Bek API in architecture §10."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from zhol.api.admin import require_admin
from zhol.db.base import get_session_factory
from zhol.orders.intake import (
    DuplicateOrderError,
    OrderIntakeError,
    check_readiness,
    create_test_order,
    order_status,
)

router = APIRouter(
    prefix="/api/v1/local",
    tags=["local-orders"],
    dependencies=[Depends(require_admin)],
)


def get_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.post("/orders")
def post_order(spec: dict[str, Any] = Body(...), session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        order = create_test_order(session, spec)
    except DuplicateOrderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrderIntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report = check_readiness(session, order.id)
    if report is None:
        raise HTTPException(status_code=500, detail="order was not stored")
    return {"order_id": order.id, "stage": order.stage, "readiness": report.as_dict()}


@router.get("/orders/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    view = order_status(session, order_id)
    if view is None:
        raise HTTPException(status_code=404, detail="order not found")
    return view.as_dict()


@router.get("/orders/{order_id}/readiness")
def get_readiness(order_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    report = check_readiness(session, order_id)
    if report is None:
        raise HTTPException(status_code=404, detail="order not found")
    return report.as_dict()
