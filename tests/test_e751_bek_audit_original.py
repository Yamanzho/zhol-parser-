"""Bek audit import against the original four-sheet workbook."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.audit.bek import import_bek_audit, task_totals
from zhol.audit.workbook import read_workbook
from zhol.db.models import Audit, CardSnapshot, ChangeItem, ChangeLog, Company, HumanTask, ListingImprovementPlan
from zhol.timeutil import ALMATY

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "source" / "bek-audit-original.xlsx"
FINISHED = frozenset({"completed", "prepared", "applied", "done", "submitted"})


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _tasks(plan: dict, task_type: str, platform: str | None = None) -> list[dict]:
    found = [task for task in [*plan["automatic_preparation"], *plan["manual_work"]] if task["type"] == task_type]
    if platform is not None:
        found = [task for task in found if task["platform"] == platform]
    return found


def _plan(session: Session, id_2gis: str) -> dict:
    company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
    assert company is not None
    stored = session.scalar(select(ListingImprovementPlan).where(ListingImprovementPlan.company_id == company.id))
    assert stored is not None
    return stored.plan


def _snapshot(session: Session, id_2gis: str, platform: str) -> CardSnapshot:
    company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
    row = session.scalar(
        select(CardSnapshot).where(
            CardSnapshot.company_id == company.id,
            CardSnapshot.platform == platform,
            CardSnapshot.collected_by == "bek_audit",
        )
    )
    assert row is not None
    return row


def test_original_workbook_reads_all_four_sheets() -> None:
    book = read_workbook(ORIGINAL)
    guide = dict(book.guide)
    assert book.sheets == ("Сводка", "Контакты для связи", "Все данные", "Как читать")
    assert book.sheet == "Все данные"
    assert "Данных нет" in guide["Пустая ячейка"]
    assert "Прайс компании" in guide["Вкладка цен"]
    assert "50" in guide["Что это"]
    importable = [row for row in book.rows if row.skip_reason is None]
    salon = [row for row in importable if row.layout != "section"]
    section = [row for row in importable if row.layout == "section"]
    assert len(book.rows) == 150
    assert len(importable) == 150
    assert len(salon) == 50
    assert len(section) == 100
    assert {row.text("слой") for row in section} == {"косметология (Almaty)"}
    assert {row.text("тип") for row in section} == {"косметология"}
    star = importable[0]
    assert star.text("название") == "Star Nails"
    assert star.text("id_2gis") == "9429940001464980"
    assert star.text("url_2gis") == "https://2gis.kz/almaty/firm/9429940001464980"
    assert star.text("балл_итого") == "90"
    assert star.hyperlinks["номер_whatsapp"] == "https://wa.me/77073800939"
    assert star.hyperlinks["аккаунт_instagram"] == "https://instagram.com/starnails_kz"
    assert "открыть" not in star.text("url_2gis")


def test_original_import_preserves_scores_dates_links_and_is_idempotent(factory) -> None:
    book = read_workbook(ORIGINAL)
    expected = {row.text("id_2gis"): row for row in book.rows if row.skip_reason is None}
    with factory() as session, session.begin():
        first = import_bek_audit(session, ORIGINAL)
    assert first.sheet == "Все данные"
    assert first.companies == 150
    assert first.snapshots == 250
    assert first.audits == 150
    assert first.plans == 150
    assert first.ambiguous == []
    assert first.unmatched == []
    with factory() as session:
        audits = session.scalars(select(Audit).where(Audit.prompt_version == "bek-audit-import-1")).all()
        assert len(audits) == 150
        for audit in audits:
            company = session.get(Company, audit.company_id)
            row = expected[company.id_2gis]
            assert audit.score_total == int(row.text("балл_итого"))
            assert audit.score_groups == {
                "reviews": int(row.text("балл_отзывы")),
                "prices": int(row.text("балл_цены")),
                "photos": int(row.text("балл_фото")),
                "contacts": int(row.text("балл_контакты")),
            }
            assert audit.observations["collected_on"] == "2026-09-30"
            twogis = _snapshot(session, company.id_2gis, "2gis")
            assert twogis.collected_at.astimezone(ALMATY).date().isoformat() == row.text("дата_просмотра")
            assert twogis.fields["last_review_date"] == row.text("2гис_последний_отзыв")
            assert twogis.url == row.text("url_2gis")
            assert twogis.field_sources["whatsapp"]["hyperlink"] == row.hyperlinks["номер_whatsapp"]
        proface = session.scalar(select(Company).where(Company.id_2gis == "70000001075254224"))
        assert proface is not None
        assert proface.name == "ProFace"
        assert proface.niche == "косметология"
        assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0
        assert session.scalar(select(func.count()).select_from(ChangeItem)) == 0
        before = (
            session.scalar(select(func.count()).select_from(Company)),
            session.scalar(select(func.count()).select_from(CardSnapshot)),
            session.scalar(select(func.count()).select_from(Audit)),
            session.scalar(select(func.count()).select_from(ListingImprovementPlan)),
            session.scalar(select(func.count()).select_from(HumanTask)),
        )
    with factory() as session, session.begin():
        second = import_bek_audit(session, ORIGINAL)
    assert (second.companies, second.snapshots, second.audits, second.plans) == (0, 0, 0, 0)
    with factory() as session:
        after = (
            session.scalar(select(func.count()).select_from(Company)),
            session.scalar(select(func.count()).select_from(CardSnapshot)),
            session.scalar(select(func.count()).select_from(Audit)),
            session.scalar(select(func.count()).select_from(ListingImprovementPlan)),
            session.scalar(select(func.count()).select_from(HumanTask)),
        )
    assert after == before


def test_original_sample_findings_follow_the_workbook(factory) -> None:
    with factory() as session, session.begin():
        stats = import_bek_audit(session, ORIGINAL)
        montebello = _plan(session, "70000001066660465")
        badi = _plan(session, "70000001025141518")
        style = _plan(session, "70000001054653678")
        nogti = _plan(session, "70000001040596963")
        nickol = _plan(session, "70000001044789875")
        merke = _plan(session, "70000001039453735")
        jam = _snapshot(session, "70000001112462234", "google")
        aidar = _snapshot(session, "70000001110594011", "2gis")
        yabloko = _snapshot(session, "70000001060051188", "2gis")
        montebello_google = _snapshot(session, "70000001066660465", "google")
        merke_yandex = _snapshot(session, "70000001039453735", "yandex")
        plans = [montebello, badi, style, nogti]
    assert montebello["score_total"] == 72
    assert montebello["source_date"] == "2026-09-30"
    assert montebello["observations"] == ["нет в Google; пустая вкладка цен в 2ГИС"]
    assert _tasks(montebello, "reconcile_platform_data") == []
    create = _tasks(montebello, "create_listing_candidate", "google")
    assert create[0]["status"] == "unsupported"
    assert create[0]["current"] == {"match_status": "not_found"}
    assert create[0]["completed"] is False
    prices = _tasks(montebello, "add_prices", "2gis")
    assert prices[0]["current"] == {"prices": "empty"}
    assert prices[0]["desired"] == {"prices": "company_price"}
    services = _tasks(montebello, "add_services", "2gis")
    assert services[0]["desired"] == {"services": "client service list"}
    assert "price" not in str(services[0]["desired"]["services"])
    assert montebello_google.fields["rating"] is None
    assert montebello_google.fields["reviews_count"] is None
    assert montebello_google.field_sources["reviews_count"]["csv_column"] is None
    badi_prices = _tasks(badi, "add_prices", "2gis")
    assert badi_prices[0]["current"] == {"prices": "photo_only"}
    assert _tasks(badi, "add_services", "2gis")[0]["current"] == {"prices": "photo_only"}
    assert _tasks(badi, "prepare_review_replies", "2gis")[0]["current"] == {"replies_count": 8, "reviews_shown": 51}
    assert _tasks(badi, "create_listing_candidate", "google")[0]["status"] == "unsupported"
    assert _tasks(style, "request_owner_verification", "google")[0]["current"] == {"owner_claimed": False}
    assert _tasks(style, "request_owner_verification", "google")[0]["status"] == "manual"
    assert _tasks(style, "add_prices", "2gis")[0]["current"] == {"prices": "photo_only"}
    assert _tasks(style, "add_prices", "yandex")[0]["current"] == {"prices": "no"}
    assert _tasks(style, "add_services", "yandex")[0]["desired"] == {"services": "client service list"}
    assert _tasks(nogti, "prepare_review_replies", "2gis")[0]["current"] == {"replies_count": 0, "reviews_shown": 52}
    assert _tasks(nogti, "add_prices", "2gis") == []
    assert _tasks(nogti, "add_services", "2gis") == []
    assert _tasks(nogti, "add_prices", "yandex")[0]["current"] == {"prices": "yes_outdated"}
    assert _tasks(nogti, "request_owner_verification", "google") == []
    assert nogti["observations"] == ["нет ответов на отзывы"]
    assert _tasks(nickol, "request_owner_verification", "google") == []
    assert _tasks(nickol, "update_listing") == []
    reconcile = _tasks(nickol, "reconcile_platform_data", "2gis")
    assert "рубрика" in reconcile[0]["current"]["text"][0]
    assert _tasks(merke, "request_real_photos", "2gis")[0]["current"] == {"photos_count": 2}
    assert merke_yandex.fields["ratings_count"] == 0
    assert merke_yandex.fields["photos_count"] == 0
    assert jam.fields["reviews_count"] == 0
    assert jam.fields["rating"] is None
    assert jam.field_sources["reviews_count"]["formula"] is False
    assert aidar.fields["has_instagram"] is False
    assert aidar.fields["instagram"] is None
    aidar_plan = None
    with factory() as session:
        aidar_plan = _plan(session, "70000001110594011")
    contact = _tasks(aidar_plan, "update_contacts", "2gis")
    assert contact[0]["current"] == {"has_instagram": False}
    assert contact[0]["desired"] == {"instagram": "client value"}
    assert yabloko.fields["phones"] == ["+7 707 755 88 89"]
    assert yabloko.fields["whatsapp"] == "+7 747 578 11 03"
    for plan in (*plans, nickol, merke, aidar_plan):
        assert plan["listing_modified"] is False
        for task in [*plan["automatic_preparation"], *plan["manual_work"]]:
            assert task["completed"] is False
            assert task["status"] not in FINISHED
            assert "балл" not in (task["evidence"].get("column") or "")
            assert task["type"] != "add_description"
        assert _tasks(plan, "add_description") == []
        assert _tasks(plan, "update_hours") == []
    assert stats.tasks_by_type["create_listing_candidate"] >= 1
    assert stats.tasks_by_type["prepare_review_replies"] >= 1
    assert stats.tasks_by_type["request_real_photos"] >= 1
    assert stats.tasks_by_type["request_owner_verification"] >= 1
    assert stats.tasks_by_type["reconcile_platform_data"] >= 1
    assert stats.tasks_by_type["update_contacts"] >= 1
    assert stats.tasks_by_type["add_prices"] >= 1
    assert stats.tasks_by_type["add_services"] >= 1


def test_misaligned_rows_are_not_merged_into_a_company(factory) -> None:
    with factory() as session, session.begin():
        session.add(Company(id_2gis="99900000000000001", name="ProFace", city="Алматы", source="seed"))
        session.add(Company(id_2gis="111", name="Montebello", city="Алматы", source="seed"))
        session.add(Company(id_2gis="222", name="Montebello", city="Алматы", source="seed"))
        stats = import_bek_audit(session, ORIGINAL)
        decoy = session.scalar(select(Company).where(Company.id_2gis == "99900000000000001"))
        attached = session.scalar(
            select(func.count()).select_from(CardSnapshot).where(CardSnapshot.company_id == decoy.id)
        )
        real_proface = session.scalar(select(Company).where(Company.id_2gis == "70000001075254224"))
        decoys = session.scalars(select(Company).where(Company.name == "Montebello", Company.id_2gis.in_(["111", "222"]))).all()
        real = session.scalar(select(Company).where(Company.id_2gis == "70000001066660465"))
        real_audit = session.scalar(select(Audit).where(Audit.company_id == real.id))
    assert attached == 0
    assert real_proface is not None
    assert real_proface.id != decoy.id
    assert real_audit is not None
    assert real_audit.score_total == 72
    for decoy_montebello in decoys:
        assert session_audit_count(factory, decoy_montebello.id) == 0
    assert stats.unmatched == []
    assert stats.ambiguous == []


def session_audit_count(factory, company_id: int) -> int:
    with factory() as session:
        return session.scalar(select(func.count()).select_from(Audit).where(Audit.company_id == company_id))


def test_newer_verified_snapshot_from_the_original_is_kept(factory) -> None:
    with factory() as session, session.begin():
        company = Company(id_2gis="70000001066660465", name="Montebello", city="Алматы", source="seed")
        session.add(company)
        session.flush()
        session.add(
            CardSnapshot(
                company_id=company.id,
                platform="2gis",
                match_status="found",
                url="https://2gis.kz/almaty/firm/70000001066660465",
                fields={"photos_count": 7, "name": "Montebello"},
                field_sources={"photos_count": {"csv_column": "kept"}},
                purpose="prospect",
                collected_at=datetime(2026, 10, 11, tzinfo=ALMATY),
                collected_by="manual_import",
                status="verified",
            )
        )
        stats = import_bek_audit(session, ORIGINAL)
        kept = session.scalar(
            select(CardSnapshot).where(
                CardSnapshot.company_id == company.id,
                CardSnapshot.platform == "2gis",
                CardSnapshot.status == "verified",
            )
        )
        bek = session.scalar(
            select(CardSnapshot).where(
                CardSnapshot.company_id == company.id,
                CardSnapshot.platform == "2gis",
                CardSnapshot.collected_by == "bek_audit",
            )
        )
    assert kept.fields["photos_count"] == 7
    assert bek is None
    assert "70000001066660465 2gis" in stats.skipped_newer


def test_task_totals_cover_the_workbook_evidence(factory) -> None:
    with factory() as session, session.begin():
        stats = import_bek_audit(session, ORIGINAL)
        plans = [row.plan for row in session.scalars(select(ListingImprovementPlan))]
    assert task_totals(plans) == stats.tasks_by_type
    assert set(stats.tasks_by_type) <= {
        "create_listing_candidate",
        "update_listing",
        "add_description",
        "update_contacts",
        "update_hours",
        "add_services",
        "add_prices",
        "request_real_photos",
        "prepare_review_replies",
        "request_owner_verification",
        "reconcile_platform_data",
        "request_client_facts",
    }
