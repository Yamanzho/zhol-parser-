"""Collection metrics, temporary failures, and resume. Chrome talks only to 127.0.0.1."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import threading
import time
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.jobs import handlers
from zhol.agents.scout.selenium_collector import (
    BrowserFirm,
    _commit_failure,
    _commit_firm,
    _open,
    classify_failure,
    run_browser_batch,
)
from zhol.api.main import app
from zhol.browser.discovery.types import RunControl
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, Job
from zhol.jobs import enqueue, run_one
from zhol.timeutil import now
from zhol.transitions import transition

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "selenium-load" / "server.py"
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
NETWORK = "70000001000016001"
TIMEOUT = "70000001000016002"
GOOD = "70000001000016003"
EMPTY = "70000001000016004"
BROKEN = "70000001000016005"
BLOCKED = "70000001000016006"


def _load_fixture():
    spec = importlib.util.spec_from_file_location("selenium_fault_server", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


@pytest.fixture
def faults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if not Path(CHROME).is_file() and not os.environ.get("ZHOL_CHROME_BINARY"):
        pytest.skip("Chrome is not installed")
    module = _load_fixture()
    cards = [
        module.Card(NETWORK, "Салон Сеть", "улица Сбоя, 1", module._phone(16001), None, None, query="Проверка сбоев"),
        module.Card(TIMEOUT, "Салон Таймаут", "улица Сбоя, 2", module._phone(16002), None, None, query="Проверка сбоев"),
        module.Card(GOOD, "Салон Живой", "улица Сбоя, 3", module._phone(16003), None, None, query="Проверка сбоев"),
        module.Card(EMPTY, "Салон Пустое Имя", "улица Сбоя, 4", module._phone(16004), None, None, empty_name=True, query="Проверка сбоев"),
        module.Card(BROKEN, "Салон Сломанная Карточка", "улица Сбоя, 5", module._phone(16005), None, None, malformed=True, query="Проверка сбоев"),
        module.Card(BLOCKED, "Салон Закрытый", "улица Сбоя, 6", module._phone(16006), None, None, restricted=True, query="Проверка сбоев"),
    ]
    for card in cards:
        module.BY_ID[card.firm_id] = card
    network, timeout, good, empty, broken, blocked = cards
    module.QUERIES["Проверка сбоев"] = cards
    module.QUERIES["Проверка сети"] = [network, good]
    module.QUERIES["Проверка таймаута"] = [timeout]
    module.QUERIES["Проверка отказа"] = [empty, broken, blocked, good]
    module.QUERIES["Проверка паузы"] = [network, good]
    state = {"hang": "", "block": set(), "paths": []}

    class Handler(module.Handler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            state["paths"].append(path)
            firm = path.rstrip("/").rsplit("/firm/", 1)[-1] if "/firm/" in path else ""
            if firm and firm == state["hang"]:
                time.sleep(40)
                return
            if firm and firm in state["block"]:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.close_connection = True
                return
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, name="fault-lab", daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    binary = os.environ.get("ZHOL_CHROME_BINARY", "").strip() or CHROME
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", origin)
    monkeypatch.setenv("ZHOL_CHROME_HEADLESS", "1")
    monkeypatch.setenv("ZHOL_CHROME_BINARY", binary)
    monkeypatch.setenv("ZHOL_CHROME_PROFILE", str(tmp_path / "chrome-profile"))
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    try:
        yield {"origin": origin, "binary": binary, "state": state, "root": tmp_path}
    finally:
        server.shutdown()
        server.server_close()


def _chrome(binary: str, profile: Path):
    options = Options()
    options.page_load_strategy = "eager"
    options.binary_location = binary
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "--host-resolver-rules="
        "MAP 2gis.kz 0.0.0.0, MAP www.2gis.kz 0.0.0.0, MAP 2gis.ru 0.0.0.0, "
        "MAP yandex.ru 0.0.0.0, MAP yandex.kz 0.0.0.0, MAP maps.yandex.ru 0.0.0.0, "
        "MAP maps.google.com 0.0.0.0"
    )
    driver_path = os.environ.get("ZHOL_CHROMEDRIVER", "").strip()
    service = Service(executable_path=driver_path) if driver_path else None
    return webdriver.Chrome(options=options, service=service)


class _Drop:
    """Real Chrome for every page except one firm, which fails like a reset connection."""

    def __init__(self, inner, blocked: set[str]) -> None:
        self._inner = inner
        self._blocked = blocked

    def get(self, url: str) -> None:
        if any(firm in (url or "") for firm in self._blocked):
            raise WebDriverException("unknown error: net::ERR_CONNECTION_RESET")
        self._inner.get(url)

    def quit(self) -> None:
        try:
            self._inner.quit()
        except Exception:
            pass

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _Gate:
    def __init__(self, inner, *, after: int, action) -> None:
        self._inner = inner
        self._after = after
        self._action = action
        self.cards = 0

    def execute_script(self, script, *args):
        text = script or ""
        if "return window.gisExtract" in text:
            self.cards += 1
            if self.cards == self._after:
                self._action()
        return self._inner.execute_script(script, *args)

    def quit(self) -> None:
        try:
            self._inner.quit()
        except Exception:
            pass

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _run(factory, folder: Path, lab: dict, **kwargs) -> int:
    profile = folder / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    os.environ["ZHOL_CHROME_PROFILE"] = str(profile)
    driver_factory = kwargs.pop("driver_factory", None)
    if driver_factory is None:
        driver_factory = lambda: _chrome(lab["binary"], profile)  # noqa: E731
    return run_browser_batch(
        factory,
        city="Алматы",
        category=kwargs.pop("category", "Проверка сбоев"),
        limit=kwargs.pop("limit"),
        out=folder / "out.txt",
        resume=kwargs.pop("resume", None),
        control=kwargs.pop("control", None),
        driver_factory=driver_factory,
        run_dir=folder / "run",
        delay_range=(0.0, 0.0),
        job_attempt=kwargs.pop("job_attempt", None),
    )


def _progress(factory, batch_id: int) -> tuple[dict, str]:
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        return dict(batch.progress or {}), batch.status


def _item(factory, batch_id: int, firm_id: str) -> CollectionBatchItem:
    with factory() as db:
        row = db.scalar(
            select(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == batch_id,
                CollectionBatchItem.id_2gis == firm_id,
            )
        )
        assert row is not None
        db.expunge(row)
        return row


def _local(lab: dict) -> None:
    paths = lab["state"]["paths"]
    assert paths
    assert all("2gis." not in path and "yandex." not in path and "google." not in path for path in paths)


def _firm() -> BrowserFirm:
    return BrowserFirm(
        firm_id="70000001000017001",
        name="Салон Откат",
        city="Алматы",
        address="улица Отката, 1",
        address_reason=None,
        phones=("+77010001701",),
        phone_display=("+7 701 000 17 01",),
        phones_reason=None,
        whatsapp=None,
        whatsapp_reason="empty",
        instagram=None,
        instagram_reason="empty",
        url="http://127.0.0.1/almaty/firm/70000001000017001",
        query="Салон красоты",
        collected_at="",
    )


def test_failure_classes_do_not_retry_captcha_access_or_bad_identity() -> None:
    assert classify_failure("На странице капча") == "captcha"
    assert classify_failure("Доступ к карточке ограничен, обход не выполняется") == "access_restriction"
    assert classify_failure("Нет идентификатора филиала или названия", "identity") == "missing_identity"
    assert classify_failure("В карточке не найдено название") == "missing_identity"
    assert classify_failure("На странице нет названия или идентификатора компании") == "invalid_card"
    assert classify_failure("Страница карточки загружается слишком долго") == "page_timeout"
    assert classify_failure("unknown error: net::ERR_CONNECTION_RESET") == "network_failure"
    assert classify_failure("tab crashed") == "browser_session"


def test_temporary_failure_stays_pending_until_the_third_attempt(factory, tmp_path: Path) -> None:
    batch_id = _open(factory, city="Алматы", category="Салон", limit=3, out=tmp_path / "out.txt")
    firm_id = "70000001000018001"
    _commit_failure(factory, batch_id, firm_id, "Страница карточки загружается слишком долго")
    _commit_failure(factory, batch_id, firm_id, "unknown error: net::ERR_CONNECTION_RESET")
    progress, _status = _progress(factory, batch_id)
    row = _item(factory, batch_id, firm_id)
    assert row.status == "pending"
    assert row.attempts == 2
    assert row.detail["retryable"] is True
    assert firm_id not in progress["rejected_ids"]
    assert firm_id not in progress["seen_ids"]
    assert progress["metrics"]["failed_records"] == 0
    _commit_failure(factory, batch_id, firm_id, "tab crashed")
    progress, _status = _progress(factory, batch_id)
    row = _item(factory, batch_id, firm_id)
    assert row.status == "failed"
    assert row.attempts == 3
    assert row.detail["retryable"] is False
    assert row.detail["classification"] == "browser_session"
    assert firm_id in progress["rejected_ids"]
    assert firm_id not in progress["seen_ids"]
    assert progress["metrics"]["failed_records"] == 1


def test_captcha_and_access_are_rejected_once(factory, tmp_path: Path) -> None:
    batch_id = _open(factory, city="Алматы", category="Салон", limit=2, out=tmp_path / "out.txt")
    _commit_failure(factory, batch_id, "70000001000018002", "Подтвердите, что вы не робот, капча")
    _commit_failure(factory, batch_id, "70000001000018002", "Подтвердите, что вы не робот, капча")
    _commit_failure(factory, batch_id, "70000001000018003", "Доступ к карточке ограничен")
    captcha = _item(factory, batch_id, "70000001000018002")
    access = _item(factory, batch_id, "70000001000018003")
    assert captcha.status == "failed"
    assert captcha.attempts == 1
    assert captcha.detail["classification"] == "captcha"
    assert access.detail["classification"] == "access_restriction"
    progress, _status = _progress(factory, batch_id)
    assert "70000001000018002" in progress["rejected_ids"]
    assert "70000001000018003" in progress["rejected_ids"]


def test_failed_commit_rolls_back_the_checkpoint(factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    batch_id = _open(factory, city="Алматы", category="Салон", limit=1, out=tmp_path / "out.txt")

    def boom(*_args, **_kwargs):
        raise RuntimeError("rollback")

    monkeypatch.setattr("zhol.agents.scout.selenium_collector._snapshot", boom)
    with pytest.raises(RuntimeError, match="rollback"):
        _commit_firm(factory, batch_id, _firm())
    progress, status = _progress(factory, batch_id)
    assert status == "running"
    assert progress["seen_ids"] == []
    assert progress["metrics"]["companies_committed"] == 0
    assert progress["metrics"]["snapshots_written"] == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatchItem)) == 0


def test_network_failure_is_retried_without_a_second_snapshot(factory, faults, tmp_path: Path) -> None:
    faults["state"]["block"].add(NETWORK)

    def dropping():
        return _Drop(_chrome(faults["binary"], tmp_path / "net-profile"), faults["state"]["block"])

    batch_id = _run(
        factory,
        tmp_path / "first",
        faults,
        category="Проверка сети",
        limit=2,
        driver_factory=dropping,
    )
    progress, status = _progress(factory, batch_id)
    row = _item(factory, batch_id, NETWORK)
    assert status == "interrupted"
    assert row.status == "pending"
    assert row.detail["classification"] == "network_failure", row.detail
    assert NETWORK not in progress["seen_ids"]
    assert NETWORK not in progress["rejected_ids"]
    good = _item(factory, batch_id, GOOD)
    assert good.status == "completed"
    with factory() as db:
        snapshot = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id == good.company_id))
        assert snapshot is not None
        transition(snapshot, "verified", db)
        db.commit()
        kept = dict(snapshot.fields)
        kept_status = snapshot.status
        snapshot_id = snapshot.id
    faults["state"]["block"].discard(NETWORK)
    faults["state"]["paths"].clear()
    _run(
        factory,
        tmp_path / "second",
        faults,
        category="Проверка сети",
        limit=2,
        resume=batch_id,
        driver_factory=dropping,
    )
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert NETWORK in progress["seen_ids"]
    with factory() as db:
        company = db.scalar(select(Company).where(Company.id_2gis == NETWORK))
        assert company is not None
        snaps = list(db.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company.id)).all())
        assert len(snaps) == 1
        original = db.get(CardSnapshot, snapshot_id)
        assert original is not None
        assert original.status == kept_status == "verified"
        assert original.fields == kept
    _local(faults)


def test_page_timeout_waits_for_retry_and_then_saves_once(factory, faults, tmp_path: Path) -> None:
    faults["state"]["hang"] = TIMEOUT
    batch_id = _run(factory, tmp_path / "hang", faults, category="Проверка таймаута", limit=1)
    progress, status = _progress(factory, batch_id)
    row = _item(factory, batch_id, TIMEOUT)
    assert status == "interrupted"
    assert row.status == "pending"
    assert row.detail["classification"] == "page_timeout", row.detail
    assert TIMEOUT not in progress["seen_ids"]
    assert progress["metrics"]["companies_committed"] == 0
    faults["state"]["hang"] = ""
    _run(factory, tmp_path / "ready", faults, category="Проверка таймаута", limit=1, resume=batch_id)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert progress["metrics"]["snapshots_written"] == 1
    assert progress["metrics"]["companies_committed"] == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 1
    _local(faults)


def test_network_retries_stop_after_three_and_are_not_opened_again(factory, faults, tmp_path: Path) -> None:
    faults["state"]["block"].add(NETWORK)

    def dropping():
        return _Drop(_chrome(faults["binary"], tmp_path / "drop-profile"), faults["state"]["block"])

    batch_id = _run(factory, tmp_path / "a", faults, category="Проверка сети", limit=1, driver_factory=dropping)
    _run(factory, tmp_path / "b", faults, category="Проверка сети", limit=1, resume=batch_id, driver_factory=dropping)
    _run(factory, tmp_path / "c", faults, category="Проверка сети", limit=1, resume=batch_id, driver_factory=dropping)
    progress, status = _progress(factory, batch_id)
    row = _item(factory, batch_id, NETWORK)
    assert status == "completed"
    assert row.status == "failed"
    assert row.attempts == 3
    assert row.detail["classification"] == "network_failure"
    assert row.detail["retryable"] is False
    assert NETWORK in progress["rejected_ids"]
    assert progress["metrics"]["failed_records"] == 1
    before = len(faults["state"]["paths"])
    again = _run(factory, tmp_path / "d", faults, category="Проверка сети", limit=1, resume=batch_id)
    assert again == batch_id
    assert not any(NETWORK in path for path in faults["state"]["paths"][before:])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
    _local(faults)


def test_invalid_card_and_access_block_are_not_retried(factory, faults, tmp_path: Path) -> None:
    batch_id = _run(factory, tmp_path / "bad", faults, category="Проверка отказа", limit=4)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    empty = _item(factory, batch_id, EMPTY)
    broken = _item(factory, batch_id, BROKEN)
    blocked = _item(factory, batch_id, BLOCKED)
    saved = _item(factory, batch_id, GOOD)
    assert empty.status == "failed"
    assert empty.detail["classification"] == "missing_identity"
    assert broken.status == "failed"
    assert broken.detail["classification"] == "invalid_card", broken.detail
    assert blocked.status == "failed"
    assert blocked.detail["classification"] == "access_restriction"
    assert saved.status == "completed"
    for firm_id in (EMPTY, BROKEN, BLOCKED):
        assert firm_id in progress["rejected_ids"]
        assert firm_id not in progress["seen_ids"]
    _local(faults)
    faults["state"]["paths"].clear()
    _run(factory, tmp_path / "again", faults, category="Проверка отказа", limit=4, resume=batch_id)
    assert not any(firm_id in path for path in faults["state"]["paths"] for firm_id in (EMPTY, BROKEN, BLOCKED))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 1


def test_pause_keeps_a_temporary_card_and_resume_saves_it_once(factory, faults, tmp_path: Path) -> None:
    faults["state"]["block"].add(NETWORK)
    control = RunControl()

    def pausing():
        dropped = _Drop(_chrome(faults["binary"], tmp_path / "pause-profile"), faults["state"]["block"])
        return _Gate(dropped, after=1, action=control.pause)

    batch_id = _run(
        factory,
        tmp_path / "paused",
        faults,
        category="Проверка паузы",
        limit=2,
        control=control,
        driver_factory=pausing,
    )
    progress, status = _progress(factory, batch_id)
    assert status == "paused"
    waiting = _item(factory, batch_id, NETWORK)
    assert waiting.status == "pending"
    assert waiting.detail["classification"] == "network_failure"
    assert NETWORK not in progress["seen_ids"]
    assert progress["metrics"]["companies_committed"] >= 1
    faults["state"]["block"].discard(NETWORK)
    _run(factory, tmp_path / "resume", faults, category="Проверка паузы", limit=2, resume=batch_id)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert NETWORK in progress["seen_ids"]
    with factory() as db:
        rows = list(db.scalars(select(CardSnapshot)).all())
        keys = [row.field_sources["_collection"]["key"] for row in rows]
        assert len(keys) == len(set(keys))
        network = db.scalar(select(Company).where(Company.id_2gis == NETWORK))
        assert network is not None
        assert db.scalar(select(func.count()).select_from(CardSnapshot).where(CardSnapshot.company_id == network.id)) == 1
    _local(faults)


def test_browser_crash_is_temporary_and_resume_does_not_duplicate(factory, faults, tmp_path: Path) -> None:
    def crash() -> None:
        raise WebDriverException("tab crashed")

    def crashing():
        return _Gate(_chrome(faults["binary"], tmp_path / "crash-profile"), after=2, action=crash)

    with pytest.raises(WebDriverException, match="tab crashed"):
        _run(factory, tmp_path / "crash", faults, category="Салон красоты", limit=4, driver_factory=crashing)
    with factory() as db:
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"
        assert (batch.progress or {})["interruption"]["classification"] == "browser_session"
        assert (batch.progress or {})["interruption"]["retryable"] is True
        committed = len((batch.progress or {}).get("seen_ids") or [])
        assert db.scalar(select(func.count()).select_from(Company)) == committed
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == committed
        batch_id = batch.id
    _run(factory, tmp_path / "after", faults, category="Салон красоты", limit=4, resume=batch_id)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert progress["metrics"]["browser_restarts"] >= 1
    assert progress["metrics"]["snapshots_written"] == 4
    with factory() as db:
        rows = list(db.scalars(select(CardSnapshot)).all())
        assert len(rows) == 4
        keys = [row.field_sources["_collection"]["key"] for row in rows]
        assert len(keys) == len(set(keys))
    _local(faults)


def test_worker_restart_retries_the_job_and_keeps_one_snapshot(factory, faults, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    def start(_headless: bool):
        calls["n"] += 1
        driver = _chrome(faults["binary"], tmp_path / f"worker-{calls['n']}")
        if calls["n"] == 1:
            def crash() -> None:
                raise WebDriverException("tab crashed")

            return _Gate(driver, after=2, action=crash)
        return driver

    monkeypatch.setattr("parser_service._start_chrome", start)
    payload = {
        "source_mode": "browser_2gis",
        "city": "Алматы",
        "category": "Салон красоты",
        "platforms": ["2gis"],
        "limit": 3,
        "dedup_key": "e1022-restart",
        "out": "",
    }
    with factory() as db, db.begin():
        enqueue(db, "admin.scout_batch", payload, queue="collector", dedup_key="e1022-restart")
    registered = handlers(factory)
    run_one(factory, "collector", registered, notifier=lambda _message: None)
    with factory() as db:
        job = db.scalar(select(Job).where(Job.dedup_key == "e1022-restart"))
        assert job is not None
        assert job.status == "failed"
        assert job.attempts == 1
        job_id = job.id
    with factory() as db, db.begin():
        job = db.get(Job, job_id)
        assert job is not None
        job.run_after = now() - timedelta(seconds=1)
    run_one(factory, "collector", registered, notifier=lambda _message: None)
    with factory() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "done"
        assert job.attempts == 2
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "completed"
        assert batch.progress["metrics"]["retries"] == 1
        assert batch.progress["metrics"]["browser_restarts"] >= 1
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 3
        rows = list(db.scalars(select(CardSnapshot)).all())
        keys = [row.field_sources["_collection"]["key"] for row in rows]
        assert len(keys) == len(set(keys))
    _local(faults)


def test_admin_batch_exposes_stored_metrics(factory, faults, tmp_path: Path) -> None:
    batch_id = _run(factory, tmp_path / "metrics", faults, category="Салон красоты", limit=2)
    with TestClient(app) as client:
        body = client.get(f"/api/v1/admin/scout/batches/{batch_id}", headers=HEADERS)
    assert body.status_code == 200
    payload = body.json()
    metrics = payload["metrics"]
    assert metrics["companies_processed"] == 2
    assert metrics["companies_committed"] == 2
    assert metrics["snapshots_written"] == 2
    assert metrics["failed_records"] == 0
    assert isinstance(metrics["average_persistence_ms"], (int, float))
    assert metrics["browser_restarts"] == 0
    assert metrics["retries"] == 0
    assert "companies_processed" in metrics
    assert payload["card_states"]["saved"] == 2
    assert payload["card_states"]["retry_wait"] == 0
    assert payload["card_states"]["rejected"] == 0
    _local(faults)
