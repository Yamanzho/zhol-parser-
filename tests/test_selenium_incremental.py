"""Incremental Selenium commits against the local fixture server and real Chrome.

These tests do not open 2gis.kz, Yandex, or Google. Chrome is pointed at 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.collection_outcome import collection_outcome
from zhol.agents.scout.selenium_collector import run_browser_batch
from zhol.browser.discovery.types import RunControl
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "selenium-load" / "server.py"
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MISSING_ADDRESS = "70000001000011012"
EMPTY_NAME = "70000001000012005"
MALFORMED = "70000001000012006"
RESTRICTED = "70000001000012007"


def _load_fixture():
    spec = importlib.util.spec_from_file_location("selenium_load_server", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if not Path(CHROME).is_file() and not os.environ.get("ZHOL_CHROME_BINARY"):
        pytest.skip("Chrome is not installed")
    module = _load_fixture()
    paths: list[str] = []

    class Recording(module.Handler):
        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recording)
    threading.Thread(target=server.serve_forever, name="incremental-lab", daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    binary = os.environ.get("ZHOL_CHROME_BINARY", "").strip() or CHROME
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", origin)
    monkeypatch.setenv("ZHOL_CHROME_HEADLESS", "1")
    monkeypatch.setenv("ZHOL_CHROME_BINARY", binary)
    monkeypatch.setenv("ZHOL_CHROME_PROFILE", str(tmp_path / "chrome-profile"))
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    try:
        yield {"origin": origin, "paths": paths, "binary": binary, "root": tmp_path}
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


class _Gate:
    """Real Chrome session that can pause, stop, or die after N successful card reads."""

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
    profile = kwargs.pop("profile", folder / "chrome-profile")
    profile.mkdir(parents=True, exist_ok=True)
    os.environ["ZHOL_CHROME_PROFILE"] = str(profile)
    driver_factory = kwargs.pop("driver_factory", None)
    if driver_factory is None:
        driver_factory = lambda: _chrome(lab["binary"], profile)  # noqa: E731
    return run_browser_batch(
        factory,
        city="Алматы",
        category=kwargs.pop("category", "Салон красоты"),
        limit=kwargs.pop("limit"),
        out=folder / "out.txt",
        resume=kwargs.pop("resume", None),
        control=kwargs.pop("control", None),
        driver_factory=driver_factory,
        run_dir=folder / "run",
        delay_range=kwargs.pop("delay_range", (0.0, 0.0)),
    )


def _count(factory, model) -> int:
    with factory() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _progress(factory, batch_id: int) -> dict:
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        return dict(batch.progress or {}), batch.status


def _assert_local(lab: dict) -> None:
    relevant = [path for path in lab["paths"] if "/firm/" in path or "/search/" in path]
    assert relevant
    assert all("2gis." not in path and "yandex." not in path and "google." not in path for path in lab["paths"])


def test_ten_companies_are_committed_one_by_one(factory, lab, tmp_path: Path) -> None:
    batch_id = _run(factory, tmp_path, lab, limit=10)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert _count(factory, Company) == 10
    assert _count(factory, CardSnapshot) == 10
    assert progress["seen_ids"]
    assert len(progress["seen_ids"]) == 10
    metrics = progress["metrics"]
    assert metrics["companies_processed"] == 10
    assert metrics["companies_committed"] == 10
    assert metrics["snapshots_written"] == 10
    assert metrics["failed_records"] == 0
    assert metrics["average_persistence_ms"] >= 0
    assert metrics["browser_restarts"] == 0
    timings = progress["timings"]
    assert timings["persistence_scope"] == "commit"
    assert timings["runs"] == 1
    assert timings["cards_persisted"] == 10
    assert timings["cards_opened"] == 10
    assert metrics["persistence_samples"] == 10
    assert metrics["persistence_ms_total"] == pytest.approx(timings["persistence_ms"], abs=0.01)
    assert timings["persistence_ms"] > 0
    for key in ("run_ms", "browser_start_ms", "link_discovery_ms", "card_read_ms", "card_pacing_ms"):
        assert timings[key] >= 0
    phases = timings["link_discovery_ms"] + timings["card_read_ms"] + timings["card_pacing_ms"] + timings["persistence_ms"]
    assert phases <= timings["run_ms"] + 1
    outcome = collection_outcome(status, progress, collected=10, limit=10)
    assert outcome["code"] == "limit_reached"
    assert outcome["search_exhausted"] is False
    _assert_local(lab)


def test_fifty_companies_keep_one_snapshot_each(factory, lab, tmp_path: Path) -> None:
    batch_id = _run(factory, tmp_path, lab, limit=50)
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert _count(factory, Company) == 50
    assert _count(factory, CardSnapshot) == 50
    assert len(progress["seen_ids"]) == 50
    with factory() as db:
        company = db.scalar(select(Company).where(Company.id_2gis == MISSING_ADDRESS))
        assert company is not None
        assert company.name
        snapshot = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id == company.id))
        assert snapshot is not None
        assert snapshot.fields["address"] is None
        assert snapshot.field_sources["address"]["reason"] == "not_shown"
        assert "Тестовая" not in str(snapshot.fields["address"])
    _assert_local(lab)


def test_crash_after_two_commits_resumes_without_a_duplicate_snapshot(factory, lab, tmp_path: Path) -> None:
    def crash() -> None:
        raise WebDriverException("tab crashed")

    def crashing() -> _Gate:
        return _Gate(_chrome(lab["binary"], tmp_path / "crash-profile"), after=3, action=crash)

    with pytest.raises(WebDriverException, match="tab crashed"):
        _run(factory, tmp_path / "first", lab, limit=10, driver_factory=crashing, profile=tmp_path / "crash-profile")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 2
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 2
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"
        assert len((batch.progress or {}).get("seen_ids") or []) == 2
        batch_id = batch.id
    _run(factory, tmp_path / "second", lab, limit=10, resume=batch_id, profile=tmp_path / "resume-profile")
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert _count(factory, Company) == 10
    assert _count(factory, CardSnapshot) == 10
    assert len(progress["seen_ids"]) == 10
    assert progress["metrics"]["browser_restarts"] >= 1
    assert progress["metrics"]["snapshots_written"] == 10
    _assert_local(lab)


def test_pause_waits_for_the_card_and_resume_continues(factory, lab, tmp_path: Path) -> None:
    control = RunControl()

    def pausing() -> _Gate:
        return _Gate(_chrome(lab["binary"], tmp_path / "pause-profile"), after=2, action=control.pause)

    batch_id = _run(
        factory,
        tmp_path / "paused",
        lab,
        limit=6,
        control=control,
        driver_factory=pausing,
        profile=tmp_path / "pause-profile",
        delay_range=(2.0, 2.0),
    )
    progress, status = _progress(factory, batch_id)
    assert status == "paused"
    assert len(progress["seen_ids"]) == 2
    assert _count(factory, Company) == 2
    _run(
        factory,
        tmp_path / "resume",
        lab,
        limit=6,
        resume=batch_id,
        profile=tmp_path / "resume-profile",
        delay_range=(2.0, 2.0),
    )
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert _count(factory, Company) == 6
    assert _count(factory, CardSnapshot) == 6
    assert len(progress["seen_ids"]) == 6


def test_stop_is_terminal_and_keeps_committed_rows(factory, lab, tmp_path: Path) -> None:
    control = RunControl()
    opened: list[int] = []

    def stopping() -> _Gate:
        opened.append(1)
        return _Gate(_chrome(lab["binary"], tmp_path / "stop-profile"), after=2, action=control.stop)

    batch_id = _run(
        factory,
        tmp_path / "stopped",
        lab,
        limit=8,
        control=control,
        driver_factory=stopping,
        profile=tmp_path / "stop-profile",
        delay_range=(2.0, 2.0),
    )
    _, status = _progress(factory, batch_id)
    assert status == "stopped"
    assert _count(factory, Company) == 2
    assert _count(factory, CardSnapshot) == 2
    again = _run(factory, tmp_path / "ignored", lab, limit=8, resume=batch_id, profile=tmp_path / "ignored-profile")
    assert again == batch_id
    assert _count(factory, Company) == 2
    assert opened == [1]


def test_recollection_adds_a_historical_snapshot(factory, lab, tmp_path: Path) -> None:
    first = _run(factory, tmp_path / "first", lab, limit=4, profile=tmp_path / "first-profile")
    progress, _status = _progress(factory, first)
    original = list(progress["seen_ids"])
    assert len(original) == 4
    second = _run(factory, tmp_path / "second", lab, limit=4, profile=tmp_path / "second-profile")
    assert first != second
    # Existing companies spend this batch's limit, so the same four cards are opened again.
    # Their earlier snapshots stay, and a fifth company is not created.
    assert _count(factory, Company) == 4
    assert _count(factory, CardSnapshot) == 8
    with factory() as db:
        rows = list(db.scalars(select(CardSnapshot)).all())
        keys = [row.field_sources["_collection"]["key"] for row in rows]
        assert len(keys) == len(set(keys))
        for firm_id in original:
            company = db.scalar(select(Company).where(Company.id_2gis == firm_id))
            assert company is not None
            history = list(db.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company.id)).all())
            assert len(history) == 2
            assert {row.field_sources["_collection"]["key"] for row in history} == {
                f"browser_2gis:{first}:{firm_id}",
                f"browser_2gis:{second}:{firm_id}",
            }


def test_missing_address_malformed_html_and_access_block(factory, lab, tmp_path: Path) -> None:
    batch_id = _run(
        factory,
        tmp_path / "fields",
        lab,
        category="Проверка полей",
        limit=6,
        profile=tmp_path / "fields-profile",
    )
    _, status = _progress(factory, batch_id)
    assert status == "completed"
    with factory() as db:
        empty = db.scalar(select(Company).where(Company.id_2gis == "70000001000012002"))
        assert empty is not None
        assert empty.name == "Салон Пустой Адрес"
        snapshot = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id == empty.id))
        assert snapshot is not None
        assert snapshot.fields["address"] is None
        assert snapshot.field_sources["address"]["reason"] == "not_shown"
        assert db.scalar(select(Company).where(Company.id_2gis == EMPTY_NAME)) is None
        assert db.scalar(select(Company).where(Company.id_2gis == MALFORMED)) is None
        failed = list(
            db.scalars(
                select(CollectionBatchItem).where(CollectionBatchItem.status == "failed")
            ).all()
        )
        reasons = {item.id_2gis: item.detail.get("reason", "") for item in failed}
        assert EMPTY_NAME in reasons
        assert "назван" in reasons[EMPTY_NAME]
        assert MALFORMED in reasons
        assert "идентификатор" in reasons[MALFORMED] or "назван" in reasons[MALFORMED]
    access_id = _run(
        factory,
        tmp_path / "access",
        lab,
        category="Проверка доступа",
        limit=1,
        profile=tmp_path / "access-profile",
    )
    with factory() as db:
        assert db.scalar(select(Company).where(Company.id_2gis == RESTRICTED)) is None
        item = db.scalar(
            select(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == access_id,
                CollectionBatchItem.id_2gis == RESTRICTED,
            )
        )
        assert item is not None
        assert item.status == "failed"
        assert "ограничен" in item.detail.get("reason", "")


def test_two_browsers_on_one_batch_do_not_duplicate_rows(factory, lab, tmp_path: Path) -> None:
    started = threading.Event()
    holder: dict[str, int] = {}
    errors: list[BaseException] = []

    def first() -> None:
        try:
            def opened(batch_id: int) -> None:
                holder["id"] = batch_id
                started.set()

            run_browser_batch(
                factory,
                city="Алматы",
                category="Салон красоты",
                limit=4,
                out=tmp_path / "a.txt",
                on_open=opened,
                driver_factory=lambda: _chrome(lab["binary"], tmp_path / "profile-a"),
                run_dir=tmp_path / "run-a",
                delay_range=(0.0, 0.0),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            started.set()

    def second() -> None:
        if not started.wait(60):
            errors.append(TimeoutError("batch did not open"))
            return
        batch_id = holder.get("id")
        if batch_id is None:
            return
        try:
            run_browser_batch(
                factory,
                city="Алматы",
                category="Салон красоты",
                limit=4,
                out=tmp_path / "b.txt",
                resume=batch_id,
                driver_factory=lambda: _chrome(lab["binary"], tmp_path / "profile-b"),
                run_dir=tmp_path / "run-b",
                delay_range=(0.0, 0.0),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    left = threading.Thread(target=first)
    right = threading.Thread(target=second)
    left.start()
    right.start()
    left.join()
    right.join()
    assert errors == []
    assert _count(factory, Company) == 4
    assert _count(factory, CardSnapshot) == 4
    with factory() as db:
        items = list(db.scalars(select(CollectionBatchItem).where(CollectionBatchItem.status == "completed")).all())
        assert len(items) == 4
        assert len({item.id_2gis for item in items}) == 4


def _seed_existing_salons(factory) -> str:
    """Fifty salon ids that the local fixture already serves, plus one stored snapshot."""
    first = "70000001000011001"
    with factory() as db, db.begin():
        for offset in range(1, 51):
            firm_id = f"700000010000{11000 + offset:05d}"
            company = Company(
                id_2gis=firm_id,
                name="Салон Сирень" if offset <= 4 else f"Салон Лаборатория {offset:02d}",
                city="Алматы",
                niche="Салон красоты",
                source="manual",
            )
            db.add(company)
            db.flush()
            if offset == 1:
                db.add(
                    CardSnapshot(
                        company_id=company.id,
                        platform="2gis",
                        match_status="found",
                        url=f"https://2gis.kz/almaty/firm/{firm_id}",
                        fields={"name": company.name, "rating": 4.2},
                        field_sources={"name": {"url": "stored", "seen_at": "2026-09-30T00:00:00+05:00"}},
                        purpose="prospect",
                        collected_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
                        collected_by="scout",
                        status="verified",
                    )
                )
    return first


def _firm_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if "/firm/" in path]


def _profile_pids(profile: Path) -> set[int]:
    from parser_service import _process_rows, _uses_profile

    return {pid for pid, _ppid, command in _process_rows() if _uses_profile(command, profile)}


def test_limit_counts_existing_companies_and_repeat_stays_inside_it(factory, lab, tmp_path: Path) -> None:
    kept = _seed_existing_salons(factory)
    before = len(lab["paths"])
    started = time.monotonic()
    first = _run(factory, tmp_path / "first", lab, limit=10, profile=tmp_path / "first-profile")
    progress, status = _progress(factory, first)
    assert status == "completed"
    assert time.monotonic() - started < 90
    with factory() as db:
        batch = db.get(CollectionBatch, first)
        assert batch is not None
        assert batch.item_limit == 10
    assert len(progress["seen_ids"]) == 10
    assert progress["metrics"]["companies_processed"] == 10
    assert progress["metrics"]["snapshots_written"] == 10
    assert _count(factory, Company) == 50
    assert _count(factory, CardSnapshot) == 11
    opened = _firm_paths(lab["paths"][before:])
    assert len(opened) == 10
    assert len(set(opened)) == 10
    assert not any("/page/" in path for path in lab["paths"][before:])
    with factory() as db:
        company = db.scalar(select(Company).where(Company.id_2gis == kept))
        assert company is not None
        history = list(db.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company.id)).all())
        assert len(history) == 2
        assert any(row.status == "verified" and row.fields.get("rating") == 4.2 for row in history)
    mark = len(lab["paths"])
    started = time.monotonic()
    second = _run(
        factory,
        tmp_path / "second",
        lab,
        limit=10,
        profile=tmp_path / "second-profile",
        delay_range=(2.0, 2.0),
    )
    assert time.monotonic() - started < 90
    progress, status = _progress(factory, second)
    assert first != second
    assert status == "completed"
    assert len(progress["seen_ids"]) == 10
    assert _count(factory, Company) == 50
    assert _count(factory, CardSnapshot) == 21
    again = _firm_paths(lab["paths"][mark:])
    assert len(again) == 10
    with factory() as db:
        assert db.scalar(select(func.count(func.distinct(Company.id_2gis)))) == 50
        company = db.scalar(select(Company).where(Company.id_2gis == kept))
        assert company is not None
        history = list(db.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company.id)).all())
        assert len(history) == 3
        assert any(row.status == "verified" and row.fields.get("rating") == 4.2 for row in history)
    _assert_local(lab)


def test_resume_keeps_the_remaining_limit_for_existing_companies(factory, lab, tmp_path: Path) -> None:
    _seed_existing_salons(factory)
    control = RunControl()

    def pausing() -> _Gate:
        return _Gate(_chrome(lab["binary"], tmp_path / "pause-existing"), after=3, action=control.pause)

    batch_id = _run(
        factory,
        tmp_path / "paused",
        lab,
        limit=10,
        control=control,
        driver_factory=pausing,
        profile=tmp_path / "pause-existing",
        delay_range=(2.0, 2.0),
    )
    progress, status = _progress(factory, batch_id)
    assert status == "paused"
    assert len(progress["seen_ids"]) == 3
    assert _count(factory, CardSnapshot) == 4
    _run(
        factory,
        tmp_path / "resume",
        lab,
        limit=10,
        resume=batch_id,
        profile=tmp_path / "resume-existing",
        delay_range=(2.0, 2.0),
    )
    progress, status = _progress(factory, batch_id)
    assert status == "completed"
    assert len(progress["seen_ids"]) == 10
    assert _count(factory, Company) == 50
    assert _count(factory, CardSnapshot) == 11
    _assert_local(lab)


def test_forced_browser_failure_cleans_up_and_the_next_start_works(lab, tmp_path: Path, monkeypatch) -> None:
    from parser_service import _process_rows, _start_chrome, _uses_profile, stop_chrome

    profile = tmp_path / "recovery-profile"
    monkeypatch.setenv("ZHOL_CHROME_PROFILE", str(profile))
    monkeypatch.setenv("ZHOL_CHROME_HEADLESS", "1")
    monkeypatch.setenv("ZHOL_CHROME_BINARY", lab["binary"])
    monkeypatch.setenv("ZHOL_BROWSER_BASE_URL", lab["origin"])

    def pids() -> set[int]:
        return {pid for pid, _ppid, command in _process_rows() if _uses_profile(command, profile)}

    first = _start_chrome(True)
    assert pids()
    os.kill(first.service.process.pid, signal.SIGKILL)
    started = time.monotonic()
    stop_chrome(first)
    assert time.monotonic() - started < 15
    assert pids() == set()

    held = _start_chrome(True)
    assert pids()
    started = time.monotonic()
    replacement = _start_chrome(True)
    assert time.monotonic() - started < 40
    stop_chrome(replacement)
    stop_chrome(held)
    assert pids() == set()
