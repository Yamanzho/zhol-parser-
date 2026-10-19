"""Cosmetology section of the original Bek workbook."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.audit.bek import import_bek_audit
from zhol.audit.workbook import read_workbook
from zhol.db.models import Audit, CardSnapshot, ChangeItem, ChangeLog, Company, CompanyPlatformId, HumanTask, ListingImprovementPlan

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "source" / "bek-audit-original.xlsx"


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _section_rows():
    book = read_workbook(ORIGINAL)
    return [row for row in book.rows if row.layout == "section" and row.skip_reason is None]


def test_same_address_and_same_name_are_not_merged(factory) -> None:
    rows = _section_rows()
    shared = {}
    for row in rows:
        shared.setdefault(row.text("адрес_2гис"), []).append(row)
    pair = next(items for items in shared.values() if len(items) > 1)
    assert {item.text("название") for item in pair} == {"Beauty Day", "Morpheus Clinic"}
    assert pair[0].text("id_2gis") != pair[1].text("id_2gis")
    with factory() as session, session.begin():
        session.add(Company(id_2gis=pair[0].text("id_2gis"), name="Чужая клиника", city="Алматы", source="seed"))
        stats = import_bek_audit(session, ORIGINAL)
        blocked = session.scalar(select(Company).where(Company.id_2gis == pair[0].text("id_2gis")))
        blocked_snapshots = session.scalar(
            select(func.count()).select_from(CardSnapshot).where(CardSnapshot.company_id == blocked.id)
        )
        other = session.scalar(select(Company).where(Company.id_2gis == pair[1].text("id_2gis")))
        other_address = session.scalar(
            select(CardSnapshot).where(CardSnapshot.company_id == other.id, CardSnapshot.platform == "2gis")
        )
    assert blocked.name == "Чужая клиника"
    assert blocked_snapshots == 0
    assert other.name == pair[1].text("название")
    assert other_address.fields["address"] == pair[1].text("адрес_2гис")
    assert any(item["name"] == pair[0].text("название") for item in stats.ambiguous)
    assert stats.companies == 149


def test_section_plans_keep_sheet_scores_and_skip_copied_platforms(factory) -> None:
    rows = {row.text("id_2gis"): row for row in _section_rows()}
    assert len(rows) == 100
    with factory() as session, session.begin():
        first = import_bek_audit(session, ORIGINAL)
        plans = {
            row.plan["id_2gis"]: row.plan
            for row in session.scalars(select(ListingImprovementPlan))
            if row.plan.get("layer") == "косметология (Almaty)"
        }
        salon_company = session.scalar(select(Company).where(Company.id_2gis == "70000001066660465"))
        salon = session.scalar(
            select(ListingImprovementPlan).where(ListingImprovementPlan.company_id == salon_company.id)
        )
        for firm_id, row in rows.items():
            company = session.scalar(select(Company).where(Company.id_2gis == firm_id))
            audit = session.scalar(select(Audit).where(Audit.company_id == company.id))
            twogis = session.scalar(
                select(CardSnapshot).where(
                    CardSnapshot.company_id == company.id,
                    CardSnapshot.platform == "2gis",
                    CardSnapshot.collected_by == "bek_audit",
                )
            )
            others = session.scalars(
                select(CardSnapshot).where(
                    CardSnapshot.company_id == company.id,
                    CardSnapshot.platform.in_(("yandex", "google")),
                )
            ).all()
            yandex_id = session.get(CompanyPlatformId, (company.id, "yandex"))
            assert audit.score_total == int(row.text("балл_итого"))
            assert audit.score_groups["prices"] == int(row.text("балл_цены"))
            assert audit.observations["category"] == "косметология"
            assert audit.observations["layer"] == "косметология (Almaty)"
            assert twogis.url == row.text("url_2gis")
            assert twogis.fields["rating"] == float(row.text("2гис_рейтинг"))
            assert twogis.fields["photos_count"] == int(row.text("2гис_фото"))
            assert twogis.fields["reviews_count"] is None
            assert twogis.fields["last_review_date"] is None
            assert twogis.field_sources["whatsapp"]["hyperlink"] == row.hyperlinks["номер_whatsapp"]
            assert others == []
            assert yandex_id is None
            assert plans[firm_id]["score_total"] == int(row.text("балл_итого"))
            assert plans[firm_id]["category"] == "косметология"
        nurbayan = plans["70000001109136572"]
        lazer = plans["70000001061964233"]
        beauty = plans["70000001111856465"]
        proface = plans["70000001075254224"]
        verified = [
            plan
            for plan in plans.values()
            if any(
                task["platform"] == "google" and task["current"].get("match_status") == "needs_verification"
                for task in plan["manual_work"]
            )
        ]
        assert len(verified) == 5
        for plan in verified:
            assert all(task["type"] != "create_listing_candidate" for task in [*plan["manual_work"], *plan["automatic_preparation"]])
            assert all(task["current"].get("match_status") != "not_found" for task in plan["manual_work"])
        nurbayan_tasks = [*nurbayan["manual_work"], *nurbayan["automatic_preparation"]]
        lazer_tasks = [*lazer["manual_work"], *lazer["automatic_preparation"]]
        proface_tasks = [*proface["manual_work"], *proface["automatic_preparation"]]
        assert any(task["type"] == "request_real_photos" and task["current"] == {"photos_count": 1} for task in nurbayan_tasks)
        assert any(task["type"] == "add_prices" and task["current"] == {"prices": "empty"} for task in lazer_tasks)
        assert any(task["current"].get("match_status") == "needs_verification" for task in beauty["manual_work"])
        assert proface["score_total"] == 98
        assert all(task["type"] != "add_prices" for task in proface_tasks)
        assert "layer" not in salon.plan
        assert salon.plan["score_total"] == 72
        assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0
        assert session.scalar(select(func.count()).select_from(ChangeItem)) == 0
        before = session.scalar(select(func.count()).select_from(HumanTask))
    with factory() as session, session.begin():
        second = import_bek_audit(session, ORIGINAL)
    assert (first.companies, first.snapshots, first.audits, first.plans) == (150, 250, 150, 150)
    assert (second.companies, second.snapshots, second.audits, second.plans) == (0, 0, 0, 0)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(HumanTask)) == before
