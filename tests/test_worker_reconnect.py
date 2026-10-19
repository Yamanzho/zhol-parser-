"""A worker keeps polling after PostgreSQL drops the connection.

The fixture database is disposable. This test does not use the application database.
"""

from __future__ import annotations

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from zhol.db.base import POOL_RECYCLE_SECONDS, get_engine
from zhol.db.models import Job
from zhol.jobs.queue import enqueue
from zhol.jobs.queue import recover as real_recover
from zhol.worker import serve


def _closed(statement: str = "SELECT 1") -> OperationalError:
    return OperationalError(statement, {}, Exception("server closed the connection unexpectedly"))


def test_pool_pre_ping_replaces_a_closed_connection(postgres_url: str) -> None:
    engine = get_engine(postgres_url)
    assert engine.pool._pre_ping is True
    assert engine.pool._recycle == POOL_RECYCLE_SECONDS
    with engine.connect() as conn:
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    raw = psycopg.connect(postgres_url.replace("postgresql+psycopg://", "postgresql://", 1))
    raw.autocommit = True
    raw.execute("SELECT pg_terminate_backend(%s)", (pid,))
    raw.close()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    engine.dispose()


def test_worker_claims_the_queued_job_after_a_disconnect(migrated_engine, postgres_url: str, monkeypatch) -> None:
    engine = get_engine(postgres_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session, session.begin():
        job_id = enqueue(session, "snapshot.verify", {"snapshot_ids": []}, queue="main")
    claimed: list[int] = []
    state = {"failed": False}

    def recover_once(bound, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise _closed("SELECT jobs")
        return real_recover(bound, **kwargs)

    monkeypatch.setattr("zhol.worker.recover", recover_once)
    monkeypatch.setattr(
        "zhol.worker.handlers",
        lambda _factory: {
            "snapshot.verify": lambda _payload: claimed.append(1),
            "admin.scout_batch": lambda _payload: None,
        },
    )
    cycles = serve(
        factory,
        "main",
        notifier=lambda _message: None,
        poll_seconds=0,
        max_cycles=1,
        reconnect_sleep=lambda _seconds: None,
    )
    assert cycles == 1
    assert claimed == [1]
    with factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "done"
    engine.dispose()


def test_worker_stops_after_the_reconnect_bound(migrated_engine, monkeypatch) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)

    def always_down(*_args, **_kwargs):
        raise _closed()

    monkeypatch.setattr("zhol.worker.recover", always_down)
    monkeypatch.setattr("zhol.worker.run_one", lambda *_args, **_kwargs: None)
    try:
        serve(
            factory,
            "main",
            notifier=lambda _message: None,
            poll_seconds=0,
            max_cycles=5,
            reconnect_sleep=lambda _seconds: None,
            max_db_disconnects=2,
        )
    except OperationalError as exc:
        assert "server closed the connection unexpectedly" in str(exc)
    else:
        raise AssertionError("the worker kept running after the reconnect bound")
