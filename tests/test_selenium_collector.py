"""Existing Selenium collector, exercised with fixtures and a mocked Chrome session.

These tests do not open 2gis.kz and do not start Chrome.
"""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from selenium.common.exceptions import WebDriverException
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from card import CARD_DOM_JS, build_record
from parser_service import resolve_city

from zhol.agents.scout.browser_2gis import (
    BLOCKER,
    GRANT_ENV,
    GRANT_VALUE,
    BrowserCollectionRefused,
    build_browser_plans,
)
from zhol.agents.scout.selenium_collector import normalize_firm, run_browser_batch
from zhol.api.admin_jobs import handle_scout_batch
from zhol.api.main import app
from zhol.browser.discovery.types import RunControl
from zhol.db.models import Audit, CardSnapshot, CollectionBatch, CollectionBatchItem, Company, Job
from zhol.jobs.queue import StaleJob, recover_stale
from zhol.timeutil import now

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "selenium"
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
NORTH = "70000001000000901"
SOUTH = "70000001000000902"


class _Element:
    def is_displayed(self) -> bool:
        return True

    def click(self) -> None:
        return None


class ScriptedDriver:
    """Mock Chrome session. Responses come from the local HTML fixtures."""

    def __init__(self, crash: bool = False, on_card=None) -> None:
        self.current_url = ""
        self.gets: list[str] = []
        self.closed = False
        self.crash = crash
        self.on_card = on_card
        self.cards = 0

    def get(self, url: str) -> None:
        self.gets.append(url)
        self.current_url = url

    def set_page_load_timeout(self, _seconds: float) -> None:
        return None

    def quit(self) -> None:
        self.closed = True

    def find_element(self, _by: str, _value: str) -> _Element:
        return _Element()

    def find_elements(self, _by: str, _value: str) -> list:
        return []

    def execute_script(self, script: str, *_args):
        text = script or ""
        if self.crash and "return window.gisExtract" in text:
            raise WebDriverException("tab crashed")
        if "return window.gisCollectSearch" in text:
            return _search_payload()
        if "return document.readyState" in text:
            return "complete"
        if "return (function(branchId)" in text:
            return {
                "signal": "h1",
                "readyState": "complete",
                "url": self.current_url,
                "title": "card",
                "h1": True,
                "visibleH1": True,
                "card": True,
                "text": "North Beauty",
            }
        if "querySelectorAll" in text and "h1" in text and text.strip().startswith("return"):
            return True
        if "return window.gisMarkCard" in text:
            return True
        if "return window.gisShowPhoneCount" in text:
            return 0
        if "return window.gisExtract" in text:
            self.cards += 1
            if self.on_card is not None:
                self.on_card(self.cards)
            return _card_payload(self.current_url)
        if "return !!" in text:
            return True
        return True


def _search_payload() -> dict:
    html = (FIXTURE / "search.html").read_text(encoding="utf-8")
    items = [
        {"branchId": firm_id, "listName": name.strip()}
        for firm_id, name in re.findall(r'href="/almaty/firm/(\d+)"[^>]*>([^<]+)', html)
    ]
    return {"items": items, "pageNumbers": [], "heading": "Салон красоты"}


def _card_payload(url: str) -> dict:
    firm_id = re.search(r"/firm/(\d+)", url).group(1)
    html = (FIXTURE / f"{firm_id}.html").read_text(encoding="utf-8")
    name = re.search(r"<h1><span>([^<]+)</span></h1>", html).group(1)
    address = re.search(r'href="/almaty/geo/\d+">([^<]+)', html).group(1)
    locality = re.search(r'data-locality="1">([^<]+)', html).group(1)
    phone = re.search(r'href="tel:[^"]+">([^<]+)', html).group(1)
    links = [
        {"href": href, "aria": aria, "text": aria, "inAd": False}
        for href, aria in re.findall(r'href="(https:[^"]+)" aria-label="([^"]+)"', html)
    ]
    rubric = re.search(r'href="[^"]*rubricId/\d+"[^>]*>\s*(?:<span>)?([^<]+)', html)
    return {
        "name": name,
        "category": rubric.group(1).strip() if rubric else None,
        "address": address,
        "cityLine": locality,
        "phones": [{"text": phone}],
        "links": links,
        "firmIds": [firm_id],
        "showPhoneCount": 0,
    }


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("DGIS_CATALOG_KEY", raising=False)
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    monkeypatch.delenv(GRANT_ENV, raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GRANT_ENV, GRANT_VALUE)


def _run(factory, tmp_path: Path, driver: ScriptedDriver, **kwargs) -> int:
    control = kwargs.pop("control", None) or RunControl()
    return run_browser_batch(
        factory,
        city="Алматы",
        category="Салон красоты",
        limit=kwargs.pop("limit", 2),
        out=tmp_path / "batch-out.txt",
        control=control,
        driver_factory=lambda: driver,
        run_dir=tmp_path / "run",
        delay_range=(0.0, 0.0),
        **kwargs,
    )


def test_known_cities_keep_their_own_slug() -> None:
    assert resolve_city() == ("Алматы", "almaty")
    assert resolve_city("Алматы") == ("Алматы", "almaty")
    assert resolve_city("Астана") == ("Астана", "astana")
    assert resolve_city("нур-султан") == ("Астана", "astana")
    with pytest.raises(ValueError, match="не переключается на Алматы"):
        resolve_city("Сарань")
    with pytest.raises(ValueError, match="не переключается на Алматы"):
        resolve_city("Алматы", "astana")


def test_normalize_firm_keeps_only_fields_the_card_showed() -> None:
    raw = _card_payload(f"https://2gis.kz/almaty/firm/{NORTH}")
    record = build_record(raw, f"https://2gis.kz/almaty/firm/{NORTH}", NORTH)
    record["collected_at"] = "2026-10-15T01:00:00+05:00"
    firm = normalize_firm(record, city="Алматы", query="Салон красоты", slug="almaty")
    assert firm is not None
    assert firm.firm_id == NORTH
    assert firm.phones == ("77010000901",)
    assert firm.whatsapp == "77010000901"
    assert firm.instagram == "north.beauty"
    assert firm.address == "улица Абая, 10"
    assert firm.city == "Алматы"
    assert firm.category == "Парикмахерские услуги"
    assert firm.category_checked is True
    assert firm.query == "Салон красоты"
    assert not hasattr(firm, "rating")
    assert "rubricId/" in CARD_DOM_JS
    source = (ROOT / "zhol" / "agents" / "scout" / "selenium_collector.py").read_text(encoding="utf-8")
    assert "excel_writer" not in source
    policy = (ROOT / "zhol" / "agents" / "scout" / "browser_2gis.py").read_text(encoding="utf-8")
    assert "webdriver" not in policy
    assert "selenium" not in policy


def test_plans_stay_closed_without_the_written_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRANT_ENV, raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_HTML_ROOT", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_ENGINE", raising=False)
    monkeypatch.delenv("ZHOL_CHROME_CONTAINER", raising=False)
    with pytest.raises(BrowserCollectionRefused, match="выключен"):
        build_browser_plans(
            city="Алматы", categories=["Салон красоты"], queries=[], platforms=["2gis"], limit=20
        )
    _grant(monkeypatch)
    with pytest.raises(Exception, match="не переключается на Алматы"):
        build_browser_plans(
            city="Сарань", categories=[], queries=["Косметология"], platforms=["2gis"], limit=20
        )


def test_mocked_session_extracts_and_closes(factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    driver = ScriptedDriver()
    batch_id = _run(factory, tmp_path, driver, limit=1)
    assert driver.closed is True
    assert driver.gets
    assert all(url.startswith("https://2gis.kz/almaty/") for url in driver.gets)
    with factory() as db:
        company = db.scalar(select(Company).where(Company.id_2gis == NORTH))
        assert company is not None
        assert company.name == "North Beauty"
        assert company.city == "Алматы"
        assert company.niche == "Салон красоты"
        assert company.whatsapp == "77010000901"
        assert company.instagram == "north.beauty"
        assert company.source == "browser_2gis"
        snapshot = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id == company.id))
        assert snapshot is not None
        assert snapshot.status == "collected"
        assert snapshot.url == f"https://2gis.kz/almaty/firm/{NORTH}"
        assert snapshot.fields["phones"] == ["77010000901"]
        assert snapshot.fields["rating"] is None
        assert snapshot.fields["prices"] is None
        assert snapshot.fields["photos_count"] is None
        assert snapshot.fields["category"] == "Парикмахерские услуги"
        assert snapshot.field_sources["category"]["checked"] is True
        assert snapshot.field_sources["_collection"]["search_query"] == "Салон красоты"
        assert snapshot.field_sources["name"]["source"] == "2gis.kz"
        assert snapshot.field_sources["name"]["url"].endswith(NORTH)
        assert snapshot.field_sources["_collection"]["kind"] == "selenium_public_card"
        assert snapshot.field_sources["rating"]["reason"] == "not_shown"
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.source_mode == "browser_2gis"
        assert NORTH in (batch.progress or {}).get("seen_ids", [])
        assert db.scalar(select(func.count()).select_from(Company)) == 1


def test_resume_skips_the_saved_branch(factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    control = RunControl()
    first = ScriptedDriver(on_card=lambda _count: control.pause())
    batch_id = _run(factory, tmp_path, first, limit=2, control=control)
    with factory() as db:
        assert db.get(CollectionBatch, batch_id).status == "paused"
        assert db.scalar(select(func.count()).select_from(Company)) == 1
    second = ScriptedDriver()
    _run(factory, tmp_path / "second", second, limit=2, resume=batch_id)
    assert second.closed is True
    assert all(NORTH not in url for url in second.gets)
    assert any(SOUTH in url for url in second.gets)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 2
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 2
        names = set(db.scalars(select(Company.name)).all())
        assert names == {"North Beauty", "South Beauty"}


def test_stop_before_start_does_not_open_a_session(factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    opened: list[ScriptedDriver] = []

    def factory_driver() -> ScriptedDriver:
        driver = ScriptedDriver()
        opened.append(driver)
        return driver

    control = RunControl()
    control.stop()
    batch_id = run_browser_batch(
        factory,
        city="Алматы",
        category="Салон красоты",
        limit=2,
        out=tmp_path / "batch-out.txt",
        control=control,
        driver_factory=factory_driver,
        run_dir=tmp_path / "run",
        delay_range=(0.0, 0.0),
    )
    assert opened == []
    with factory() as db:
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "stopped"
        assert db.scalar(select(func.count()).select_from(Company)) == 0


def test_missing_address_is_stored_as_null() -> None:
    firm = normalize_firm(
        {
            "branch_id": NORTH,
            "name": "North Beauty",
            "address": None,
            "address_seen": False,
            "phones": [],
            "whatsapp": [],
            "instagram": [],
            "diagnostics": {"errors": []},
            "two_gis_url": f"https://2gis.kz/almaty/firm/{NORTH}",
        },
        city="Алматы",
        query="Салон красоты",
    )
    assert firm is not None
    assert firm.address is None
    assert firm.address_reason == "not_shown"
    blank = normalize_firm(
        {
            "branch_id": NORTH,
            "name": "North Beauty",
            "address": "   ",
            "address_seen": True,
            "diagnostics": {"errors": []},
            "two_gis_url": f"https://2gis.kz/almaty/firm/{NORTH}",
        },
        city="Алматы",
        query="Салон красоты",
    )
    assert blank is not None
    assert blank.address is None
    assert blank.address_reason == "empty"


def test_a_failed_card_transaction_rolls_back_without_moving_the_checkpoint(
    factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant(monkeypatch)
    from zhol.agents.scout import selenium_collector

    real = selenium_collector._snapshot_once

    def explode(session, batch, company, firm):
        if firm.firm_id == SOUTH:
            raise RuntimeError("stop mid-write")
        return real(session, batch, company, firm)

    monkeypatch.setattr(selenium_collector, "_snapshot_once", explode)
    with pytest.raises(RuntimeError, match="stop mid-write"):
        _run(factory, tmp_path, ScriptedDriver(), limit=2)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(Company.id_2gis)) == NORTH
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"
        assert (batch.progress or {}).get("seen_ids") == [NORTH]
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 1


def test_concurrent_commits_keep_one_snapshot(factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    import threading

    from zhol.agents.scout.selenium_collector import _commit_firm, _open

    record = build_record(_card_payload(f"https://2gis.kz/almaty/firm/{NORTH}"), f"https://2gis.kz/almaty/firm/{NORTH}", NORTH)
    firm = normalize_firm(record, city="Алматы", query="Салон красоты")
    assert firm is not None
    batch_id = _open(factory, city="Алматы", category="Салон красоты", limit=1, out=tmp_path / "out.txt")
    errors: list[BaseException] = []

    def work() -> None:
        try:
            _commit_firm(factory, batch_id, firm)
        except BaseException as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 1
        assert db.scalar(select(func.count()).select_from(CollectionBatchItem)) == 1
        progress = db.get(CollectionBatch, batch_id).progress or {}
        assert progress["seen_ids"] == [NORTH]
        assert progress["metrics"]["snapshots_written"] == 1
        assert progress["metrics"]["companies_committed"] == 1


def test_a_crashed_session_is_closed(factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    driver = ScriptedDriver(crash=True)
    with pytest.raises(WebDriverException, match="tab crashed"):
        _run(factory, tmp_path, driver, limit=1)
    assert driver.closed is True
    with factory() as db:
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.status == "interrupted"


def test_existing_company_snapshot_and_audit_are_preserved(
    factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant(monkeypatch)
    with factory() as db, db.begin():
        company = Company(
            id_2gis=NORTH,
            name="Kept Name",
            city="Алматы",
            niche="старая ниша",
            whatsapp="77000000000",
            source="manual",
        )
        db.add(company)
        db.flush()
        snapshot = CardSnapshot(
            company_id=company.id,
            platform="2gis",
            match_status="found",
            url=f"https://2gis.kz/almaty/firm/{NORTH}",
            fields={"name": "Kept Name", "rating": 4.2},
            field_sources={"name": {"url": "stored", "seen_at": "2026-10-01T00:00:00+05:00"}},
            purpose="prospect",
            collected_at=now() - timedelta(days=30),
            collected_by="scout",
            status="verified",
        )
        db.add(snapshot)
        db.flush()
        audit = Audit(
            company_id=company.id,
            snapshot_ids=[snapshot.id],
            score_total=40,
            score_groups={"base": 40},
            derived={},
            discrepancies={},
            gaps={},
            status="verified",
        )
        db.add(audit)
        db.flush()
        snapshot_id = snapshot.id
        audit_id = audit.id
    driver = ScriptedDriver()
    _run(factory, tmp_path, driver, limit=2)
    with factory() as db:
        kept = db.scalar(select(Company).where(Company.id_2gis == NORTH))
        assert kept is not None
        assert kept.name == "Kept Name"
        assert kept.niche == "старая ниша"
        assert kept.whatsapp == "77000000000"
        old = db.get(CardSnapshot, snapshot_id)
        assert old is not None
        assert old.status == "verified"
        assert old.fields["rating"] == 4.2
        stored_audit = db.get(Audit, audit_id)
        assert stored_audit is not None
        assert stored_audit.snapshot_ids == [snapshot_id]
        assert stored_audit.score_total == 40
        assert db.scalar(select(func.count()).select_from(Audit)) == 1
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 3
        kept_rows = list(db.scalars(select(CardSnapshot).where(CardSnapshot.company_id == kept.id)).all())
        assert len(kept_rows) == 2
        assert any(row.id != snapshot_id and row.fields["rating"] is None for row in kept_rows)
        fresh = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id != kept.id))
        assert fresh is not None
        assert fresh.fields["rating"] is None
        linked = db.scalar(select(CollectionBatchItem).where(CollectionBatchItem.id_2gis == NORTH))
        assert linked is not None
        assert linked.detail["reason"] == "already_present"


def test_screenshot_is_stored_and_a_hidden_phone_is_not_zero(
    factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant(monkeypatch)

    class Shot(ScriptedDriver):
        def save_screenshot(self, path: str) -> bool:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x89PNG\r\n\x1a\n")
            return True

    _run(factory, tmp_path, Shot(), limit=1)
    with factory() as db:
        snapshot = db.scalar(select(CardSnapshot))
        assert snapshot is not None
        assert snapshot.screenshot_path
        assert Path(snapshot.screenshot_path).is_file()
        assert snapshot.fields["rating"] is None
        assert snapshot.field_sources["rating"]["reason"] == "not_shown"
        assert snapshot.fields["phones"] == ["77010000901"]
        assert snapshot.field_sources["phones"]["display"] == ["+7 701 000 09 01"]
    hidden = normalize_firm(
        {
            "branch_id": NORTH,
            "name": "North Beauty",
            "phones": ["8 701…"],
            "diagnostics": {"errors": [{"field": "phones", "message": "Телефон остался скрытым"}]},
            "two_gis_url": f"https://2gis.kz/almaty/firm/{NORTH}",
        },
        city="Алматы",
        query="Салон красоты",
    )
    assert hidden is not None
    assert hidden.phones == ()
    assert hidden.phones_reason == "not_shown"
    blank = normalize_firm(
        {
            "branch_id": NORTH,
            "name": "North Beauty",
            "phones": [],
            "whatsapp": [],
            "instagram": [],
            "diagnostics": {"errors": []},
            "two_gis_url": f"https://2gis.kz/almaty/firm/{NORTH}",
        },
        city="Алматы",
        query="Салон красоты",
    )
    assert blank is not None
    assert blank.phones_reason == "empty"
    assert blank.whatsapp_reason == "empty"


def test_worker_refuses_browser_collection_without_opening_chrome(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_chrome(_headless: bool):
        raise AssertionError("chrome started")

    monkeypatch.setattr("parser_service._start_chrome", refuse_chrome)
    payload = {
        "source_mode": "browser_2gis",
        "city": "Алматы",
        "category": "Салон красоты",
        "platforms": ["2gis"],
        "limit": 20,
        "dedup_key": "browser-refused",
        "out": "",
    }
    with factory() as db, db.begin():
        db.add(
            Job(
                type="admin.scout_batch",
                queue="main",
                status="running",
                attempts=1,
                payload=payload,
                dedup_key="browser-refused",
            )
        )
    with pytest.raises(BrowserCollectionRefused):
        handle_scout_batch(factory, payload)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionBatch)) == 0


def test_authorized_worker_uses_the_existing_starter(
    factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant(monkeypatch)
    driver = ScriptedDriver()
    monkeypatch.setattr("parser_service._start_chrome", lambda _headless: driver)
    payload = {
        "source_mode": "browser_2gis",
        "city": "Алматы",
        "category": "Салон красоты",
        "platforms": ["2gis"],
        "limit": 1,
        "dedup_key": "browser-granted",
        "out": "",
    }
    with factory() as db, db.begin():
        db.add(
            Job(
                type="admin.scout_batch",
                queue="main",
                status="running",
                attempts=1,
                payload=payload,
                dedup_key="browser-granted",
                run_after=now() + timedelta(minutes=30),
            )
        )
    handle_scout_batch(factory, payload)
    assert driver.closed is True
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 1
        batch = db.scalar(select(CollectionBatch))
        assert batch is not None
        assert batch.source_mode == "browser_2gis"


def test_lost_lease_closes_the_session(factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grant(monkeypatch)
    driver = ScriptedDriver()

    def lost() -> None:
        raise StaleJob("lease expired")

    with pytest.raises(StaleJob):
        run_browser_batch(
            factory,
            city="Алматы",
            category="Салон красоты",
            limit=1,
            out=tmp_path / "batch-out.txt",
            driver_factory=lambda: driver,
            ensure_owner=lost,
            run_dir=tmp_path / "run",
            delay_range=(0.0, 0.0),
        )
    assert driver.closed is True


def test_stale_job_recovery_does_not_open_chrome(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_chrome(_headless: bool):
        raise AssertionError("chrome started")

    monkeypatch.setattr("parser_service._start_chrome", refuse_chrome)
    with factory() as db, db.begin():
        db.add(
            Job(
                type="admin.scout_batch",
                queue="main",
                status="running",
                attempts=1,
                payload={"source_mode": "browser_2gis", "dedup_key": "stale-browser"},
                dedup_key="stale-browser",
                run_after=now() - timedelta(minutes=5),
            )
        )
    with factory() as db, db.begin():
        recovered = recover_stale(db)
    assert recovered == []
    with factory() as db:
        job = db.scalar(select(Job).where(Job.dedup_key == "stale-browser"))
        assert job is not None
        assert job.status == "failed"
        assert "lease expired" in (job.last_error or "")


def test_api_rejects_browser_collection_until_the_grant(factory) -> None:
    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "categories": ["Салон красоты"],
                "platforms": ["2gis"],
                "limit": 20,
                "source_mode": "browser_2gis",
            },
        )
    assert denied.status_code == 400
    assert denied.json()["detail"] == BLOCKER
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_api_enqueues_when_the_grant_is_set_without_collecting(
    factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant(monkeypatch)

    def refuse_chrome(_headless: bool):
        raise AssertionError("chrome started")

    monkeypatch.setattr("parser_service._start_chrome", refuse_chrome)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "categories": ["Салон красоты"],
                "platforms": ["2gis"],
                "limit": 20,
                "source_mode": "browser_2gis",
            },
        )
        yandex = client.post(
            "/api/v1/admin/scout/collect",
            headers=HEADERS,
            json={
                "city": "Алматы",
                "queries": ["Косметология"],
                "platforms": ["yandex"],
                "limit": 20,
                "source_mode": "browser_2gis",
            },
        )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["source_mode"] == "browser_2gis"
    assert body["data_source"] == "selenium_public_cards"
    assert body["live_mass_discovery"] is False
    assert yandex.status_code == 400
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.scalar(select(func.count()).select_from(Company)) == 0
        job = db.scalar(select(Job))
        assert job is not None
        assert job.status == "queued"
        assert job.queue == "collector"
