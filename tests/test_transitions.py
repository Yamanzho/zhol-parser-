from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import Company, Deal, EventOut, Order
from zhol.timeutil import reset_clock, set_now
from zhol.transitions import (
    SETUP_FORWARD,
    SETUP_STATES,
    SUBSCRIPTION_FORWARD,
    SUBSCRIPTION_STATES,
    ConcurrentTransition,
    IllegalTransition,
    transition,
)

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_time() -> Iterator[None]:
    set_now(NOW)
    yield
    reset_clock()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _company_deal(session: Session) -> tuple[Company, Deal]:
    suffix = uuid.uuid4().hex
    company = Company(id_2gis=f"2gis-{suffix}", name="Test", source="manual")
    session.add(company)
    session.flush()
    deal = Deal(syntony_deal_id=f"deal-{suffix}", company_id=company.id)
    session.add(deal)
    session.flush()
    return company, deal


def make_order(session: Session, *, kind: str = "setup", stage: str | None = "created") -> Order:
    company, deal = _company_deal(session)
    kwargs: dict[str, object] = {
        "deal_id": deal.id,
        "company_id": company.id,
        "kind": kind,
        "amount_kzt": 1,
    }
    if stage is not None:
        kwargs["stage"] = stage
    order = Order(**kwargs)
    session.add(order)
    session.flush()
    return order


def _event_names(session: Session, order: Order) -> list[str]:
    return list(
        session.scalars(select(EventOut.event).where(EventOut.deal_id == order.deal_id)).all()
    )


SETUP_CANCEL = {(s, "cancelled") for s in SETUP_STATES if s not in {"closed", "cancelled"}}
SETUP_HUMAN = {(s, "human") for s in SETUP_STATES}
SETUP_VALID = SETUP_FORWARD | SETUP_CANCEL | SETUP_HUMAN

SUB_CANCEL = {(s, "cancelled") for s in SUBSCRIPTION_STATES if s not in {"closed", "cancelled"}}
SUB_VALID = SUBSCRIPTION_FORWARD | SUB_CANCEL


@pytest.mark.parametrize(("current", "to"), sorted(SETUP_FORWARD, key=lambda p: (str(p[0]), p[1])))
def test_setup_forward_transitions(session: Session, current: str | None, to: str) -> None:
    if current is None:
        company, deal = _company_deal(session)
        order = Order(deal_id=deal.id, company_id=company.id, kind="setup", amount_kzt=1)
        session.add(order)
    else:
        order = make_order(session, stage=current)
    transition(order, to, session)
    session.flush()
    assert order.stage == to


@pytest.mark.parametrize(("current", "to"), sorted(SUBSCRIPTION_FORWARD, key=lambda p: (str(p[0]), p[1])))
def test_subscription_forward_transitions(session: Session, current: str | None, to: str) -> None:
    if current is None:
        company, deal = _company_deal(session)
        order = Order(deal_id=deal.id, company_id=company.id, kind="subscription", amount_kzt=1)
        session.add(order)
    else:
        order = make_order(session, kind="subscription", stage=current)
    transition(order, to, session)
    assert order.stage == to


@pytest.mark.parametrize(
    ("current", "to"),
    sorted(
        {(src, dst) for src in (None, *SETUP_STATES) for dst in SETUP_STATES} - SETUP_VALID,
        key=lambda p: (str(p[0]), p[1]),
    ),
)
def test_illegal_setup_pairs(session: Session, current: str | None, to: str) -> None:
    if current is None:
        company, deal = _company_deal(session)
        order = Order(deal_id=deal.id, company_id=company.id, kind="setup", amount_kzt=1)
        session.add(order)
    else:
        order = make_order(session, stage=current)
    before = _current_or_missing(order)
    events_before = _event_names(session, order) if order.id is not None else []
    with pytest.raises(IllegalTransition):
        transition(order, to, session)
    assert _current_or_missing(order) == before
    if order.id is not None:
        session.flush()
        assert _event_names(session, order) == events_before
    else:
        session.expunge(order)


@pytest.mark.parametrize(
    ("current", "to"),
    sorted(
        {(src, dst) for src in (None, *SUBSCRIPTION_STATES) for dst in SUBSCRIPTION_STATES} - SUB_VALID,
        key=lambda p: (str(p[0]), p[1]),
    ),
)
def test_illegal_subscription_pairs(session: Session, current: str | None, to: str) -> None:
    if current is None:
        company, deal = _company_deal(session)
        order = Order(deal_id=deal.id, company_id=company.id, kind="subscription", amount_kzt=1)
        session.add(order)
    else:
        order = make_order(session, kind="subscription", stage=current)
    with pytest.raises(IllegalTransition):
        transition(order, to, session)
    assert _current_or_missing(order) == current
    if order.id is None:
        session.expunge(order)


def _current_or_missing(order: Order) -> str | None:
    return order.__dict__.get("stage")


@pytest.mark.parametrize("stage", SETUP_STATES)
def test_same_state_setup_rejected(session: Session, stage: str) -> None:
    order = make_order(session, stage=stage)
    with pytest.raises(IllegalTransition):
        transition(order, stage, session)
    assert order.stage == stage


@pytest.mark.parametrize("stage", SUBSCRIPTION_STATES)
def test_same_state_subscription_rejected(session: Session, stage: str) -> None:
    order = make_order(session, kind="subscription", stage=stage)
    with pytest.raises(IllegalTransition):
        transition(order, stage, session)


def test_unknown_target_rejected(session: Session) -> None:
    order = make_order(session)
    with pytest.raises(IllegalTransition):
        transition(order, "unknown", session)
    assert order.stage == "created"


def test_unknown_current_rejected(session: Session) -> None:
    order = make_order(session)
    order.stage = "mystery"
    session.flush()
    with pytest.raises(IllegalTransition):
        transition(order, "prepaid", session)
    assert order.stage == "mystery"


def test_terminal_closed_cannot_cancel(session: Session) -> None:
    order = make_order(session, stage="closed")
    with pytest.raises(IllegalTransition):
        transition(order, "cancelled", session)


def test_human_saves_previous_stage(session: Session) -> None:
    order = make_order(session, stage="intake")
    transition(order, "human", session)
    assert order.stage == "human"
    assert order.stage_before_human == "intake"


def test_human_returns_to_previous(session: Session) -> None:
    order = make_order(session, stage="intake")
    transition(order, "human", session)
    transition(order, "intake", session)
    assert order.stage == "intake"


def test_human_can_cancel(session: Session) -> None:
    order = make_order(session, stage="intake")
    transition(order, "human", session)
    transition(order, "cancelled", session)
    assert order.stage == "cancelled"


def test_human_cannot_return_to_other_stage(session: Session) -> None:
    order = make_order(session, stage="intake")
    transition(order, "human", session)
    with pytest.raises(IllegalTransition):
        transition(order, "package", session)
    assert order.stage == "human"


def test_subscription_has_no_human(session: Session) -> None:
    order = make_order(session, kind="subscription", stage="created")
    with pytest.raises(IllegalTransition):
        transition(order, "human", session)


def test_revision_from_delivered_returns_to_delivered(session: Session) -> None:
    order = make_order(session, stage="report")
    transition(order, "delivered", session)
    assert order.revision_until == NOW + timedelta(days=3)
    transition(order, "package", session)
    assert order.stage == "package"
    assert order.stage_before_revision == "delivered"
    order.stage = "report"
    session.flush()
    transition(order, "delivered", session)
    assert order.stage == "delivered"


def test_revision_from_final_invoiced_does_not_use_delivered(session: Session) -> None:
    order = make_order(session, stage="final_invoiced")
    order.revision_until = NOW + timedelta(days=3)
    session.flush()
    transition(order, "package", session)
    order.stage = "report"
    session.flush()
    with pytest.raises(IllegalTransition):
        transition(order, "delivered", session)
    transition(order, "final_invoiced", session)
    assert order.stage == "final_invoiced"


def test_revision_without_window_rejected(session: Session) -> None:
    order = make_order(session, stage="delivered")
    with pytest.raises(IllegalTransition):
        transition(order, "package", session)


def test_expired_revision_rejected(session: Session) -> None:
    order = make_order(session, stage="delivered")
    order.revision_until = NOW - timedelta(seconds=1)
    session.flush()
    with pytest.raises(IllegalTransition):
        transition(order, "package", session)


def test_intake_and_report_events(session: Session) -> None:
    order = make_order(session, stage="prepaid")
    transition(order, "intake", session)
    session.flush()
    assert _event_names(session, order) == ["intake_missing"]
    order.stage = "report"
    session.flush()
    transition(order, "delivered", session)
    session.flush()
    assert _event_names(session, order) == ["intake_missing", "report_ready"]


def test_illegal_does_not_write_events(session: Session) -> None:
    order = make_order(session, stage="created")
    with pytest.raises(IllegalTransition):
        transition(order, "closed", session)
    session.flush()
    assert _event_names(session, order) == []


def test_atomic_rollback_on_failure(session: Session) -> None:
    order = make_order(session, stage="prepaid")
    session.commit()
    try:
        transition(order, "intake", session)
        session.flush()
        raise RuntimeError("force rollback")
    except RuntimeError:
        session.rollback()
    session.refresh(order)
    assert order.stage == "prepaid"
    assert session.scalars(select(EventOut)).all() == []


def test_stale_concurrent_transition(migrated_engine: Engine) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    setup = factory()
    order = make_order(setup, stage="created")
    setup.commit()
    order_id = order.id
    setup.close()

    s1 = factory()
    s2 = factory()
    first = s1.get(Order, order_id)
    second = s2.get(Order, order_id)
    assert first is not None and second is not None
    transition(first, "prepay_invoiced", s1)
    s1.commit()
    with pytest.raises(ConcurrentTransition):
        transition(second, "prepay_invoiced", s2)
    s2.rollback()
    s3 = factory()
    assert s3.get(Order, order_id).stage == "prepay_invoiced"
    s1.close()
    s2.close()
    s3.close()


def test_unbound_order_rejected() -> None:
    order = Order(kind="setup", amount_kzt=1, stage="created")
    with pytest.raises(IllegalTransition):
        transition(order, "prepay_invoiced")


def test_non_order_rejected(session: Session) -> None:
    with pytest.raises(TypeError):
        transition(object(), "created", session)
