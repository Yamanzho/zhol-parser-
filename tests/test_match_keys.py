"""Match keys stay aligned with ORM writes. Direct SQL stays invisible until rebuild."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import func, select, text

from tests.conftest import alembic_config
from tests.test_identity_links import _card, _factory, _place
from zhol.agents.scout.identity_store import decide
from zhol.agents.scout.maps_selenium import _find_company
from zhol.agents.scout.match_index import rebuild_match_keys
from zhol.cli import cmd_rebuild_match_keys
from zhol.db.models import CardSnapshot, Company, CompanyMatchKey
from zhol.browser.discovery.types import Branch, PlatformEvidence


def _keys(session, company_id: int, kind: str) -> set[str]:
    rows = session.scalars(
        select(CompanyMatchKey.value).where(
            CompanyMatchKey.company_id == company_id,
            CompanyMatchKey.kind == kind,
        )
    ).all()
    return set(rows)


def _branch(phone: str, external_id: str) -> Branch:
    evidence = PlatformEvidence(
        "yandex",
        f"https://example.test/{external_id}",
        external_id,
        None,
        "2026-10-16T09:00:00+05:00",
        "saved",
        True,
    )
    return Branch(
        name="North",
        category="салон",
        subcategory=None,
        sector=None,
        address="улица Абая, 10",
        city="Алматы",
        phones=(phone,),
        platforms={"yandex": evidence},
    )


def test_orm_write_does_not_create_a_second_company(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
        )
    with factory() as session:
        found, reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-orm", phones=("77010000001",)),
        )
        outcome = decide(session, _branch("77010000001", "ya-orm-2"))
        assert reason == "same_identity"
        assert found is not None and found.name == "North"
        assert outcome.action == "attach"
        assert session.scalar(select(func.count()).select_from(Company)) == 1


def test_a_phone_change_does_not_keep_the_old_key(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        company = _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
        )
        snapshot = session.scalar(select(CardSnapshot).where(CardSnapshot.company_id == company.id))
        assert snapshot is not None
        snapshot.fields = {"name": "North", "address": "улица Абая, 10", "phones": ["77010000009"]}
    with factory() as session:
        assert _keys(session, company.id, "phone") == {"77010000009"}
        _old, old_reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-old", phones=("77010000001",)),
        )
        _new, new_reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-new", phones=("77010000009",)),
        )
        assert old_reason == "ambiguous"
        assert new_reason == "same_identity"
        assert session.scalar(select(func.count()).select_from(Company)) == 1


def test_deleting_a_snapshot_removes_its_phone_key(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        company = _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
        )
        snapshot = session.scalar(select(CardSnapshot).where(CardSnapshot.company_id == company.id))
        assert snapshot is not None
        session.delete(snapshot)
    with factory() as session:
        assert _keys(session, company.id, "phone") == set()
        assert _keys(session, company.id, "name")


def test_missing_keys_attach_again_after_rebuild(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        company = _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
        )
        session.execute(text("DELETE FROM company_match_keys WHERE company_id = :id"), {"id": company.id})
    with factory() as session:
        _found, reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-gap", phones=("77010000001",)),
        )
        assert reason is None
        assert session.scalar(select(func.count()).select_from(Company)) == 1
    with migrated_engine.begin() as connection:
        rebuild_match_keys(connection)
    with factory() as session:
        found, reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-gap", phones=("77010000001",)),
        )
        assert reason == "same_identity"
        assert found is not None and found.id == company.id
        assert session.scalar(select(func.count()).select_from(Company)) == 1


def test_direct_sql_stays_unmatched_until_rebuild(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with migrated_engine.begin() as connection:
        company_id = connection.execute(
            text("INSERT INTO companies (name, city, source) VALUES ('North', 'Алматы', 'sql') RETURNING id")
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO card_snapshots (
                    company_id, platform, match_status, fields, field_sources,
                    purpose, collected_at, collected_by, status
                )
                VALUES (
                    :id, 'yandex', 'found',
                    CAST(:fields AS jsonb), '{}'::jsonb,
                    'prospect', '2026-10-16T09:00:00+05', 'sql', 'collected'
                )
                """
            ),
            {
                "id": company_id,
                "fields": '{"name":"North","address":"улица Абая, 10","phones":["77010000001"]}',
            },
        )
        assert connection.execute(
            text("SELECT count(*) FROM company_match_keys WHERE company_id = :id"),
            {"id": company_id},
        ).scalar_one() == 0
    with factory() as session:
        _found, reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-sql", phones=("77010000001",)),
        )
        assert reason is None
    assert cmd_rebuild_match_keys(None, factory) == 0
    assert cmd_rebuild_match_keys(None, factory) == 0
    with factory() as session:
        found, reason = _find_company(
            session,
            _place(platform="yandex", external_id="ya-sql", phones=("77010000001",)),
        )
        assert reason == "same_identity"
        assert found is not None and found.id == company_id
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        orphans = session.execute(
            text(
                """
                SELECT count(*)
                FROM company_match_keys AS keys
                LEFT JOIN companies ON companies.id = keys.company_id
                WHERE companies.id IS NULL
                """
            )
        ).scalar_one()
        assert orphans == 0


def test_interrupted_upgrade_rolls_back_then_retries(migrated_engine, postgres_url: str, monkeypatch) -> None:
    cfg = alembic_config(postgres_url)
    command.downgrade(cfg, "e5a9b3c20f41")
    with migrated_engine.begin() as connection:
        connection.execute(text("INSERT INTO companies (name, city, source) VALUES ('North', 'Алматы', 'sql')"))
        before = connection.execute(text("SELECT count(*) FROM companies")).scalar_one()

    def boom(_connection):
        raise RuntimeError("interrupted")

    monkeypatch.setattr("zhol.agents.scout.match_index.rebuild_match_keys", boom)
    with pytest.raises(RuntimeError, match="interrupted"):
        command.upgrade(cfg, "head")
    monkeypatch.undo()

    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "e5a9b3c20f41"
        assert connection.execute(text("SELECT to_regclass('public.company_match_keys')")).scalar_one() is None
        assert connection.execute(text("SELECT count(*) FROM companies")).scalar_one() == before
        assert connection.execute(text("SELECT name FROM companies")).scalar_one() == "North"

    command.upgrade(cfg, "head")
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "b6d2f4a81c30"
        assert connection.execute(text("SELECT count(*) FROM companies")).scalar_one() == before
        assert connection.execute(text("SELECT name FROM companies")).scalar_one() == "North"
        assert connection.execute(text("SELECT count(*) FROM company_match_keys")).scalar_one() == 1
        plan = "\n".join(
            connection.execute(
                text(
                    """
                    EXPLAIN
                    SELECT company_id FROM company_match_keys
                    WHERE city = 'Алматы' AND kind = 'name' AND platform = '' AND value = 'north'
                    """
                )
            ).scalars()
        )
        assert "ix_company_match_keys_lookup" in plan
