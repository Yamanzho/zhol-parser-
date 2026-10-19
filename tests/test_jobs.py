from __future__ import annotations

import threading
import urllib.error
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import Company, Deal, HumanTask, Job, Order
from zhol.integrations import telegram
from zhol.jobs import LEASE, StaleJob, claim, complete, enqueue, fail, heartbeat, recover, run_one
from zhol.timeutil import reset_clock, set_now
from zhol.transitions import transition

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
FAKE_KEY = "sk" + "-" + "abcdefghijklmnop1234"


@pytest.fixture(autouse=True)
def _clock() -> Iterator[None]:
    set_now(T0)
    yield
    reset_clock()


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


class Notifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.messages: list[str] = []
        self.error = error

    def __call__(self, text: str) -> None:
        self.messages.append(text)
        if self.error is not None:
            raise self.error


def _boom(_payload: dict) -> None:
    raise RuntimeError(f"upstream failed token={FAKE_KEY} key {FAKE_KEY}")


def _add(factory: sessionmaker[Session], **kwargs: object) -> int:
    with factory() as s, s.begin():
        return enqueue(s, kwargs.pop("type", "test.job"), kwargs.pop("payload", {}), **kwargs)  # type: ignore[arg-type]


def _job(factory: sessionmaker[Session], job_id: int) -> Job:
    with factory() as s:
        return s.get(Job, job_id)


def _count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_enqueue_dedup_returns_existing(factory: sessionmaker[Session]) -> None:
    first = _add(factory, dedup_key="audit:1")
    second = _add(factory, dedup_key="audit:1", payload={"other": True})
    assert first == second
    assert _count(factory, Job) == 1


def test_enqueue_without_dedup_creates_rows(factory: sessionmaker[Session]) -> None:
    assert _add(factory) != _add(factory)


def test_concurrent_dedup(factory: sessionmaker[Session]) -> None:
    barrier = threading.Barrier(4)
    ids: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        job_id = _add(factory, dedup_key="scout:7")
        with lock:
            ids.append(job_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1
    assert _count(factory, Job) == 1


def test_dedup_key_reusable_after_done(factory: sessionmaker[Session]) -> None:
    first = _add(factory, dedup_key="k")
    assert run_one(factory, "main", {"test.job": lambda p: None}, notifier=Notifier()) == first
    second = _add(factory, dedup_key="k")
    assert second != first
    assert _job(factory, first).status == "done"


def test_unknown_queue_rejected(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        with pytest.raises(ValueError):
            enqueue(s, "x", {}, queue="slow")
        with pytest.raises(ValueError):
            claim(s, "slow")


def test_queue_isolation(factory: sessionmaker[Session]) -> None:
    main_id = _add(factory, queue="main")
    browser_id = _add(factory, queue="browser")
    with factory() as s, s.begin():
        assert claim(s, "browser").id == browser_id
        assert claim(s, "browser") is None
    with factory() as s, s.begin():
        assert claim(s, "main").id == main_id


def test_future_job_not_claimed(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory, run_after=T0 + timedelta(minutes=5))
    with factory() as s, s.begin():
        assert claim(s, "main") is None
    set_now(T0 + timedelta(minutes=5))
    with factory() as s, s.begin():
        assert claim(s, "main").id == job_id


def test_skip_locked_does_not_block(factory: sessionmaker[Session]) -> None:
    first = _add(factory)
    second = _add(factory)
    holder = factory()
    holder.begin()
    assert claim(holder, "main").id == first
    with factory() as s, s.begin():
        s.execute(select(func.set_config("lock_timeout", "2s", True)))
        assert claim(s, "main").id == second
    with factory() as s, s.begin():
        assert claim(s, "main") is None
    holder.rollback()
    holder.close()


def test_concurrent_workers_never_share_jobs(factory: sessionmaker[Session]) -> None:
    expected = {_add(factory) for _ in range(30)}
    claimed: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker() -> None:
        barrier.wait()
        while True:
            with factory() as s, s.begin():
                job = claim(s, "main")
                if job is None:
                    return
                job_id = job.id
            with lock:
                claimed.append(job_id)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == len(set(claimed))
    assert set(claimed) == expected


def test_success_marks_done(factory: sessionmaker[Session]) -> None:
    seen: list[dict] = []
    job_id = _add(factory, payload={"company_id": 5})
    assert run_one(factory, "main", {"test.job": seen.append}, notifier=Notifier()) == job_id
    job = _job(factory, job_id)
    assert (job.status, job.attempts) == ("done", 1)
    assert seen == [{"company_id": 5}]


def test_empty_queue_returns_none(factory: sessionmaker[Session]) -> None:
    assert run_one(factory, "main", {}, notifier=Notifier()) is None


def test_retry_progression_then_dead(factory: sessionmaker[Session]) -> None:
    notifier = Notifier()
    handlers = {"test.job": _boom}
    job_id = _add(factory, dedup_key="retry:1")

    run_one(factory, "main", handlers, notifier=notifier)
    job = _job(factory, job_id)
    assert (job.status, job.attempts, job.run_after) == ("failed", 1, T0 + timedelta(minutes=1))

    set_now(T0 + timedelta(seconds=59))
    assert run_one(factory, "main", handlers, notifier=notifier) is None

    set_now(T0 + timedelta(minutes=1))
    run_one(factory, "main", handlers, notifier=notifier)
    job = _job(factory, job_id)
    assert (job.status, job.attempts, job.run_after) == ("failed", 2, T0 + timedelta(minutes=6))
    assert _count(factory, HumanTask) == 0
    assert notifier.messages == []

    set_now(T0 + timedelta(minutes=6))
    run_one(factory, "main", handlers, notifier=notifier)
    job = _job(factory, job_id)
    assert (job.status, job.attempts, job.dedup_key) == ("dead", 3, None)
    assert "RuntimeError: upstream failed" in job.last_error

    with factory() as s:
        task = s.scalars(select(HumanTask)).one()
    assert (task.reason, task.object_type, task.object_id, task.status) == ("job_dead", "job", job_id, "open")
    assert task.details["attempts"] == 3
    assert len(notifier.messages) == 1
    assert str(job_id) in notifier.messages[0]

    set_now(T0 + timedelta(days=1))
    assert run_one(factory, "main", handlers, notifier=notifier) is None


def test_failure_reason_redacts_secrets(factory: sessionmaker[Session]) -> None:
    notifier = Notifier()
    job_id = _add(factory)
    for minutes in (0, 1, 6):
        set_now(T0 + timedelta(minutes=minutes))
        run_one(factory, "main", {"test.job": _boom}, notifier=notifier)
    job = _job(factory, job_id)
    with factory() as s:
        task = s.scalars(select(HumanTask)).one()
    for text in (job.last_error, task.details["last_error"], notifier.messages[0]):
        assert FAKE_KEY not in text
        assert "[redacted]" in text


def test_unknown_handler_is_failure(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory, type="nobody.handles")
    run_one(factory, "main", {}, notifier=Notifier())
    job = _job(factory, job_id)
    assert job.status == "failed"
    assert "no handler" in job.last_error


def test_notifier_failure_keeps_dead_and_human_task(factory: sessionmaker[Session]) -> None:
    notifier = Notifier(error=telegram.TelegramError("telegram HTTP 502"))
    job_id = _add(factory)
    for minutes in (0, 1, 6):
        set_now(T0 + timedelta(minutes=minutes))
        run_one(factory, "main", {"test.job": _boom}, notifier=notifier)
    assert _job(factory, job_id).status == "dead"
    assert _count(factory, HumanTask) == 1
    assert len(notifier.messages) == 1


def test_failure_rollback_leaves_job_running(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory)
    with factory() as s, s.begin():
        attempt = claim(s, "main").attempts
    s = factory()
    s.begin()
    fail(s, job_id, attempt, "boom")
    s.rollback()
    s.close()
    job = _job(factory, job_id)
    assert (job.status, job.last_error) == ("running", None)
    assert _count(factory, HumanTask) == 0


def test_enqueue_rolls_back_with_transition(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.begin()
        company = Company(id_2gis="1", name="A", source="manual")
        s.add(company)
        s.flush()
        deal = Deal(syntony_deal_id="d1", company_id=company.id)
        s.add(deal)
        s.flush()
        order = Order(deal_id=deal.id, company_id=company.id, kind="setup", amount_kzt=1, stage="prepaid")
        s.add(order)
        s.flush()
        transition(order, "intake", s)
        enqueue(s, "intake.check", {"order_id": order.id}, dedup_key=f"intake.check:{order.id}")
        s.rollback()
    assert _count(factory, Job) == 0
    assert _count(factory, Order) == 0


def test_stale_lease_recovered_and_old_worker_rejected(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory)
    with factory() as s, s.begin():
        stale_attempt = claim(s, "main").attempts

    set_now(T0 + LEASE - timedelta(seconds=1))
    assert recover(factory, notifier=Notifier()) == []
    assert _job(factory, job_id).status == "running"

    set_now(T0 + LEASE)
    assert recover(factory, notifier=Notifier()) == []
    job = _job(factory, job_id)
    assert (job.status, job.last_error, job.run_after) == ("failed", "lease expired", T0 + LEASE + timedelta(minutes=1))

    set_now(T0 + LEASE + timedelta(minutes=1))
    with factory() as s, s.begin():
        assert claim(s, "main").attempts == 2
    with factory() as s, s.begin():
        with pytest.raises(StaleJob):
            complete(s, job_id, stale_attempt)
    assert _job(factory, job_id).status == "running"


def test_stale_lease_on_last_attempt_goes_dead(factory: sessionmaker[Session]) -> None:
    notifier = Notifier()
    job_id = _add(factory)
    when = T0
    for _ in range(3):
        set_now(when)
        with factory() as s, s.begin():
            claim(s, "main")
        when += LEASE
        set_now(when)
        recover(factory, notifier=notifier)
        when = _job(factory, job_id).run_after
    job = _job(factory, job_id)
    assert (job.status, job.attempts) == ("dead", 3)
    assert _count(factory, HumanTask) == 1
    assert len(notifier.messages) == 1


def test_heartbeat_extends_owned_lease(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory)
    with factory() as s, s.begin():
        attempt = claim(s, "main").attempts
    set_now(T0 + timedelta(minutes=20))
    with factory() as s, s.begin():
        deadline = heartbeat(s, job_id, attempt)
    assert deadline == T0 + timedelta(minutes=20) + LEASE
    assert _job(factory, job_id).run_after == deadline
    set_now(T0 + LEASE)
    assert recover(factory, notifier=Notifier()) == []
    assert _job(factory, job_id).status == "running"


def test_heartbeat_rejects_stale_owner(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory)
    with factory() as s, s.begin():
        old = claim(s, "main").attempts
    set_now(T0 + LEASE)
    recover(factory, notifier=Notifier())
    set_now(T0 + LEASE + timedelta(minutes=1))
    with factory() as s, s.begin():
        claim(s, "main")
    before = _job(factory, job_id).run_after
    with factory() as s, s.begin():
        with pytest.raises(StaleJob):
            heartbeat(s, job_id, old)
    assert _job(factory, job_id).run_after == before


def test_heartbeat_rejects_finished_job(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory)
    with factory() as s, s.begin():
        attempt = claim(s, "main").attempts
    with factory() as s, s.begin():
        complete(s, job_id, attempt)
    with factory() as s, s.begin():
        with pytest.raises(StaleJob):
            heartbeat(s, job_id, attempt)


def test_run_one_keeps_long_job_alive(factory: sessionmaker[Session]) -> None:
    job_id = _add(factory)
    later = T0 + timedelta(minutes=25)
    seen: list[datetime] = []

    def slow(_payload: dict) -> None:
        set_now(later)
        for _ in range(100):
            run_after = _job(factory, job_id).run_after
            if run_after == later + LEASE:
                seen.append(run_after)
                return
            threading.Event().wait(0.02)

    assert run_one(factory, "main", {"test.job": slow}, notifier=Notifier(), heartbeat_every=0.02) == job_id
    assert seen == [later + LEASE]
    assert _job(factory, job_id).status == "done"


def test_telegram_requires_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RESPONSIBLE_TG_CHAT_ID", raising=False)
    with pytest.raises(telegram.TelegramError, match="not set"):
        telegram.notify("hi")


def test_telegram_error_hides_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "123456789:" + "A" * 30
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("RESPONSIBLE_TG_CHAT_ID", "42")
    sent: list[object] = []

    def fake_urlopen(request: object, timeout: float) -> object:
        sent.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)  # type: ignore[attr-defined]

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(telegram.TelegramError) as info:
        telegram.notify("hi")
    assert token not in str(info.value)
    assert info.value.__cause__ is None
    assert len(sent) == 1
