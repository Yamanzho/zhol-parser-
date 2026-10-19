"""E10.11 collection defects. Saved pages and mocks only. No live map site."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.discover import run_discovery
from zhol.agents.scout.normalize import phone_parts
from zhol.api.company_view import _indicator, _presence
from zhol.browser.collector import SavedHtmlCollector
from zhol.browser.discovery.engine import walk_saved
from zhol.browser.pace import collection_rate_seconds, wait_turn
from zhol.db.models import CollectionBatchItem, Company

ROOT = Path("tests/fixtures/discovery/almaty-salon")
SALON = "Салон красоты"
NORTH = "70000001000000901"
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _walk(tmp_path: Path, **kwargs):
    options = dict(
        city="Алматы",
        category=SALON,
        platforms=("2gis", "yandex", "google"),
        source="fixture",
        evidence_dir=tmp_path / "evidence",
    )
    options.update(kwargs)
    return walk_saved(ROOT, **options)


def test_limit_one_still_attaches_yandex_and_google(tmp_path: Path) -> None:
    result = _walk(tmp_path, limit=1)
    accepted = [branch for branch in result.branches if branch.firm_id]
    assert [branch.firm_id for branch in accepted] == [NORTH]
    assert set(accepted[0].platforms) == {"2gis", "yandex", "google"}
    unmatched = result.progress["unmatched"]
    assert any(item["reason"] == "limit" and item["platform"] == "2gis" for item in unmatched)
    yandex_other = next(item for item in unmatched if item["platform"] == "yandex")
    assert yandex_other["reason"] == "limit"
    assert yandex_other["relation"] == "other_address"
    collected = SavedHtmlCollector(ROOT).collect(
        city="Алматы",
        category=SALON,
        platforms=("2gis", "yandex", "google"),
        source_mode="fixture",
        evidence_dir=tmp_path / "collector",
        limit=1,
    )
    assert [item.external_id for item in collected.companies] == [NORTH]
    assert collected.unmatched
    assert collected.status == "discover_done"


def test_yandex_only_limit_accepts_one_company(tmp_path: Path, migrated_engine) -> None:
    root = tmp_path / "saved"
    page = root / "yandex"
    page.mkdir(parents=True)
    (page / "page-1.html").write_text(
        """<!DOCTYPE html><html><body>
        <section data-page="search" data-platform="yandex">
        <a data-candidate="1" data-external-id="1001" href="https://yandex.kz/maps/org/one/1001">
        <span data-field="name">One Salon</span>
        <span data-field="address">улица Абая, 10</span>
        <span data-field="category">Салон красоты</span>
        <span data-field="phones">8 701 000 10 01</span>
        </a>
        <a data-candidate="2" data-external-id="1002" href="https://yandex.kz/maps/org/two/1002">
        <span data-field="name">Two Salon</span>
        <span data-field="address">улица Абая, 12</span>
        <span data-field="category">Салон красоты</span>
        <span data-field="phones">+7 701 000 10 02</span>
        </a>
        </section></body></html>""",
        encoding="utf-8",
    )
    (root / "manifest.yaml").write_text(
        yaml.safe_dump({"city": "Алматы", "category": SALON, "starts": {"yandex": "yandex/page-1.html"}}, allow_unicode=True),
        encoding="utf-8",
    )
    pages = root / "pages"
    pages.mkdir()
    (pages / "blank.png").write_bytes((ROOT / "pages" / "blank.png").read_bytes())
    walked = walk_saved(
        root,
        city="Алматы",
        category=SALON,
        platforms=("yandex",),
        source="fixture",
        evidence_dir=tmp_path / "evidence",
        limit=1,
    )
    assert len(walked.branches) == 1
    assert walked.branches[0].firm_id is None
    assert walked.branches[0].platforms["yandex"].external_id == "1001"
    assert len(walked.progress["unmatched"]) == 1
    assert walked.progress["unmatched"][0]["reason"] == "limit"
    assert walked.progress["unmatched"][0]["external_id"] == "1002"
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    run_discovery(
        factory,
        city="Алматы",
        category=SALON,
        platforms=("yandex",),
        limit=1,
        out=tmp_path / "out.xlsx",
        source_mode="fixture",
        root=root,
        fixtures_dir=root / "pages",
    )
    with factory() as session:
        companies = list(session.scalars(select(Company)).all())
        assert len(companies) == 1
        assert companies[0].id_2gis is None
        items = list(session.scalars(select(CollectionBatchItem)).all())
        assert {item.status for item in items} == {"completed", "blocked"}
        blocked = [item for item in items if item.status == "blocked"]
        assert len(blocked) == 1
        assert blocked[0].company_id is None
        assert blocked[0].id_2gis == "trace:yandex:1002"


def test_robot_check_interrupts_the_saved_walk(tmp_path: Path) -> None:
    result = _walk(tmp_path, platforms=("2gis",))
    assert result.status == "interrupted"
    assert result.progress["blocked"][0]["access"] == "robot_check"
    assert {branch.firm_id for branch in result.branches} == {
        "70000001000000901",
        "70000001000000902",
        "70000001000000903",
        "70000001000000904",
    }


def test_company_page_keeps_ambiguous_off_the_miss_line() -> None:
    ambiguous = SimpleNamespace(
        id=1,
        platform="yandex",
        match_status="not_found",
        status="collected",
        fail_reason=None,
        collected_at=T0,
        field_sources={"_collection": {"hold": "ambiguous_match"}, "name": {"reason": "ambiguous"}},
    )
    missing = SimpleNamespace(
        id=2,
        platform="google",
        match_status="not_found",
        status="collected",
        fail_reason=None,
        collected_at=T0,
        field_sources={},
    )
    other = SimpleNamespace(
        id=3,
        platform="yandex",
        match_status="other_address",
        status="collected",
        fail_reason=None,
        collected_at=T0,
        field_sources={},
    )
    found = SimpleNamespace(
        id=4,
        platform="2gis",
        match_status="found",
        status="verified",
        fail_reason=None,
        collected_at=T0,
        field_sources={},
    )
    assert _presence(ambiguous) == "Совпадение не определено"
    assert "не найдена" not in _indicator("yandex", [ambiguous])["text"]
    assert _presence(missing) == "Не найдена"
    assert _indicator("google", [missing])["text"] == "не найдена"
    assert _indicator("yandex", [other])["text"] == "другой адрес"
    assert _indicator("2gis", [found])["text"] == "есть"


def test_one_phone_function_keeps_the_display_string(tmp_path, monkeypatch) -> None:
    number, shown = phone_parts("8 (701) 000-09-01")
    assert number == "77010000901"
    assert shown == "8 (701) 000-09-01"
    assert collection_rate_seconds() == 2
    monkeypatch.setenv("ZHOL_COLLECTION_PACE", str(tmp_path / "pace"))
    slept: list[float] = []
    wait_turn(sleep=slept.append, clock=lambda: T0)
    wait_turn(sleep=slept.append, clock=lambda: T0)
    assert slept == [2.0]
