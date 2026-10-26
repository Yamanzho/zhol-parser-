"""Collection result, mode labels, job list and run timings.

The database tests use the isolated test database. No browser and no platform site is opened.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout import run_timing
from zhol.agents.scout.collection_outcome import SEARCH_STOP_TEXTS, collection_mode, collection_outcome, stop_reason_code
from zhol.agents.scout.run_timing import PersistTimer, RunClock, begin_timings, merge_timings, public_timings
from zhol.api.admin_jobs import JOB_TYPE
from zhol.api.main import app
from zhol.db.models import CollectionBatch, CollectionBatchItem, Job
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
WEB = ROOT / "web" / "src"


def _search():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import search

    return search


def test_stop_texts_match_the_scraping_engine() -> None:
    search = _search()
    assert SEARCH_STOP_TEXTS == {
        search.STOP_LIMIT: "limit",
        search.STOP_END: "confirmed_end",
        search.STOP_REPEAT: "repeat",
        search.STOP_EMPTY: "stall",
        search.STOP_PAGE_CAP: "page_cap",
        search.STOP_INTERRUPTED: "stopped",
    }
    assert stop_reason_code("limit") == "limit"
    assert stop_reason_code("Не удалось открыть страницу выдачи 3: timeout") == "page_error"
    assert stop_reason_code("что-то другое") == "unconfirmed"
    assert stop_reason_code(None) is None


def test_reaching_the_limit_is_not_a_warning() -> None:
    stored = {"stop_reason": _search().STOP_LIMIT, "link_collection_complete": False, "metrics": {"failed_records": 0}}
    result = collection_outcome("completed", stored, collected=10, limit=10)
    assert result["code"] == "limit_reached"
    assert result["label"] == "Достигнут лимит"
    assert result["warnings"] == []
    assert result["limit_reached"] is True
    assert result["search_exhausted"] is False
    assert "могла продолжаться" in result["note"]


def test_confirmed_end_is_full_completion() -> None:
    stored = {"stop_reason": _search().STOP_END, "link_collection_complete": True}
    result = collection_outcome("completed", stored, collected=3, limit=10)
    assert result["code"] == "completed"
    assert result["search_exhausted"] is True
    assert result["limit_reached"] is False
    assert result["warnings"] == []


def test_errors_and_an_open_feed_are_partial() -> None:
    search = _search()
    with_errors = collection_outcome("completed", {"stop_reason": search.STOP_LIMIT, "metrics": {"failed_records": 2}}, collected=10, limit=10)
    assert with_errors["code"] == "partial"
    assert with_errors["limit_reached"] is True
    assert with_errors["warnings"] == ["Не прочитано карточек: 2."]
    rejected = collection_outcome("completed", {"stop_reason": "confirmed_end"}, collected=4, limit=10, rejected=1)
    assert rejected["code"] == "partial"
    stalled = collection_outcome("completed", {"stop_reason": search.STOP_EMPTY}, collected=4, limit=10)
    assert stalled["code"] == "partial"
    assert stalled["search_exhausted"] is False
    assert stalled["warnings"] == ["Три страницы подряд без новых карточек, конец выдачи не подтверждён."]


def test_paused_failed_and_blocked_are_distinct() -> None:
    assert collection_outcome("paused", {}, collected=2, limit=10)["code"] == "paused"
    assert collection_outcome("running", {}, collected=2, limit=10)["code"] == "running"
    assert collection_outcome("awaiting_manual_verification", {}, collected=2, limit=10)["code"] == "blocked"
    captcha = {"interruption": {"classification": "captcha", "retryable": False}}
    assert collection_outcome("stopped", captcha, collected=2, limit=10)["code"] == "blocked"
    assert collection_outcome("stopped", {}, collected=2, limit=10)["code"] == "stopped"
    assert collection_outcome("interrupted", {}, collected=2, limit=10)["code"] == "interrupted"
    assert collection_outcome("failed", {}, collected=0, limit=10)["code"] == "failed"
    assert collection_outcome("failed", {}, collected=3, limit=10)["code"] == "partial"


def test_collection_mode_tells_live_from_local_and_saved() -> None:
    live = collection_mode("browser_2gis", {"browser_source": "live_2gis"})
    assert live["code"] == "live"
    assert "2gis.kz" in live["label"]
    assert "а не обход всего города" in live["scope_label"]
    assert collection_mode("browser_2gis", {"browser_source": "localhost"})["code"] == "local"
    assert collection_mode("browser_2gis", {"browser_source": "local_html"})["code"] == "local"
    assert collection_mode("browser_yandex", {"browser_source": "browser_yandex"})["code"] == "live"
    assert collection_mode("browser_google", {})["code"] == "unknown"
    assert collection_mode("fixture", {})["code"] == "fixture"
    assert collection_mode("manual", {})["code"] == "saved"


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_run_clock_splits_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Clock()
    monkeypatch.setattr(run_timing.time, "perf_counter", fake)
    clock = RunClock()
    started = fake()
    fake.advance(2.0)
    clock.browser_started(started)
    clock.search_began()
    pace = clock.paced(lambda: fake.advance(0.5))
    pause = clock.paused(lambda _reason: fake.advance(4.0))
    assert pace is not None and pause is not None
    pace()
    fake.advance(3.0)
    clock.progress({"status": "Очередь: 2."})
    for _ in range(2):
        clock.card_opened()
        pace()
        fake.advance(1.0)
        clock.persist.timed(lambda: fake.advance(0.25) or "new")
    pause("проверка")
    clock.search_ended()
    fake.advance(1.0)
    summary = clock.summary()
    assert summary["browser_start_ms"] == 2000
    assert summary["link_discovery_ms"] == 3500
    assert summary["card_pacing_ms"] == 1000
    assert summary["persistence_ms"] == 500
    assert summary["verification_wait_ms"] == 4000
    assert summary["card_read_ms"] == 2000
    assert summary["cards_opened"] == 2
    assert summary["cards_persisted"] == 2
    assert summary["run_ms"] == 14000


def test_commit_time_reaches_the_metrics_once() -> None:
    timer = PersistTimer()
    first: dict = {}
    timer.apply(first)
    timer.committed(12.0)
    assert first == {"persistence_ms_total": 0.0, "persistence_samples": 0}
    second: dict = {}
    timer.apply(second)
    timer.rolled_back()
    third: dict = {}
    timer.apply(third)
    timer.committed(8.0)
    assert third == {"persistence_ms_total": 12.0, "persistence_samples": 1}
    final: dict = {"persistence_ms_total": 12.0, "persistence_samples": 1}
    timer.apply(final)
    timer.drained()
    assert final == {"persistence_ms_total": 20.0, "persistence_samples": 2}
    assert timer.pending_samples == 0


def test_old_partial_persistence_numbers_are_not_presented_as_full() -> None:
    assert public_timings(None) == {"persistence_scope": "partial_legacy"}
    state = {"metrics": {"persistence_ms_total": 3.0, "persistence_samples": 3, "average_persistence_ms": 1.0, "companies_committed": 3}}
    begin_timings(state)
    assert state["metrics"] == {"companies_committed": 3}
    assert state["timings"]["persistence_scope"] == "commit"
    merged = merge_timings(state["timings"], {"run_ms": 1000.0, "persistence_ms": 40.0, "cards_persisted": 4, "cards_opened": 4, "card_read_ms": 400.0})
    merged = merge_timings(merged, {"run_ms": 500.0, "persistence_ms": 10.0, "cards_persisted": 1, "cards_opened": 1, "card_read_ms": 100.0})
    shown = public_timings(merged)
    assert shown["runs"] == 2
    assert shown["run_ms"] == 1500.0
    assert shown["average_persistence_ms"] == 10.0
    assert shown["average_card_read_ms"] == 100.0


@pytest.fixture
def factory(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _job(factory, *, status: str, progress: dict, items: list[str], limit: int, query: str) -> tuple[int, int]:
    started = now() - timedelta(seconds=90)
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city="Алматы",
            category=query,
            platforms=["2gis"],
            item_limit=limit,
            source_mode="browser_2gis",
            status=status,
            out_path="/tmp/zhol-ux-out",
            catalog_path="browser_2gis",
            progress=progress,
            created_at=started,
            finished_at=started + timedelta(seconds=60) if status != "running" else None,
        )
        session.add(batch)
        session.flush()
        for index, state in enumerate(items):
            session.add(
                CollectionBatchItem(
                    batch_id=batch.id,
                    id_2gis=f"7000000100{batch.id:03d}{index:04d}",
                    status=state,
                    attempts=1,
                    detail={"name": f"Компания {index}"},
                )
            )
        job = Job(
            type=JOB_TYPE,
            queue="collector",
            status="done" if status != "running" else "running",
            payload={
                "batch_id": batch.id,
                "platforms": ["2gis"],
                "source_mode": "browser_2gis",
                "city": "Алматы",
                "category": query,
                "limit": limit,
            },
            created_at=started - timedelta(seconds=5),
        )
        session.add(job)
        session.flush()
        return job.id, batch.id


def test_api_jobs_carry_one_result_for_the_list_and_the_panel(factory) -> None:
    search = _search()
    limit_job, limit_batch = _job(
        factory,
        status="completed",
        progress={"stop_reason": search.STOP_LIMIT, "link_collection_complete": False, "browser_source": "live_2gis", "metrics": {"failed_records": 0}},
        items=["completed"] * 3,
        limit=3,
        query="кофейни",
    )
    partial_job, _ = _job(
        factory,
        status="completed",
        progress={"stop_reason": search.STOP_END, "link_collection_complete": True, "browser_source": "live_2gis", "metrics": {"failed_records": 1}},
        items=["completed", "completed", "failed"],
        limit=10,
        query="барбершоп",
    )
    running_job, _ = _job(
        factory,
        status="running",
        progress={"browser_source": "localhost", "timings": {"persistence_scope": "commit", "runs": 0}},
        items=["completed"],
        limit=5,
        query="салон",
    )
    with TestClient(app) as client:
        listed = client.get("/api/v1/jobs?limit=20&offset=0", headers=HEADERS)
        panel = client.get(f"/api/v1/admin/scout/batches/{limit_batch}", headers=HEADERS)
    assert listed.status_code == 200
    jobs = {item["id"]: item for item in listed.json()["items"]}
    assert {limit_job, partial_job, running_job} <= set(jobs)
    limited = jobs[limit_job]
    assert limited["status"] == "completed"
    assert limited["result"]["code"] == "limit_reached"
    assert limited["result"]["search_exhausted"] is False
    assert limited["collection_mode"]["code"] == "live"
    assert limited["timings"] == {"persistence_scope": "partial_legacy"}
    assert limited["duration_seconds"] == 65.0
    assert limited["collected"] == 3
    assert limited["controls"] == {"pause": False, "resume": False, "stop": False}
    partial = jobs[partial_job]
    assert partial["status"] == "completed"
    assert partial["result"]["code"] == "partial"
    assert partial["result"]["warnings"] == ["Не прочитано карточек: 1."]
    running = jobs[running_job]
    assert running["result"]["code"] == "running"
    assert running["collection_mode"]["code"] == "local"
    assert running["controls"]["pause"] is True
    assert running["duration_seconds"] is None
    body = panel.json()
    assert body["display_status"] == limited["result"]["code"]
    assert body["result"] == limited["result"]
    assert body["stop_reason"] == "limit"
    assert body["duration_seconds"] == 60.0


def test_simple_collection_page_lists_jobs_and_keeps_controls() -> None:
    home = (WEB / "home.tsx").read_text(encoding="utf-8")
    assert "/api/v1/jobs?limit=20&offset=0" in home
    assert "Последние задания" in home
    assert "в том числе созданные через API" in home
    assert "/api/v1/jobs/${job.id}/${action}" in home
    for control in ("job.controls?.pause", "job.controls?.resume", "job.controls?.stop"):
        assert control in home
    assert "Собранные компании" in home
    assert "#/scout/${job.batch_id}" in home
    assert "job.result?.label" in home
    app_source = (WEB / "App.tsx").read_text(encoding="utf-8")
    assert '<Collection jobId={id} />' in app_source


def test_live_collection_pages_do_not_show_the_demo_or_city_crawl_labels() -> None:
    sources = {path.name: path.read_text(encoding="utf-8") for path in WEB.glob("*.tsx")}
    joined = "\n".join(sources.values())
    assert "Живой сбор города" not in joined
    collect = sources["collect.tsx"]
    assert "<Banner />" not in collect
    pages = sources["pages.tsx"]
    detail = pages[pages.index("export function ScoutDetail") :]
    detail = detail[: detail.index("\nexport function ", 1)]
    assert "<Banner />" not in detail
    app_source = sources["App.tsx"]
    assert '{meta?.demo && <div className="demo-banner">' in app_source
    ui = sources["ui.tsx"]
    assert "Сбор компаний с площадок к" in ui
    assert "Запись в базу, включая фиксацию" in ui
    assert "Прежний замер записи в базу был неполным" in ui
    for name in ("collect.tsx", "home.tsx", "ui.tsx", "pages.tsx"):
        assert "\u2013" not in sources[name] and "\u2014" not in sources[name]


def test_demo_jobs_are_labeled_demo() -> None:
    from zhol.demo import DEMO_MODE

    assert DEMO_MODE["code"] == "demo"
    assert "Демонстрация" in DEMO_MODE["label"]
