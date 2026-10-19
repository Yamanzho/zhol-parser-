from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session, object_session

from zhol.db.models import CardSnapshot, EventOut, Order
from zhol.timeutil import now

SETUP_STATES = (
    "created",
    "prepay_invoiced",
    "prepaid",
    "intake",
    "package",
    "client_review",
    "applying",
    "moderation",
    "report",
    "delivered",
    "final_invoiced",
    "closed",
    "cancelled",
    "human",
)

SUBSCRIPTION_STATES = (
    "created",
    "invoiced",
    "active",
    "paused",
    "closed",
    "cancelled",
)

SETUP_FORWARD = {
    (None, "created"),
    ("created", "prepay_invoiced"),
    ("prepay_invoiced", "prepaid"),
    ("prepaid", "intake"),
    ("intake", "package"),
    ("package", "client_review"),
    ("client_review", "package"),
    ("client_review", "applying"),
    ("applying", "moderation"),
    ("moderation", "report"),
    ("report", "delivered"),
    ("delivered", "final_invoiced"),
    ("final_invoiced", "closed"),
}

SUBSCRIPTION_FORWARD = {
    (None, "created"),
    ("created", "invoiced"),
    ("invoiced", "active"),
    ("invoiced", "paused"),
    ("paused", "active"),
    ("active", "closed"),
}

REVISION_SOURCES = frozenset({"delivered", "final_invoiced", "closed"})
EVENT_ON_TRANSITION = {
    ("prepaid", "intake"): "intake_missing",
    ("report", "delivered"): "report_ready",
}


class IllegalTransition(Exception):
    def __init__(self, current: str | None, to: str, *, reason: str = "not allowed") -> None:
        self.current = current
        self.to = to
        super().__init__(f"illegal transition {current!r} -> {to!r}: {reason}")


class ConcurrentTransition(IllegalTransition):
    def __init__(self, current: str | None, to: str) -> None:
        super().__init__(current, to, reason="stale stage")


def _current_stage(order: Order) -> str | None:
    return order.__dict__.get("stage")


def _states_for(kind: str) -> tuple[str, ...]:
    if kind == "setup":
        return SETUP_STATES
    if kind == "subscription":
        return SUBSCRIPTION_STATES
    raise IllegalTransition(None, kind, reason="unknown order kind")


def allowed(order: Order, current: str | None, to: str) -> bool:
    kind = order.kind
    if kind == "setup":
        if to == current:
            return False
        if current is not None and current not in SETUP_STATES:
            return False
        if to not in SETUP_STATES:
            return False
        if (current, to) in SETUP_FORWARD:
            if (
                current == "report"
                and to == "delivered"
                and order.stage_before_revision not in {None, "delivered"}
            ):
                return False
            return True
        if current in REVISION_SOURCES and to == "package":
            until = order.revision_until
            return until is not None and now() < until
        if current == "report" and order.stage_before_revision is not None:
            return to == order.stage_before_revision
        if to == "cancelled":
            return current not in {None, "closed", "cancelled"}
        if to == "human":
            return current is not None
        if current == "human":
            return to == order.stage_before_human or to == "cancelled"
        return False
    if kind == "subscription":
        if to == current:
            return False
        if current is not None and current not in SUBSCRIPTION_STATES:
            return False
        if to not in SUBSCRIPTION_STATES:
            return False
        if (current, to) in SUBSCRIPTION_FORWARD:
            return True
        if to == "cancelled":
            return current not in {None, "closed", "cancelled"}
        return False
    return False


def _session_for(obj: Order, session: Session | None, to: str) -> Session:
    found = session or object_session(obj)
    if found is None:
        raise IllegalTransition(_current_stage(obj), to, reason="object is not bound to a session")
    return found


SNAPSHOT_FORWARD = {
    ("collected", "verified"),
    ("collected", "rejected"),
    ("collected", "failed"),
    ("verified", "rejected"),
}


def _transition_snapshot(snapshot: CardSnapshot, to: str, session: Session | None) -> None:
    current = snapshot.status
    db = session or object_session(snapshot)
    if db is None:
        raise IllegalTransition(current, to, reason="object is not bound to a session")
    if (current, to) not in SNAPSHOT_FORWARD:
        raise IllegalTransition(current, to)
    if snapshot.id is None:
        snapshot.status = to
        db.add(snapshot)
        db.flush()
        return
    result = db.execute(
        update(CardSnapshot).where(CardSnapshot.id == snapshot.id, CardSnapshot.status == current).values(status=to)
    )
    if result.rowcount != 1:
        raise ConcurrentTransition(current, to)
    db.refresh(snapshot)


def transition(obj: object, to: str, session: Session | None = None) -> None:
    if isinstance(obj, CardSnapshot):
        _transition_snapshot(obj, to, session)
        return
    if not isinstance(obj, Order):
        raise TypeError("transition() accepts Order or CardSnapshot")
    db = _session_for(obj, session, to)
    current = _current_stage(obj)
    _states_for(obj.kind)
    if not allowed(obj, current, to):
        raise IllegalTransition(current, to)

    values: dict[str, object] = {"stage": to}
    if obj.kind == "setup" and to == "human":
        values["stage_before_human"] = current
    if obj.kind == "setup" and current in REVISION_SOURCES and to == "package":
        values["stage_before_revision"] = current
    if obj.kind == "setup" and current == "report" and to == "delivered":
        values["revision_until"] = now() + timedelta(days=3)

    event_name = EVENT_ON_TRANSITION.get((current, to))

    if obj.id is None:
        for key, value in values.items():
            setattr(obj, key, value)
        db.add(obj)
        db.flush()
        if event_name:
            _record_event(db, obj, event_name)
            db.flush()
        return

    result = db.execute(
        update(Order).where(Order.id == obj.id, Order.stage == current).values(**values)
    )
    if result.rowcount != 1:
        raise ConcurrentTransition(current, to)
    db.refresh(obj)
    if event_name:
        _record_event(db, obj, event_name)
    db.flush()


def _record_event(session: Session, order: Order, name: str) -> None:
    session.add(
        EventOut(
            deal_id=order.deal_id,
            event=name,
            payload={"order_id": order.id},
        )
    )
