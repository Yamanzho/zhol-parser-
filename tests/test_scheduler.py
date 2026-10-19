from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.cli import main
from zhol.db.models import HumanTask, Job
from zhol.jobs import LEASE, claim, complete, enqueue
from zhol.scheduler import tick
from zhol.timeutil import ALMATY, reset_clock, set_now

MONDAY_9_LOCAL = datetime(2026, 11, 2, 9, 0, tzinfo=ALMATY)


class Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture(autouse=True)
def _clock():
    yield
    reset_clock()


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _jobs(factory) -> list[tuple[str, str]]:
    with factory() as s:
        return sorted((j.type, j.payload["slot"]) for j in s.scalars(select(Job)))


def test_monday_nine_enqueues_due_jobs(factory) -> None:
    set_now(MONDAY_9_LOCAL)
    result = tick(factory, notifier=Notifier())
    assert sorted((t, slot) for t, slot, _ in result.enqueued) == [
        ("manager.digest", "2026-11-02"),
        ("manager.sla_scan", "2026-11-02T09:00:00+05:00"),
        ("review.weekly_audit", "2026-11-02"),
    ]


def test_tick_is_idempotent_even_after_jobs_finish(factory) -> None:
    set_now(MONDAY_9_LOCAL)
    tick(factory, notifier=Notifier())
    with factory() as s, s.begin():
        while (job := claim(s, "main")) is not None:
            complete(s, job.id, job.attempts)
    set_now(MONDAY_9_LOCAL + timedelta(minutes=14))
    again = tick(factory, notifier=Notifier())
    assert again.enqueued == [] and len(again.already) == 3
    assert len(_jobs(factory)) == 3


def test_new_slots_fire(factory) -> None:
    set_now(MONDAY_9_LOCAL - timedelta(minutes=1))
    first = tick(factory, notifier=Notifier())
    assert [t for t, _, _ in first.enqueued] == ["manager.sla_scan", "review.weekly_audit"]
    set_now(MONDAY_9_LOCAL)
    second = tick(factory, notifier=Notifier())
    assert sorted(t for t, _, _ in second.enqueued) == ["manager.digest", "manager.sla_scan"]
    set_now(datetime(2026, 12, 1, 0, 5, tzinfo=ALMATY))
    third = tick(factory, notifier=Notifier())
    assert ("review.monthly_quality", "2026-12") in [(t, s) for t, s, _ in third.enqueued]
    assert "manager.digest" not in [t for t, _, _ in third.enqueued]


def test_utc_now_is_converted_to_almaty(factory) -> None:
    set_now(datetime(2026, 11, 2, 3, 59, tzinfo=timezone.utc))
    assert "manager.digest" not in [t for t, _, _ in tick(factory, notifier=Notifier()).enqueued]
    set_now(datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc))
    assert "manager.digest" in [t for t, _, _ in tick(factory, notifier=Notifier()).enqueued]


def test_tick_recovers_expired_leases_and_dead_letters(factory) -> None:
    notifier = Notifier()
    start = MONDAY_9_LOCAL - timedelta(days=1)
    set_now(start)
    with factory() as s, s.begin():
        job_id = enqueue(s, "test.job", {}, queue="browser")
    when = start
    for _ in range(3):
        set_now(when)
        with factory() as s, s.begin():
            assert claim(s, "browser").id == job_id
        when += LEASE
        set_now(when)
        result = tick(factory, notifier=notifier)
        with factory() as s:
            when = s.get(Job, job_id).run_after
    with factory() as s:
        assert s.get(Job, job_id).status == "dead"
        assert s.scalar(select(func.count()).select_from(HumanTask)) == 1
    assert result.dead == [job_id] and len(notifier.messages) == 1


def test_concurrent_ticks_do_not_duplicate(factory) -> None:
    set_now(MONDAY_9_LOCAL)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait()
            tick(factory, notifier=Notifier())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(_jobs(factory)) == 3


def test_scheduler_tick_cli(factory, capsys) -> None:
    notifier = Notifier()
    assert main(["scheduler-tick", "--now", "2026-11-02T09:00"], factory=factory, notifier=notifier) == 0
    out = capsys.readouterr().out
    assert "поставлено 3" in out and "manager.digest 2026-11-02" in out
    assert main(["scheduler-tick", "--now", "2026-11-02T09:00"], factory=factory, notifier=notifier) == 0
    assert "поставлено 0, уже было 3" in capsys.readouterr().out
    assert notifier.messages == []


def test_scheduler_tick_needs_database(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["scheduler-tick", "--now", "2026-11-02T09:00"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
