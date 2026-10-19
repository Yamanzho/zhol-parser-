from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import Job
from zhol.integrations import telegram
from zhol.jobs import enqueue, recover
from zhol.timeutil import ALMATY, now

log = logging.getLogger(__name__)

TICK_LOCK = 0x5A484F4C
DIGEST_AT = time(9, 0)

Slot = Callable[[datetime], str | None]


def _every_15_minutes(local: datetime) -> str:
    return local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0).isoformat()


def _daily_digest(local: datetime) -> str | None:
    return local.date().isoformat() if local.time() >= DIGEST_AT else None


def _mondays(local: datetime) -> str | None:
    return local.date().isoformat() if local.weekday() == 0 else None


def _first_of_month(local: datetime) -> str | None:
    return local.strftime("%Y-%m") if local.day == 1 else None


# (job type, queue, slot for the current Almaty time or None when not due)
PERIODIC: tuple[tuple[str, str, Slot], ...] = (
    ("manager.sla_scan", "main", _every_15_minutes),
    ("manager.digest", "main", _daily_digest),
    ("review.weekly_audit", "main", _mondays),
    ("review.monthly_quality", "main", _first_of_month),
)


@dataclass
class TickResult:
    at: datetime
    dead: list[int] = field(default_factory=list)
    enqueued: list[tuple[str, str, int]] = field(default_factory=list)
    already: list[tuple[str, str]] = field(default_factory=list)


def tick(factory: sessionmaker[Session], *, notifier: Callable[[str], None] = telegram.notify) -> TickResult:
    result = TickResult(at=now())
    result.dead = recover(factory, notifier=notifier)
    local = result.at.astimezone(ALMATY)
    with factory() as session, session.begin():
        session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": TICK_LOCK})
        for job_type, queue, slot_of in PERIODIC:
            slot = slot_of(local)
            if slot is None:
                continue
            exists = session.scalar(
                select(Job.id).where(Job.type == job_type, Job.payload["slot"].astext == slot).limit(1)
            )
            if exists is not None:
                result.already.append((job_type, slot))
                continue
            job_id = enqueue(session, job_type, {"slot": slot}, queue=queue, dedup_key=f"{job_type}:{slot}")
            result.enqueued.append((job_type, slot, job_id))
    log.info("scheduler tick at %s: enqueued %d, dead %d", result.at.isoformat(), len(result.enqueued), len(result.dead))
    return result
