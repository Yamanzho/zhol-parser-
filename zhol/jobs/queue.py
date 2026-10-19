from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection, Mapping
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import HumanTask, Job
from zhol.integrations import telegram
from zhol.redact import safe_error
from zhol.timeutil import now

log = logging.getLogger(__name__)

QUEUES = ("main", "browser", "collector")
# Set only while a worker is inside a claimed job. Direct handler calls leave it unset.
worker_queue: ContextVar[str | None] = ContextVar("zhol_worker_queue", default=None)
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
MAX_ATTEMPTS = 3
# While a job is running, run_after holds its lease deadline.
LEASE = timedelta(minutes=30)
DEAD_REASON = "job_dead"

Handler = Callable[[dict[str, Any]], None]
Notifier = Callable[[str], None]


class StaleJob(RuntimeError):
    pass


class JobDeferred(Exception):
    """The collector profile is busy. The job stays queued and is not a failure."""

    def __init__(self, seconds: int = 15) -> None:
        self.seconds = seconds
        super().__init__(f"deferred for {seconds}s")


def _check_queue(queue: str) -> None:
    if queue not in QUEUES:
        raise ValueError(f"unknown queue {queue!r}")


def enqueue(
    session: Session,
    type: str,
    payload: dict[str, Any],
    *,
    queue: str = "main",
    dedup_key: str | None = None,
    run_after: datetime | None = None,
) -> int:
    _check_queue(queue)
    current = now()
    stmt = insert(Job).values(
        type=type,
        queue=queue,
        payload=payload,
        dedup_key=dedup_key,
        run_after=run_after or current,
        created_at=current,
    )
    if dedup_key is None:
        return session.execute(stmt.returning(Job.id)).scalar_one()
    insert_new = stmt.on_conflict_do_nothing(index_elements=[Job.dedup_key]).returning(Job.id)
    for _ in range(3):
        job_id = session.execute(insert_new).scalar_one_or_none()
        if job_id is not None:
            return job_id
        job_id = session.execute(select(Job.id).where(Job.dedup_key == dedup_key)).scalar_one_or_none()
        if job_id is not None:
            return job_id
    raise RuntimeError(f"could not enqueue job with dedup_key {dedup_key!r}")


def claim(
    session: Session,
    queue: str,
    *,
    lease: timedelta = LEASE,
    types: Collection[str] | None = None,
) -> Job | None:
    _check_queue(queue)
    current = now()
    filters = [Job.queue == queue, Job.status.in_(("queued", "failed")), Job.run_after <= current]
    if types is not None:
        filters.append(Job.type.in_(tuple(types)))
    job = session.execute(
        select(Job).where(*filters).order_by(Job.run_after, Job.id).limit(1).with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.run_after = current + lease
    session.flush()
    return job


def _locked_running(session: Session, job_id: int, attempt: int) -> Job:
    job = session.execute(
        select(Job).where(Job.id == job_id, Job.status == "running", Job.attempts == attempt).with_for_update()
    ).scalar_one_or_none()
    if job is None:
        raise StaleJob(f"job {job_id} attempt {attempt} is no longer running")
    return job


def heartbeat(session: Session, job_id: int, attempt: int, *, lease: timedelta = LEASE) -> datetime:
    deadline = now() + lease
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running", Job.attempts == attempt)
        .values(run_after=deadline)
    )
    if result.rowcount != 1:
        raise StaleJob(f"job {job_id} attempt {attempt} is no longer running")
    return deadline


def _keep_alive(
    factory: sessionmaker[Session],
    job_id: int,
    attempt: int,
    lease: timedelta,
    every: float,
    stop: threading.Event,
) -> None:
    while not stop.wait(every):
        try:
            with factory() as session, session.begin():
                heartbeat(session, job_id, attempt, lease=lease)
        except StaleJob:
            log.warning("job %s attempt %s lost its lease; heartbeat stopped", job_id, attempt)
            return
        except Exception as exc:
            log.warning("heartbeat for job %s failed: %s", job_id, type(exc).__name__)


def defer(session: Session, job_id: int, attempt: int, delay: timedelta) -> None:
    """Return a running job to the queue without spending a retry."""
    job = _locked_running(session, job_id, attempt)
    job.status = "queued"
    job.attempts = max(0, job.attempts - 1)
    job.run_after = now() + delay
    session.flush()


def complete(session: Session, job_id: int, attempt: int) -> None:
    job = _locked_running(session, job_id, attempt)
    job.status = "done"
    job.dedup_key = None
    session.flush()


def _record_failure(session: Session, job: Job, error: BaseException | str) -> bool:
    job.last_error = safe_error(error)
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "dead"
        job.dedup_key = None
        session.add(
            HumanTask(
                reason=DEAD_REASON,
                object_type="job",
                object_id=job.id,
                details={"type": job.type, "queue": job.queue, "attempts": job.attempts, "last_error": job.last_error},
            )
        )
        session.flush()
        return True
    job.status = "failed"
    job.run_after = now() + RETRY_DELAYS[min(job.attempts, len(RETRY_DELAYS)) - 1]
    session.flush()
    return False


def fail(session: Session, job_id: int, attempt: int, error: BaseException | str) -> bool:
    return _record_failure(session, _locked_running(session, job_id, attempt), error)


def recover_stale(session: Session) -> list[int]:
    jobs = session.execute(
        select(Job)
        .where(Job.status == "running", Job.run_after <= now())
        .order_by(Job.id)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    return [job.id for job in jobs if _record_failure(session, job, "lease expired")]


def _dead_message(session: Session, job_id: int) -> str:
    job = session.get(Job, job_id)
    return (
        f"Жол: задача {job.id} ({job.type}, очередь {job.queue}) остановлена после {job.attempts} попыток. "
        f"Создана задача человеку. Последняя ошибка: {job.last_error}"
    )


def _notify_dead(factory: sessionmaker[Session], job_ids: list[int], notifier: Notifier) -> None:
    if not job_ids:
        return
    with factory() as session:
        messages = [_dead_message(session, job_id) for job_id in job_ids]
    for job_id, message in zip(job_ids, messages):
        try:
            notifier(message)
        except Exception as exc:
            log.warning("dead-job notification failed for job %s: %s", job_id, type(exc).__name__)


def run_one(
    factory: sessionmaker[Session],
    queue: str,
    handlers: Mapping[str, Handler],
    *,
    notifier: Notifier = telegram.notify,
    lease: timedelta = LEASE,
    heartbeat_every: float | None = None,
    types: Collection[str] | None = None,
) -> int | None:
    with factory() as session, session.begin():
        job = claim(session, queue, lease=lease, types=types)
        if job is None:
            return None
        job_id, attempt, job_type, payload = job.id, job.attempts, job.type, dict(job.payload)

    stop = threading.Event()
    keeper = threading.Thread(
        target=_keep_alive,
        args=(factory, job_id, attempt, lease, heartbeat_every or lease.total_seconds() / 3, stop),
        daemon=True,
    )
    keeper.start()
    token = worker_queue.set(queue)
    try:
        handler = handlers.get(job_type)
        if handler is None:
            raise LookupError(f"no handler for job type {job_type!r}")
        handler(payload)
    except JobDeferred as exc:
        stop.set()
        keeper.join()
        try:
            with factory() as session, session.begin():
                defer(session, job_id, attempt, timedelta(seconds=exc.seconds))
        except StaleJob:
            log.warning("job %s attempt %s lost its lease before it could wait", job_id, attempt)
        return job_id
    except Exception as exc:
        stop.set()
        keeper.join()
        try:
            with factory() as session, session.begin():
                dead = fail(session, job_id, attempt, exc)
        except StaleJob:
            log.warning("job %s attempt %s lost its lease before failure was recorded", job_id, attempt)
            return job_id
        if dead:
            _notify_dead(factory, [job_id], notifier)
        return job_id
    finally:
        worker_queue.reset(token)

    stop.set()
    keeper.join()
    try:
        with factory() as session, session.begin():
            complete(session, job_id, attempt)
    except StaleJob:
        log.warning("job %s attempt %s lost its lease before completion was recorded", job_id, attempt)
    return job_id


def recover(factory: sessionmaker[Session], *, notifier: Notifier = telegram.notify) -> list[int]:
    with factory() as session, session.begin():
        dead = recover_stale(session)
    _notify_dead(factory, dead, notifier)
    return dead
