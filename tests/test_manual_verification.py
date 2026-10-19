"""Robot-check pause on the same local session, exercised with saved fixtures.

These tests do not open 2gis.kz, do not start Chrome, and do not write the
application database. The page text comes from tests/fixtures/selenium/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from card import _access_marker, _captcha_marker
from tests.test_selenium_collector import NORTH, SOUTH, ScriptedDriver
from zhol.agents.scout.browser_2gis import GRANT_COMMERCIAL, GRANT_ENV, GRANT_LOCAL
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.selenium_collector import (
    AWAITING_MANUAL_VERIFICATION,
    MESSAGE_ALREADY_OPEN,
    MESSAGE_DENIED,
    MESSAGE_FAILED,
    MESSAGE_HEADLESS,
    MESSAGE_NO_DISPLAY,
    MESSAGE_NOT_PERMITTED,
    MESSAGE_OPEN,
    MESSAGE_PASSED,
    MESSAGE_PENDING,
    MESSAGE_RESUME_BLOCKED,
    MESSAGE_SESSION_CLOSED,
    MESSAGE_STILL,
    MESSAGE_STOP,
    MESSAGE_TIMEOUT,
    MESSAGE_UNREAD,
    VERIFY_FAILED,
    VERIFY_PASSED,
    _hold_for_manual_verification,
    _open,
    run_browser_batch,
    run_visible_verification,
)
from zhol.api.admin_jobs import AdminJobError, handle_scout_batch, resume_batch, signal_batch
from zhol.api.main import app
from zhol.browser.discovery.types import RunControl
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, Job
from zhol.jobs.queue import worker_queue

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "selenium"
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
LOOPBACK = "http://127.0.0.1:8765"


class CaptchaSearch(ScriptedDriver):
    """The second firm page is the local captcha fixture until the operator clears it.

    Clearing the flag stands in for the person finishing the check. The test
    never asks the program to click it.
    """

    def __init__(self, kind: str = "captcha") -> None:
        super().__init__()
        self.kind = kind
        self.cleared = False
        self.clicks: list[str] = []

    def execute_script(self, script: str, *_args):
        if "captchaNodes" in (script or "") and SOUTH in (self.current_url or "") and not self.cleared:
            return _snapshot(self.current_url, self.kind)
        if "captchaNodes" in (script or ""):
            return _snapshot(self.current_url, "clear")
        return super().execute_script(script, *_args)

    def find_element(self, _by: str, _value: str):
        element = super().find_element(_by, _value)
        sink = self.clicks

        class _Watched:
            def is_displayed(self) -> bool:
                return True

            def click(self) -> None:
                sink.append("click")

        return _Watched()


def _snapshot(url: str, kind: str) -> dict:
    if kind == "captcha":
        text = (FIXTURE / "captcha.html").read_text(encoding="utf-8")
        return {"url": url, "title": "Проверка", "text": text, "frames": [], "captchaNodes": 1}
    if kind == "access":
        text = (FIXTURE / "access.html").read_text(encoding="utf-8")
        return {"url": url, "title": "Доступ", "text": text, "frames": [], "captchaNodes": 0}
    if kind == "blank":
        return None
    return {"url": url, "title": "card", "text": "North Beauty", "frames": [], "captchaNodes": 0}


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", LOOPBACK)
    monkeypatch.delenv(GRANT_ENV, raising=False)
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def test_local_fixture_pages_are_classified_without_a_browser() -> None:
    captcha = (FIXTURE / "captcha.html").read_text(encoding="utf-8")
    access = (FIXTURE / "access.html").read_text(encoding="utf-8")
    captcha_page = {"url": f"{LOOPBACK}/check", "title": "Проверка", "text": captcha, "frames": []}
    access_page = {"url": f"{LOOPBACK}/denied", "title": "Доступ", "text": access, "frames": []}
    assert _captcha_marker(captcha_page) == "не робот"
    assert _access_marker(captcha_page) is None
    assert _captcha_marker(access_page) is None
    assert _access_marker(access_page) == "доступ ограничен"


def test_no_display_keeps_the_commit_and_does_not_open_chrome(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZHOL_CHROME_CONTAINER", "1")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("parser_service._start_chrome", _refuse_chrome)
    driver = CaptchaSearch()
    batch_id = _collect(factory, tmp_path, driver, limit=2, delay_range=(1.5, 2.0))
    assert driver.closed is True
    assert driver.clicks == []
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == AWAITING_MANUAL_VERIFICATION
        assert batch.item_limit == 2
        progress = batch.progress or {}
        assert progress["seen_ids"] == [NORTH]
        assert SOUTH not in (progress.get("rejected_ids") or [])
        assert progress["delay_range"] == [1.5, 2.0]
        notice = progress["manual_verification"]
        assert notice["outcome"] == "pending"
        assert notice["browser"] == "closed"
        assert notice["message"] == MESSAGE_NO_DISPLAY
        assert notice["url"].startswith(LOOPBACK)
        assert "2gis.kz" not in notice["url"]
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 1
    status = run_visible_verification(factory, batch_id, driver_factory=_refuse_chrome)
    assert status == AWAITING_MANUAL_VERIFICATION


def test_same_session_continues_only_after_the_check_is_gone(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parser_service._start_chrome", _refuse_chrome)
    driver = CaptchaSearch()
    seen: dict[str, object] = {}

    def sleep(_delay: float) -> None:
        batch_id = _waiting_id(factory)
        with factory() as db:
            notice = (db.get(CollectionBatch, batch_id).progress or {})["manual_verification"]
            seen["browser"] = notice["browser"]
            seen["message"] = notice["message"]
            seen["url"] = notice["url"]
        with factory() as db, db.begin():
            with pytest.raises(AdminJobError, match=MESSAGE_ALREADY_OPEN):
                from zhol.api.admin_jobs import start_verification_browser

                start_verification_browser(db, batch_id)
        driver.cleared = True
        _signal(factory, batch_id, VERIFY_PASSED)

    batch_id = _collect(
        factory,
        tmp_path,
        driver,
        limit=2,
        delay_range=(1.5, 2.0),
        operator_sleep=sleep,
    )
    assert seen["browser"] == "open"
    assert MESSAGE_OPEN in str(seen["message"])
    assert str(seen["url"]).startswith(LOOPBACK)
    assert driver.closed is True
    assert driver.clicks == []
    assert any(SOUTH in url for url in driver.gets)
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.item_limit == 2
        progress = batch.progress or {}
        assert progress["seen_ids"] == [NORTH, SOUTH]
        assert progress["delay_range"] == [1.5, 2.0]
        notice = progress["manual_verification"]
        assert notice["outcome"] == "passed"
        assert notice["browser"] == "closed"
        assert MESSAGE_PASSED in notice["message"]
        assert db.scalar(select(func.count()).select_from(Company)) == 2
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 2
        company = db.scalar(select(Company).where(Company.id_2gis == NORTH))
        assert company is not None
        shots = db.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company.id)).all()
        assert len(shots) == 1
        with pytest.raises(AdminJobError, match="batch is completed"):
            from zhol.api.admin_jobs import start_verification_browser

            start_verification_browser(db, batch_id)

    def another_session() -> CaptchaSearch:
        raise AssertionError("a new chrome session must not start")

    again = run_browser_batch(
        factory,
        city="Алматы",
        category="Салон красоты",
        limit=50,
        out=tmp_path / "ignored.txt",
        resume=batch_id,
        driver_factory=another_session,
        run_dir=tmp_path / "resume",
        delay_range=(0.0, 0.0),
    )
    assert again == batch_id
    with factory() as db:
        assert db.get(CollectionBatch, batch_id).item_limit == 2
        assert db.scalar(select(func.count()).select_from(Company)) == 2
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 2


def test_failed_or_blocked_verification_stops_and_closes_the_same_chrome(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parser_service._start_chrome", _refuse_chrome)
    still = CaptchaSearch()
    still_id = _collect(factory, tmp_path / "still", still, operator_sleep=_signal_sleep(factory, VERIFY_PASSED))
    assert still.closed is True
    assert still.clicks == []
    _assert_stopped(factory, still_id, MESSAGE_STILL, "failed")

    denied = CaptchaSearch()
    denied_id = _collect(
        factory,
        tmp_path / "denied",
        denied,
        operator_sleep=_signal_sleep(factory, VERIFY_PASSED, driver=denied, kind="access"),
    )
    assert denied.closed is True
    assert denied.clicks == []
    _assert_stopped(factory, denied_id, MESSAGE_DENIED, "not_permitted")

    failed = CaptchaSearch()
    failed_id = _collect(
        factory,
        tmp_path / "failed",
        failed,
        operator_sleep=_signal_sleep(factory, VERIFY_FAILED, driver=failed, clear=True),
    )
    assert failed.closed is True
    _assert_stopped(factory, failed_id, MESSAGE_FAILED, "failed")

    unread = CaptchaSearch()
    unread_id = _collect(
        factory,
        tmp_path / "unread",
        unread,
        operator_sleep=_signal_sleep(factory, VERIFY_PASSED, driver=unread, kind="blank"),
    )
    assert unread.closed is True
    _assert_stopped(factory, unread_id, MESSAGE_UNREAD, "failed")


def test_timeout_and_a_closed_session_do_not_start_chrome(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parser_service._start_chrome", _refuse_chrome)
    driver = CaptchaSearch()
    batch_id = _collect(factory, tmp_path, driver, verification_wait_seconds=0)
    assert driver.closed is True
    with factory() as db:
        notice = (db.get(CollectionBatch, batch_id).progress or {})["manual_verification"]
        assert notice["outcome"] == "pending"
        assert notice["browser"] == "closed"
        assert notice["message"] == MESSAGE_TIMEOUT
        assert db.scalar(select(func.count()).select_from(Company)) == 1
    with factory() as db, db.begin():
        from zhol.api.admin_jobs import start_verification_browser

        body = start_verification_browser(db, batch_id)
        assert body["id"] is None
        assert body["message"] == MESSAGE_SESSION_CLOSED
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    status = run_visible_verification(factory, batch_id, driver_factory=_refuse_chrome)
    assert status == AWAITING_MANUAL_VERIFICATION


def test_headless_session_is_closed_instead_of_replaced(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parser_service._start_chrome", _refuse_chrome)
    driver = CaptchaSearch()
    driver._zhol_headed = False
    batch_id = _collect(factory, tmp_path, driver)
    assert driver.closed is True
    with factory() as db:
        notice = (db.get(CollectionBatch, batch_id).progress or {})["manual_verification"]
        assert notice["message"] == MESSAGE_HEADLESS
        assert notice["browser"] == "closed"
        assert db.scalar(select(func.count()).select_from(Company)) == 1


def test_macos_raises_the_chrome_process_that_belongs_to_the_session(monkeypatch) -> None:
    import parser_service

    calls: list[list[str]] = []
    monkeypatch.setattr(
        parser_service,
        "_process_rows",
        lambda: [
            (10, 1, "chromedriver --port=1"),
            (11, 10, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/tmp/owned"),
            (12, 1, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/tmp/personal"),
        ],
    )
    monkeypatch.setattr(parser_service, "_service_pid", lambda _driver: 10)

    def run(cmd, **_kwargs):
        calls.append(list(cmd))

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(parser_service.sys, "platform", "darwin")
    driver = type("Owned", (), {"_zhol_headed": True})()
    assert parser_service.show_owned_window(driver) is True
    assert calls and calls[0][:2] == ["osascript", "-e"]
    assert "unix id is 11" in calls[0][2]
    assert "unix id is 12" not in calls[0][2]
    assert parser_service.show_owned_window(type("Hidden", (), {"_zhol_headed": False})()) is False


def test_headed_chrome_options_do_not_hide_the_window(monkeypatch, tmp_path: Path) -> None:
    import parser_service

    monkeypatch.setenv("ZHOL_CHROME_PROFILE", str(tmp_path / "profile"))
    shown = parser_service._chrome_options(False)
    hidden = parser_service._chrome_options(True)
    assert not any("headless" in item for item in shown.arguments)
    assert any(item == "--headless=new" for item in hidden.arguments)


def test_unpermitted_source_stops_without_opening_chrome(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv(GRANT_ENV, raising=False)
    with factory() as db, db.begin():
        db.add(Company(id_2gis=NORTH, name="Kept", city="Алматы", niche="Салон красоты", source="manual"))
    batch_id = _seed_waiting(factory, tmp_path)
    status = run_visible_verification(factory, batch_id, driver_factory=_refuse_chrome)
    assert status == "stopped"
    _assert_stopped(factory, batch_id, MESSAGE_NOT_PERMITTED, "not_permitted")


def test_local_grant_is_not_stored_as_commercial(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.setenv(GRANT_ENV, GRANT_LOCAL)
    assert GRANT_LOCAL != GRANT_COMMERCIAL
    batch_id = _open(factory, city="Алматы", category="Салон красоты", limit=2, out=tmp_path / "out.txt")
    _hold_for_manual_verification(factory, batch_id, url=f"{LOOPBACK}/firm/{NORTH}", reason="2GIS требует подтверждение в браузере.")
    with factory() as db:
        notice = (db.get(CollectionBatch, batch_id).progress or {})["manual_verification"]
        assert notice["authorization_scope"] == "local"
        assert notice["authorization_scope"] != "commercial"
        assert "Коммерческое разрешение не подтверждено." in notice["message"]
        assert "коммерческое использование" not in notice["message"]


def test_stop_during_the_wait_closes_the_same_chrome(factory, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("parser_service._start_chrome", _refuse_chrome)
    driver = CaptchaSearch()
    batch_id = _collect(factory, tmp_path, driver, operator_sleep=_signal_sleep(factory, "stop"))
    assert driver.closed is True
    assert driver.clicks == []
    with factory() as db:
        assert db.get(CollectionBatch, batch_id).status == "stopped"
        assert (db.get(CollectionBatch, batch_id).progress or {})["error"] == MESSAGE_STOP
        assert db.scalar(select(func.count()).select_from(Company)) == 1


def test_stop_remains_terminal_and_pause_does_not_drop_the_wait(factory, tmp_path: Path) -> None:
    batch_id = _awaiting(factory, tmp_path)
    with factory() as db, db.begin():
        with pytest.raises(AdminJobError, match="Пауза здесь не нужна"):
            signal_batch(db, batch_id, "pause")
        with pytest.raises(AdminJobError, match=MESSAGE_RESUME_BLOCKED):
            resume_batch(db, batch_id)
    with factory() as db, db.begin():
        stopped = signal_batch(db, batch_id, "stop")
        assert stopped.status == "stopped"
    with factory() as db:
        assert db.get(CollectionBatch, batch_id).status == "stopped"
        assert (db.get(CollectionBatch, batch_id).progress or {})["error"] == MESSAGE_STOP
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 1


def test_admin_batch_shows_the_verification_message(factory, tmp_path: Path) -> None:
    batch_id = _seed_waiting(factory, tmp_path)
    with TestClient(app) as client:
        body = client.get(f"/api/v1/admin/scout/batches/{batch_id}", headers=HEADERS)
    assert body.status_code == 200
    payload = body.json()
    assert payload["status"] == AWAITING_MANUAL_VERIFICATION
    assert payload["manual_verification"]["outcome"] == "pending"
    assert payload["manual_verification"]["message"] == MESSAGE_PENDING
    assert "не решает" in payload["manual_verification"]["message"]


def test_verification_job_stays_on_the_collector_queue(factory, tmp_path: Path, monkeypatch) -> None:
    batch_id = _awaiting(factory, tmp_path)
    calls: list[int] = []

    def seen(factory, batch, **_kwargs):
        calls.append(batch)
        return AWAITING_MANUAL_VERIFICATION

    monkeypatch.setattr("zhol.agents.scout.selenium_collector.run_visible_verification", seen)
    payload = {
        "action": "manual_verification",
        "source_mode": "browser_2gis",
        "batch_id": batch_id,
        "resume": batch_id,
        "dedup_key": "verify-route",
    }
    with factory() as db, db.begin():
        db.add(
            Job(
                type="admin.scout_batch",
                queue="collector",
                status="running",
                attempts=1,
                payload=payload,
                dedup_key="verify-route",
            )
        )
    handle_scout_batch(factory, payload)
    assert calls == [batch_id]
    token = worker_queue.set("main")
    try:
        with pytest.raises(BatchError, match="collector"):
            handle_scout_batch(factory, payload)
    finally:
        worker_queue.reset(token)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CollectionBatchItem)) == 1


def _collect(factory, tmp_path: Path, driver: ScriptedDriver, **kwargs) -> int:
    return run_browser_batch(
        factory,
        city="Алматы",
        category="Салон красоты",
        limit=kwargs.pop("limit", 2),
        out=tmp_path / "batch-out.txt",
        driver_factory=lambda: driver,
        run_dir=tmp_path / "run",
        delay_range=kwargs.pop("delay_range", (0.0, 0.0)),
        **kwargs,
    )


def _awaiting(factory, tmp_path: Path) -> int:
    return _collect(factory, tmp_path, CaptchaSearch(), limit=2, verification_wait_seconds=0)


def _refuse_chrome(*_args, **_kwargs):
    raise AssertionError("a new chrome session must not start")


def _waiting_id(factory) -> int:
    with factory() as db:
        found = db.scalar(
            select(CollectionBatch.id).where(CollectionBatch.status == AWAITING_MANUAL_VERIFICATION)
        )
    assert found is not None
    return int(found)


def _signal_sleep(factory, signal: str, *, driver: CaptchaSearch | None = None, kind: str | None = None, clear: bool = False):
    def sleep(_delay: float) -> None:
        if driver is not None:
            if kind is not None:
                driver.kind = kind
            driver.cleared = clear
        _signal(factory, _waiting_id(factory), signal)

    return sleep


def _seed_waiting(factory, tmp_path: Path) -> int:
    batch_id = _open(factory, city="Алматы", category="Салон красоты", limit=2, out=tmp_path / "out.txt")
    _hold_for_manual_verification(factory, batch_id, url=f"{LOOPBACK}/check", reason="2GIS требует подтверждение в браузере.")
    return batch_id


def _signal(factory, batch_id: int, signal: str) -> None:
    with factory() as db, db.begin():
        signal_batch(db, batch_id, signal)


def test_status_migration_applies_only_on_the_isolated_database(migrated_engine, postgres_url: str) -> None:
    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from tests.conftest import alembic_config

    cfg = alembic_config(postgres_url)
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "e5a9b3c20f41"
        before = conn.execute(text("SELECT count(*) FROM companies")).scalar_one()
    command.downgrade(cfg, "d4f8a2c19e30")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "d4f8a2c19e30"
    command.downgrade(cfg, "c3e7a1b84d02")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "c3e7a1b84d02"
        definition = conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_collection_batches_status'")
        ).scalar_one()
        assert "awaiting_manual_verification" not in definition
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(text(_WAITING_BATCH))
    command.upgrade(cfg, "d4f8a2c19e30")
    with migrated_engine.begin() as conn:
        conn.execute(text(_WAITING_BATCH))
        waiting = conn.execute(
            text("SELECT count(*) FROM collection_batches WHERE status = 'awaiting_manual_verification'")
        ).scalar_one()
        assert waiting == 1
    with pytest.raises(RuntimeError, match="downgrade stopped"):
        command.downgrade(cfg, "c3e7a1b84d02")
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM collection_batches"))
    command.downgrade(cfg, "c3e7a1b84d02")
    with migrated_engine.connect() as conn:
        definition = conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_collection_batches_status'")
        ).scalar_one()
        assert "awaiting_manual_verification" not in definition
        assert conn.execute(text("SELECT count(*) FROM companies")).scalar_one() == before
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "e5a9b3c20f41"


_WAITING_BATCH = """
INSERT INTO collection_batches
    (city, category, platforms, source_mode, status, out_path, catalog_path)
VALUES
    ('Алматы', 'Салон красоты', '["2gis"]'::jsonb, 'browser_2gis',
     'awaiting_manual_verification', '/tmp/zhol-migration-test', 'browser_2gis')
"""


def _assert_stopped(factory, batch_id: int, message: str, outcome: str) -> None:
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "stopped"
        notice = (batch.progress or {})["manual_verification"]
        assert notice["outcome"] == outcome
        assert notice["browser"] == "closed"
        assert notice["message"] == message
        assert db.scalar(select(func.count()).select_from(Company)) == 1
