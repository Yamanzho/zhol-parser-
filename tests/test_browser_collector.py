"""Saved-page browser collector. No live site and no Selenium."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.discover import run_discovery
from zhol.api.admin_jobs import handle_scout_batch
from zhol.browser.collector import SavedHtmlCollector, panel_browser_modes
from zhol.browser.discovery.session import LocalHtmlPage
from zhol.browser.discovery.types import DiscoveryRefused, RunControl
from zhol.db.models import CardSnapshot, Company, Job

ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "tests" / "fixtures" / "discovery" / "almaty-salon"
PAGES = SAVED / "pages"
SALON = "Салон красоты"
FIRMS = {
    "70000001000000901",
    "70000001000000902",
    "70000001000000903",
    "70000001000000904",
}
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"


def _collect(tmp_path: Path, **kwargs):
    options = dict(
        city="Алматы",
        category=SALON,
        platforms=("2gis",),
        source_mode="fixture",
        evidence_dir=tmp_path / "evidence",
    )
    options.update(kwargs)
    return SavedHtmlCollector(SAVED).collect(**options)


def test_saved_search_normalizes_firms_and_leaves_unseen_ratings_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_2GIS_BROWSER_GRANT", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_ENGINE", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_BASE_URL", raising=False)
    monkeypatch.delenv("ZHOL_BROWSER_HTML_ROOT", raising=False)
    monkeypatch.delenv("ZHOL_CHROME_CONTAINER", raising=False)
    monkeypatch.delenv("ZHOL_CHROME_BINARY", raising=False)
    result = _collect(tmp_path, query=SALON)
    found = {item.external_id: item for item in result.companies}
    assert set(found) == FIRMS
    north = found["70000001000000901"]
    assert north.source == "saved_pages"
    assert north.name == "North Beauty"
    assert north.city == "Алматы"
    assert north.address == "улица Абая, 10"
    assert north.category == SALON
    assert north.phone == "77010000901"
    assert north.whatsapp == "77010000901"
    assert north.instagram == "north.beauty"
    assert north.website == "https://north.example/salon"
    assert north.rating is None
    assert north.review_count is None
    assert north.collected_at.startswith("2026-10-11")
    assert north.source_url == "https://2gis.kz/almaty/firm/70000001000000901"
    assert result.processed == 4
    assert result.duplicates >= 1
    assert result.checkpoint["pages"] >= 2
    assert any(item == "2gis: robot_check" for item in result.errors)
    listed = panel_browser_modes()
    assert listed[0]["id"] == "browser_2gis"
    assert listed[0]["available"] is False


def test_rating_is_kept_only_when_the_page_shows_it(tmp_path: Path) -> None:
    root = tmp_path / "saved"
    page = root / "2gis"
    page.mkdir(parents=True)
    (page / "page-1.html").write_text(
        """<!DOCTYPE html><html><body>
        <section data-page="search" data-platform="2gis">
        <p data-field="seen_at">2026-10-15T12:00:00+05:00</p>
        <a data-candidate="1" href="https://2gis.kz/almaty/firm/70000001000000911">
        <span data-field="name">Rated Salon</span>
        <span data-field="address">улица Абая, 11</span>
        <span data-field="category">Салон красоты</span>
        <span data-field="phones">8 701 000 09 11</span>
        <span data-field="rating">4.8</span>
        <span data-field="reviews_count">12</span>
        </a>
        <a data-candidate="2" href="https://2gis.kz/almaty/firm/70000001000000912">
        <span data-field="name">Quiet Salon</span>
        <span data-field="address">улица Абая, 12</span>
        <span data-field="category">Салон красоты</span>
        </a>
        </section></body></html>""",
        encoding="utf-8",
    )
    (root / "manifest.yaml").write_text(
        yaml.safe_dump({"city": "Алматы", "category": SALON, "starts": {"2gis": "2gis/page-1.html"}}, allow_unicode=True),
        encoding="utf-8",
    )
    result = SavedHtmlCollector(root).collect(
        city="Алматы",
        category=SALON,
        platforms=("2gis",),
        source_mode="manual",
        evidence_dir=tmp_path / "evidence",
    )
    rated = next(item for item in result.companies if item.external_id == "70000001000000911")
    quiet = next(item for item in result.companies if item.external_id == "70000001000000912")
    assert rated.rating == 4.8
    assert rated.review_count == 12
    assert rated.phone == "77010000911"
    assert quiet.rating is None
    assert quiet.review_count is None
    assert quiet.phone is None
    assert quiet.source == "manual_browser"


def test_custom_query_does_not_return_another_saved_category(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryRefused, match="do not contain this query"):
        _collect(tmp_path, query="Косметология")
    assert not (tmp_path / "evidence").exists()


def test_pause_resume_and_stop_keep_one_row_per_firm(tmp_path: Path) -> None:
    paused = _collect(tmp_path, control=RunControl(pause_after_pages=1))
    assert paused.status == "paused"
    assert {item.external_id for item in paused.companies} == {"70000001000000901", "70000001000000902"}
    resumed = _collect(tmp_path / "more", resume_state=paused.checkpoint)
    assert {item.external_id for item in resumed.companies} == FIRMS
    assert len(resumed.companies) == len(FIRMS)
    stopped = _collect(tmp_path / "stop", control=RunControl(stop_after_pages=1))
    assert stopped.status == "stopped"
    assert stopped.checkpoint["pending"]["2gis"]


def test_live_request_stops_before_the_page_and_a_failed_session_closes(tmp_path: Path, monkeypatch) -> None:
    class Page:
        def set_content(self, html: str, wait_until: str | None = None) -> None:
            raise AssertionError("a page was loaded")

    with pytest.raises(PermissionError, match="not permitted"):
        _collect(tmp_path, source_mode="live", page=Page())

    closed = {"value": False}

    class Broken(LocalHtmlPage):
        def set_content(self, html: str, wait_until: str | None = None) -> None:
            raise RuntimeError("browser failed")

        def close(self) -> None:
            closed["value"] = True
            super().close()

    monkeypatch.setattr("zhol.browser.collector.LocalHtmlPage", Broken)
    with pytest.raises(RuntimeError, match="browser failed"):
        _collect(tmp_path / "broken")
    assert closed["value"] is True


def test_collector_module_does_not_launch_a_browser() -> None:
    source = (ROOT / "zhol" / "browser" / "collector.py").read_text(encoding="utf-8")
    for word in ("selenium", "webdriver", "urlopen", "sync_playwright", "playwright_stealth"):
        assert word not in source


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def test_worker_restart_resumes_the_checkpoint_without_a_duplicate(factory, tmp_path: Path) -> None:
    paused = run_discovery(
        factory,
        city="Алматы",
        category=SALON,
        platforms=("2gis",),
        limit=None,
        out=tmp_path / "paused.xlsx",
        source_mode="manual",
        root=SAVED,
        fixtures_dir=PAGES,
        control=RunControl(pause_after_pages=1),
    )
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Company)) == 2
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 0
    payload = {
        "source_mode": "manual",
        "city": "Алматы",
        "category": SALON,
        "platforms": ["2gis"],
        "catalog": "discovery/almaty-salon",
        "fixtures_dir": "discovery/almaty-salon/pages",
        "resume": paused.batch_id,
        "dedup_key": "browser-restart",
        "out": "",
    }
    with factory() as db, db.begin():
        db.add(Job(type="admin.scout_batch", queue="main", status="running", attempts=1, payload=payload, dedup_key="browser-restart"))
    handle_scout_batch(factory, payload)
    handle_scout_batch(factory, payload)
    with factory() as db:
        rows = db.scalars(select(Company)).all()
        assert {row.id_2gis for row in rows} == FIRMS
        assert len(rows) == 4
        assert {row.source for row in rows} == {"manual_browser"}
        assert db.scalar(select(func.count()).select_from(CardSnapshot)) == 4
