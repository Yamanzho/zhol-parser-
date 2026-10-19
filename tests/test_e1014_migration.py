"""E10.14 downgrade refuses to delete companies. Saved pages only."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from tests.conftest import alembic_config
from tests.test_e1013_identity import SALON, _candidate, _page, _run, _saved
from zhol.api.admin_data import company_detail, list_companies
from zhol.db.models import Audit, CardSnapshot, CollectionBatchItem, Company, CompanyPlatformId, Order
from zhol.timeutil import ALMATY

INDEX = "uq_company_platform_ids_platform_external"


def _factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _nullable(conn) -> str:
    return conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'companies' AND column_name = 'id_2gis'"
        )
    ).scalar_one()


def _index_present(conn) -> bool:
    return (
        conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = :name"), {"name": INDEX}).first()
        is not None
    )


def _revision(conn) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _fingerprint(conn) -> dict[str, list[tuple]]:
    companies = conn.execute(text("SELECT id, id_2gis, name FROM companies ORDER BY id")).all()
    snapshots = conn.execute(
        text("SELECT id, company_id, platform, status, match_status FROM card_snapshots ORDER BY id")
    ).all()
    audits = conn.execute(text("SELECT id, company_id, score_total, status FROM audits ORDER BY id")).all()
    orders = conn.execute(text("SELECT id, company_id, kind, amount_kzt, stage FROM orders ORDER BY id")).all()
    platforms = conn.execute(
        text("SELECT company_id, platform, external_id FROM company_platform_ids ORDER BY company_id, platform")
    ).all()
    return {
        "companies": [tuple(row) for row in companies],
        "snapshots": [tuple(row) for row in snapshots],
        "audits": [tuple(row) for row in audits],
        "orders": [tuple(row) for row in orders],
        "platforms": [tuple(row) for row in platforms],
    }


def test_downgrade_succeeds_when_every_company_has_a_2gis_id(migrated_engine, postgres_url: str) -> None:
    factory = _factory(migrated_engine)
    started = datetime(2026, 10, 16, 9, 0, tzinfo=ALMATY)
    with factory() as session, session.begin():
        company = Company(id_2gis="70000001000000951", name="Kept", city="Алматы", source="test")
        session.add(company)
        session.flush()
        session.add(
            CompanyPlatformId(
                company_id=company.id,
                platform="2gis",
                external_id="70000001000000951",
                public_url="https://2gis.kz/almaty/firm/70000001000000951",
            )
        )
        snapshot = CardSnapshot(
            company_id=company.id,
            platform="2gis",
            match_status="found",
            url="https://2gis.kz/almaty/firm/70000001000000951",
            fields={"name": "Kept"},
            field_sources={"name": {"url": "https://2gis.kz/almaty/firm/70000001000000951"}},
            purpose="prospect",
            collected_at=started,
            collected_by="scout",
            status="verified",
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Audit(
                company_id=company.id,
                snapshot_ids=[snapshot.id],
                score_total=40,
                score_groups={"card": 40},
                derived={},
                discrepancies={},
                gaps={},
                status="verified",
            )
        )
        session.add(Order(company_id=company.id, kind="setup", amount_kzt=30000, stage="created"))
        company_id = company.id
    before = None
    with migrated_engine.connect() as conn:
        before = _fingerprint(conn)
    command.downgrade(alembic_config(postgres_url), "a10e5c4d6b21")
    with migrated_engine.connect() as conn:
        assert _revision(conn) == "a10e5c4d6b21"
        assert _nullable(conn) == "NO"
        assert _index_present(conn) is False
        assert _fingerprint(conn) == before
    with factory() as session:
        stored = session.get(Company, company_id)
        assert stored is not None
        assert stored.id_2gis == "70000001000000951"


def test_downgrade_refuses_and_keeps_rows_when_a_company_has_no_2gis_id(migrated_engine, postgres_url: str) -> None:
    factory = _factory(migrated_engine)
    started = datetime(2026, 10, 16, 9, 30, tzinfo=ALMATY)
    with factory() as session, session.begin():
        kept = Company(id_2gis="70000001000000952", name="Kept", city="Алматы", source="test")
        fresh = Company(id_2gis=None, name="Fresh", city="Алматы", source="test")
        session.add_all([kept, fresh])
        session.flush()
        session.add(
            CompanyPlatformId(
                company_id=fresh.id,
                platform="yandex",
                external_id="9521",
                public_url="https://yandex.kz/maps/org/fresh/9521",
            )
        )
        snapshot = CardSnapshot(
            company_id=fresh.id,
            platform="yandex",
            match_status="found",
            url="https://yandex.kz/maps/org/fresh/9521",
            fields={"name": "Fresh"},
            field_sources={"name": {"url": "https://yandex.kz/maps/org/fresh/9521"}},
            purpose="prospect",
            collected_at=started,
            collected_by="scout",
            status="collected",
        )
        session.add(snapshot)
        session.flush()
        session.add(
            Audit(
                company_id=fresh.id,
                snapshot_ids=[snapshot.id],
                score_total=10,
                score_groups={"card": 10},
                derived={},
                discrepancies={},
                gaps={},
                status="draft",
            )
        )
        session.add(Order(company_id=fresh.id, kind="setup", amount_kzt=30000, stage="created"))
    with migrated_engine.connect() as conn:
        before = _fingerprint(conn)
        assert _revision(conn) == "e5a9b3c20f41"
        assert _index_present(conn) is True
    command.downgrade(alembic_config(postgres_url), "c3e7a1b84d02")
    with migrated_engine.connect() as conn:
        assert _revision(conn) == "c3e7a1b84d02"
        assert _index_present(conn) is True
    with pytest.raises(RuntimeError, match="cannot be restored to NOT NULL"):
        command.downgrade(alembic_config(postgres_url), "a10e5c4d6b21")
    with migrated_engine.connect() as conn:
        assert _revision(conn) == "c3e7a1b84d02"
        assert _nullable(conn) == "YES"
        assert _index_present(conn) is True
        assert _fingerprint(conn) == before
        assert any(row[1] is None and row[2] == "Fresh" for row in before["companies"])


def accept_saved_sources(engine, tmp_path: Path) -> None:
    """Create platform-only companies from saved HTML. Does not open a map site."""
    factory = _factory(engine)
    with engine.connect() as conn:
        before_companies = {row[0] for row in conn.execute(text("SELECT id FROM companies")).all()}
        before_snapshots = {
            tuple(row)
            for row in conn.execute(
                text("SELECT id, company_id, platform, status, match_status FROM card_snapshots")
            ).all()
        }
        before_orders = conn.execute(text("SELECT count(*) FROM orders")).scalar_one()
        before_audits = conn.execute(text("SELECT count(*) FROM audits")).scalar_one()

    yandex_root = _saved(
        tmp_path / "yandex",
        {"yandex": _page("yandex", _candidate("901401", "E1014 Yandex Only", "улица Проверочная, 14", "8 701 999 14 01"))},
    )
    _run(engine, yandex_root, tmp_path / "yandex-out", ("yandex",))
    google_root = _saved(
        tmp_path / "google",
        {
            "google": _page(
                "google",
                '<a data-candidate="1" href="https://www.google.com/maps/place/E1014+Google/data=!4m2!3m1!1sChIJe1014google">'
                '<span data-field="name">E1014 Google Only</span>'
                '<span data-field="address">улица Проверочная, 15</span>'
                f'<span data-field="category">{SALON}</span>'
                '<span data-field="phones">8 701 999 14 02</span></a>',
            )
        },
    )
    _run(engine, google_root, tmp_path / "google-out", ("google",))
    firm = "7000000190140001"
    twogis_root = _saved(
        tmp_path / "twogis",
        {
            "2gis": _page(
                "2gis",
                f'<a data-candidate="1" href="https://2gis.kz/almaty/firm/{firm}">'
                '<span data-field="name">E1014 Firm Only</span>'
                '<span data-field="address">улица Проверочная, 16</span>'
                f'<span data-field="category">{SALON}</span>'
                '<span data-field="phones">8 701 999 14 03</span></a>',
            )
        },
    )
    card_dir = twogis_root / "pages" / "2gis"
    card_dir.mkdir()
    (card_dir / f"{firm}.html").write_text(
        "<html><body><article data-page=\"card\" data-platform=\"2gis\" "
        f'data-external-id="{firm}">'
        '<span data-field="name">E1014 Firm Only</span>'
        '<span data-field="address">улица Проверочная, 16</span>'
        f'<span data-field="category">{SALON}</span>'
        '<span data-field="phones">+7 701 999 14 03</span>'
        "</article></body></html>",
        encoding="utf-8",
    )
    _run(engine, twogis_root, tmp_path / "twogis-out", ("2gis",))
    linked_root = _saved(
        tmp_path / "linked",
        {
            "yandex": _page("yandex", _candidate("901404", "E1014 Linked", "улица Проверочная, 17", "8 701 999 14 04")),
            "google": _page(
                "google",
                '<a data-candidate="1" href="https://www.google.com/maps/place/E1014+Linked/data=!4m2!3m1!1sChIJe1014linked">'
                '<span data-field="name">E1014 Linked</span>'
                '<span data-field="address">улица Проверочная, 17</span>'
                f'<span data-field="category">{SALON}</span>'
                '<span data-field="phones">+7 701 999 14 04</span></a>',
            ),
        },
    )
    _run(engine, linked_root, tmp_path / "linked-out", ("yandex", "google"))
    conflict_root = _saved(
        tmp_path / "conflict",
        {
            "yandex": _page(
                "yandex",
                _candidate("901405", "E1014 Conflict", "улица Проверочная, 18", "8 701 999 14 05")
                + _candidate("901406", "E1014 Conflict", "улица Проверочная, 19", "8 701 999 14 05"),
            )
        },
    )
    _run(engine, conflict_root, tmp_path / "conflict-out", ("yandex",))
    repeat_root = _saved(
        tmp_path / "repeat",
        {"yandex": _page("yandex", _candidate("901407", "E1014 Repeat", "улица Проверочная, 20", "8 701 999 14 06"))},
    )
    _run(engine, repeat_root, tmp_path / "repeat-out", ("yandex",))
    with factory() as session, session.begin():
        first = session.scalar(
            select(CardSnapshot)
            .join(Company, Company.id == CardSnapshot.company_id)
            .where(Company.name == "E1014 Repeat")
        )
        assert first is not None
        first.status = "verified"
        repeat_snapshot_id = first.id
        repeat_company_id = first.company_id
    _run(engine, repeat_root, tmp_path / "repeat-again", ("yandex",))

    with factory() as session:
        yandex = session.scalar(select(Company).where(Company.name == "E1014 Yandex Only"))
        google = session.scalar(select(Company).where(Company.name == "E1014 Google Only"))
        firm_row = session.scalar(select(Company).where(Company.id_2gis == firm))
        linked_rows = list(session.scalars(select(Company).where(Company.name == "E1014 Linked")).all())
        conflict_rows = list(session.scalars(select(Company).where(Company.name == "E1014 Conflict")).all())
        repeat_rows = list(session.scalars(select(Company).where(Company.name == "E1014 Repeat")).all())
        assert yandex is not None and yandex.id_2gis is None
        assert google is not None and google.id_2gis is None
        assert firm_row is not None and firm_row.id_2gis == firm
        assert len(linked_rows) == 1 and linked_rows[0].id_2gis is None
        linked_ids = {
            row.platform: row.external_id
            for row in session.scalars(
                select(CompanyPlatformId).where(CompanyPlatformId.company_id == linked_rows[0].id)
            ).all()
        }
        assert linked_ids == {"yandex": "901404", "google": "ChIJe1014linked"}
        assert len(conflict_rows) == 1
        held = session.scalar(
            select(CollectionBatchItem).where(CollectionBatchItem.id_2gis == "trace:yandex:901406")
        )
        assert held is not None
        assert held.status == "blocked"
        assert held.company_id is None
        assert len(repeat_rows) == 1
        history = list(
            session.scalars(
                select(CardSnapshot)
                .where(CardSnapshot.company_id == repeat_company_id)
                .order_by(CardSnapshot.id)
            ).all()
        )
        assert [row.id for row in history][0] == repeat_snapshot_id
        assert history[0].status == "verified"
        assert len(history) == 2
        assert history[1].status == "collected"
        listed = list_companies(session, q="901401", limit=10, offset=0)
        assert len(listed["items"]) == 1
        assert listed["items"][0]["id"] == yandex.id
        assert listed["items"][0]["id_2gis_label"] == "Номер 2ГИС не указан"
        detail = company_detail(session, yandex.id)
        assert detail is not None
        assert detail["header"]["id_2gis_label"] == "Номер 2ГИС не указан"
        assert detail["header"]["source_platform"] == "yandex"
        assert detail["company"]["id_2gis"] is None

    with engine.connect() as conn:
        after_companies = {row[0] for row in conn.execute(text("SELECT id FROM companies")).all()}
        after_snapshots = {
            tuple(row)
            for row in conn.execute(
                text("SELECT id, company_id, platform, status, match_status FROM card_snapshots")
            ).all()
        }
        assert before_companies <= after_companies
        assert before_snapshots <= after_snapshots
        assert conn.execute(text("SELECT count(*) FROM orders")).scalar_one() == before_orders
        assert conn.execute(text("SELECT count(*) FROM audits")).scalar_one() == before_audits
        duplicates = conn.execute(
            text(
                """
                SELECT platform, external_id
                FROM company_platform_ids
                WHERE external_id IS NOT NULL
                GROUP BY platform, external_id
                HAVING count(*) > 1
                """
            )
        ).all()
        assert duplicates == []
        assert session_count(conn, "companies") == len(before_companies) + 6


def session_count(conn, table: str) -> int:
    return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_saved_source_acceptance_on_a_fresh_database(migrated_engine, tmp_path: Path) -> None:
    accept_saved_sources(migrated_engine, tmp_path)
    with migrated_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Company)).scalar_one() == 6
