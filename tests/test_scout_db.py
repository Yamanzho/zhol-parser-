from __future__ import annotations

import csv
import shutil
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.jobs import drain, enqueue_collect, handlers, recheck_snapshots
from zhol.agents.scout.persist import COLLECTED_BY
from zhol.browser.fixtures import DEFAULT_FIXTURES
from zhol.cli import main
from zhol.db.models import CardSnapshot, Company, CompanyPlatformId, HumanTask, Job
from zhol.jobs import run_one
from zhol.scoring import derive, score, to_score_row
from zhol.timeutil import reset_clock, set_now
from zhol.transitions import IllegalTransition, transition

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
FULL = "70000001000000011"
PARTIAL = "70000001000000012"
INVALID = "70000001000000013"
DUP = "70000001000000017"
FORMULA = ("70000001000000021", "70000001000000022", "70000001000000023")


@pytest.fixture(autouse=True)
def _clock() -> Iterator[None]:
    set_now(T0)
    yield
    reset_clock()


@pytest.fixture(autouse=True)
def _no_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("zhol.agents.scout.sample.selected_for_recheck", lambda _id_2gis: False)


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _company(session: Session, id_2gis: str, name: str = "AOE Beauty") -> Company:
    company = Company(id_2gis=id_2gis, name=name, source="manual", city="Алматы")
    session.add(company)
    session.flush()
    return company


def _collect(factory: sessionmaker[Session], company_id: int, key: str, fixtures: Path | None = None) -> None:
    with factory() as session, session.begin():
        enqueue_collect(session, company_id, "prospect", str(fixtures or DEFAULT_FIXTURES), collection_key=key)
    drain(factory, notifier=lambda _text: None)


def _snapshots(session: Session, company_id: int) -> list[CardSnapshot]:
    return list(
        session.scalars(
            select(CardSnapshot).where(CardSnapshot.company_id == company_id).order_by(CardSnapshot.id)
        ).all()
    )


def test_collects_three_platforms_and_keeps_missing_distinct_from_zero(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        full = _company(session, FULL)
        partial = _company(session, PARTIAL)
        full_id, partial_id = full.id, partial.id
    _collect(factory, full_id, "full")
    _collect(factory, partial_id, "partial")
    with factory() as session:
        full_rows = {row.platform: row for row in _snapshots(session, full_id)}
        partial_rows = {row.platform: row for row in _snapshots(session, partial_id)}
        assert set(full_rows) == {"2gis", "yandex", "google"}
        assert full_rows["2gis"].status == "verified"
        assert full_rows["2gis"].fields["rating"] == 4.8
        assert full_rows["2gis"].fields["category"] == "Салон красоты"
        assert full_rows["2gis"].field_sources["category"]["read_by"] == "code"
        assert full_rows["2gis"].fields["is_ad"] is False
        source = full_rows["2gis"].field_sources["rating"]
        assert source["read_by"] == "code"
        assert source["url"].endswith(FULL)
        assert source["seen_at"]
        assert Path(source["screenshot"]).is_file()
        assert Path(full_rows["2gis"].field_sources["_collection"]["raw_path"]).is_file()
        yandex = session.get(CompanyPlatformId, (full_id, "yandex"))
        assert yandex is not None and yandex.external_id == "100011"
        assert session.get(CompanyPlatformId, (full_id, "google")) is None
        assert full_rows["google"].status == "collected"
        assert full_rows["google"].match_status == "found"
        assert session.scalar(select(HumanTask).where(HumanTask.reason == "match_needs_review")) is not None
        assert partial_rows["2gis"].fields["rating"] is None
        assert partial_rows["2gis"].fields["reviews_count"] == 0
        assert partial_rows["2gis"].status == "verified"
        assert partial_rows["yandex"].match_status == "not_found"
        assert partial_rows["yandex"].screenshot_path
        assert partial_rows["google"].match_status == "other_address"
        assert session.get(CompanyPlatformId, (partial_id, "google")) is None
        scored = score(to_score_row(partial_rows, derive(partial_rows["2gis"]), []))
        row = to_score_row(partial_rows, derive(partial_rows["2gis"]), [])
        assert row["2гис_рейтинг"] == ""
        assert row["2гис_отзывов"] == "0"
        assert isinstance(scored["итого"], int)
        verified = full_rows["2gis"]
        with pytest.raises(IllegalTransition):
            transition(verified, "collected", session)


def test_retry_is_idempotent_and_verified_platform_id_is_kept(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        company = _company(session, FULL)
        company_id = company.id
        session.add(
            CardSnapshot(
                company_id=company_id,
                platform="2gis",
                match_status="found",
                url="https://2gis.kz/almaty/firm/" + FULL,
                fields={"rating": 4.0},
                field_sources={},
                purpose="prospect",
                collected_at=T0,
                collected_by="manual_import",
                status="verified",
            )
        )
    _collect(factory, company_id, "once")
    with factory() as session, session.begin():
        manual = session.scalar(select(CardSnapshot).where(CardSnapshot.collected_by == "manual_import"))
        assert manual is not None and manual.fields["rating"] == 4.0 and manual.status == "verified"
        row = session.get(CompanyPlatformId, (company_id, "yandex"))
        assert row is not None
        row.access_status = "verified"
        row.public_url = "https://keep.example"
        before = len(_snapshots(session, company_id))
    _collect(factory, company_id, "once")
    _collect(factory, company_id, "again")
    with factory() as session:
        rows = _snapshots(session, company_id)
        scout_rows = [row for row in rows if row.collected_by == COLLECTED_BY]
        assert len(scout_rows) == 6
        assert len(rows) == before + 3
        kept = session.get(CompanyPlatformId, (company_id, "yandex"))
        assert kept is not None
        assert kept.external_id == "100011"
        assert kept.public_url == "https://keep.example"
        assert kept.access_status == "verified"
        from zhol.agents.scout.compare import compare_companies

        compared = [item for item in compare_companies(session) if item.field == "rating" and item.platform == "2gis"]
        assert compared and compared[-1].old == 4.0 and compared[-1].new == 4.8 and compared[-1].match is False
        assert compared[-1].screenshot


def test_does_not_attach_another_companys_platform_id(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        first = _company(session, FULL)
        second = _company(session, DUP, "Nord")
        first_id, second_id = first.id, second.id
    _collect(factory, first_id, "first")
    _collect(factory, second_id, "second")
    with factory() as session:
        owner = session.get(CompanyPlatformId, (first_id, "yandex"))
        assert owner is not None and owner.external_id == "100011"
        assert session.get(CompanyPlatformId, (second_id, "yandex")) is None
        assert session.scalar(
            select(HumanTask).where(HumanTask.reason == "platform_id_conflict", HumanTask.object_type == "card_snapshot")
        ) is not None
        held = session.scalar(
            select(CardSnapshot).where(CardSnapshot.company_id == second_id, CardSnapshot.platform == "yandex")
        )
        assert held is not None and held.status == "collected"
        assert held.field_sources["_collection"]["hold"] == "platform_id_conflict"


def test_missing_page_fails_three_times_then_asks_a_human(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        company = _company(session, INVALID)
        company_id = company.id
        enqueue_collect(session, company_id, "prospect", str(DEFAULT_FIXTURES), collection_key="missing")
    registered = handlers(factory)
    assert run_one(factory, "browser", registered, notifier=lambda _text: None) is not None
    set_now(T0 + timedelta(minutes=1))
    assert run_one(factory, "browser", registered, notifier=lambda _text: None) is not None
    set_now(T0 + timedelta(minutes=6))
    assert run_one(factory, "browser", registered, notifier=lambda _text: None) is not None
    with factory() as session:
        rows = _snapshots(session, company_id)
        assert len(rows) == 3
        assert {row.status for row in rows} == {"failed"}
        assert all(row.fail_reason and "PageNotLoaded" in row.fail_reason for row in rows)
        job = session.scalar(select(Job).where(Job.type == "scout.collect"))
        assert job is not None and job.status == "dead" and job.attempts == 3
        assert session.scalar(select(HumanTask).where(HumanTask.reason == "job_dead")) is not None


def test_interrupted_job_does_not_write_a_second_snapshot(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        company = _company(session, FULL)
        company_id = company.id
        enqueue_collect(session, company_id, "prospect", str(DEFAULT_FIXTURES), collection_key="once")
    state = {"calls": 0}
    registered = handlers(factory)

    def flaky(payload: dict) -> None:
        registered["scout.collect"](payload)
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("interrupted")

    assert run_one(factory, "browser", {"scout.collect": flaky}, notifier=lambda _text: None) is not None
    with factory() as session:
        assert len(_snapshots(session, company_id)) == 3
    set_now(T0 + timedelta(minutes=1))
    assert run_one(factory, "browser", {"scout.collect": flaky}, notifier=lambda _text: None) is not None
    with factory() as session:
        assert len(_snapshots(session, company_id)) == 3
        assert session.scalar(select(Job).where(Job.type == "scout.collect")).status == "done"


def test_recheck_rejects_after_more_than_one_difference(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as session, session.begin():
        company = _company(session, FULL)
        company_id = company.id
    _collect(factory, company_id, "origin")
    with factory() as session:
        rows = _snapshots(session, company_id)
        assert rows[0].platform == "2gis"
        origin = [row.id for row in rows]
        jobs_before = session.scalar(select(func.count()).select_from(Job).where(Job.type == "scout.collect"))
    one = tmp_path / "one"
    shutil.copytree(DEFAULT_FIXTURES, one)
    card = one / "2gis" / f"{FULL}.html"
    card.write_text(card.read_text(encoding="utf-8").replace(">4.8<", ">4.1<", 1), encoding="utf-8")
    with factory() as session, session.begin():
        recheck_snapshots(
            session,
            {"company_id": company_id, "snapshot_ids": origin, "fixtures_dir": str(one), "purpose": "prospect", "collection_key": "origin"},
        )
    with factory() as session:
        stored = session.get(CardSnapshot, origin[0])
        assert stored is not None and stored.status == "verified" and stored.recheck_count == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.type == "scout.collect")) == jobs_before
    two = tmp_path / "two"
    shutil.copytree(DEFAULT_FIXTURES, two)
    changed = two / "2gis" / f"{FULL}.html"
    text = changed.read_text(encoding="utf-8").replace(">4.8<", ">4.1<", 1).replace('data-field="photos_count">15<', 'data-field="photos_count">1<', 1)
    changed.write_text(text, encoding="utf-8")
    with factory() as session, session.begin():
        recheck_snapshots(
            session,
            {"company_id": company_id, "snapshot_ids": origin, "fixtures_dir": str(two), "purpose": "prospect", "collection_key": "origin"},
        )
    drain(factory, notifier=lambda _text: None)
    with factory() as session:
        assert session.get(CardSnapshot, origin[0]).status == "rejected"
        retry = [row.id for row in _snapshots(session, company_id) if row.id not in origin and row.platform == "2gis"]
        assert session.get(CardSnapshot, retry[0]).status == "verified"
    third = tmp_path / "three"
    shutil.copytree(two, third)
    again = third / "2gis" / f"{FULL}.html"
    again.write_text(again.read_text(encoding="utf-8").replace(">4.1<", ">3.3<", 1).replace('data-field="photos_count">1<', 'data-field="photos_count">8<', 1), encoding="utf-8")
    with factory() as session, session.begin():
        fresh = [row.id for row in _snapshots(session, company_id) if row.field_sources.get("_collection", {}).get("key") == "origin:retry"]
        recheck_snapshots(
            session,
            {
                "company_id": company_id,
                "snapshot_ids": fresh,
                "fixtures_dir": str(third),
                "purpose": "prospect",
                "collection_key": "origin:retry",
            },
        )
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.type == "scout.collect")) == 2
        assert session.scalar(select(HumanTask).where(HumanTask.reason == "scout_rejected_twice")) is not None


def test_formula_batch_rejects_snapshots_and_etalon_does_not(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        ids = []
        for id_2gis in FORMULA:
            ids.append(_company(session, id_2gis, id_2gis).id)
    for company_id in ids:
        _collect(factory, company_id, f"formula-{company_id}")
    with factory() as session, session.begin():
        from zhol.agents.scout.formula import check_since

        reasons = check_since(session, T0)
        assert "yandex.rating - 2gis.rating = -0.1" in reasons
        statuses = set(session.scalars(select(CardSnapshot.status).where(CardSnapshot.collected_by == COLLECTED_BY)).all())
        assert statuses == {"rejected"}
        assert session.scalar(select(HumanTask).where(HumanTask.reason == "formula_detected")) is not None
    with factory() as session, session.begin():
        session.execute(CardSnapshot.__table__.delete())
        session.execute(HumanTask.__table__.delete())
        etalon = ROOT / "source" / "etalon-50.csv"
        if not etalon.is_file():
            return
        from zhol.etalon import import_etalon

        import_etalon(session, etalon)
    with factory() as session, session.begin():
        from datetime import date, time

        from zhol.agents.scout.formula import check_since
        from zhol.timeutil import ALMATY

        since = datetime.combine(date(2026, 9, 1), time(0, 0), tzinfo=ALMATY)
        assert check_since(session, since) == []
        verified = session.scalar(select(func.count()).select_from(CardSnapshot).where(CardSnapshot.status == "verified"))
        assert verified == 150


def test_import_contacts_skips_reference_companies(factory: sessionmaker[Session], tmp_path: Path) -> None:
    path = tmp_path / "contacts.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, ["id_2gis", "название", "тип", "номер_whatsapp", "аккаунт_instagram", "etalon", "url_2gis"])
        writer.writeheader()
        writer.writerow({"id_2gis": "70000001000000991", "название": "New Salon", "тип": "салон", "номер_whatsapp": "+7 701 111 11 11", "аккаунт_instagram": "@new", "etalon": "0", "url_2gis": "https://2gis.kz/almaty/firm/70000001000000991"})
        writer.writerow({"id_2gis": "70000001000000992", "название": "Reference", "тип": "салон", "номер_whatsapp": "", "аккаунт_instagram": "", "etalon": "1", "url_2gis": ""})
    from zhol.agents.scout.contacts import import_contacts

    with factory() as session, session.begin():
        stats = import_contacts(session, path)
        assert stats.companies == 1 and stats.skipped == 1
        created = session.scalar(select(Company).where(Company.id_2gis == "70000001000000991"))
        assert created is not None and created.source == "base_550_contacts"
        assert session.scalar(select(Company.id).where(Company.id_2gis == "70000001000000992")) is None
        assert session.scalar(select(func.count()).select_from(CardSnapshot).where(CardSnapshot.company_id == created.id)) == 0
        again = import_contacts(session, path)
        assert again.companies == 0 and again.skipped == 2
    source = ROOT / "source" / "base-contacts.csv"
    if not source.is_file():
        return
    with factory() as session, session.begin():
        from zhol.etalon import import_etalon

        import_etalon(session, ROOT / "source" / "etalon-50.csv")
        before = {row.id_2gis: row.name for row in session.scalars(select(Company)).all()}
        stats = import_contacts(session, source)
        names = {row.id_2gis: row.name for row in session.scalars(select(Company).where(Company.source == "etalon_2026_09_30")).all()}
        assert names == {key: value for key, value in before.items() if key in names}
        assert stats.companies > 0
        assert session.scalar(select(func.count()).select_from(Company)) == len(before) + stats.companies


def test_cli_queues_existing_companies_and_compares(factory: sessionmaker[Session], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with factory() as session, session.begin():
        _company(session, FULL)
    ids = tmp_path / "ids.txt"
    ids.write_text(FULL + "\n70000001000000999\n", encoding="utf-8")
    assert main(["scout", "--ids-file", str(ids), "--enqueue-only"], factory=factory, notifier=lambda _text: None) == 2
    ids.write_text(FULL + "\n", encoding="utf-8")
    assert main(["scout", "--ids-file", str(ids), "--enqueue-only", "--purpose", "prospect"], factory=factory, notifier=lambda _text: None) == 0
    assert "поставлено 1" in capsys.readouterr().out
    assert main(["scout-batch-check", "--since", "2026-11-02"], factory=factory) == 0
    assert "зависимостей нет" in capsys.readouterr().out
    assert main(["scout-compare"], factory=factory) == 0
