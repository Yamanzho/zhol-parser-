"""Company links stay unresolved when another candidate is ambiguous. Saved pages only."""

from __future__ import annotations

from datetime import datetime

import zhol.browser.discovery.engine  # noqa: F401  # completes the identity import cycle
from alembic import command
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker

from tests.conftest import alembic_config
from zhol.agents.scout.identity import Profile, Ref, classify_links, relate
from zhol.agents.scout.identity_report import render
from zhol.agents.scout.identity_store import decide, relate_stored
from zhol.agents.scout.maps_selenium import MapPlace, _find_company
from zhol.agents.scout.match import Candidate, Identity, choose
from zhol.browser.discovery.types import Branch, PlatformEvidence
from zhol.db.models import CardSnapshot, Company, CompanyPlatformId
from zhol.timeutil import ALMATY

WHEN = datetime(2026, 10, 16, 9, 0, tzinfo=ALMATY)


def test_search_keeps_one_clear_card_beside_a_branch() -> None:
    identity = Identity("North", "улица Абая, 10", ["+7 701 000 00 01"])
    clear = Candidate("a", "1", "https://example/a", "North", "ул. Абая, 10", ["87010000001"])
    branch = Candidate("b", "2", "https://example/b", "North", "улица Гоголя, 5", ["87010000001"])
    decision = choose(identity, [clear, branch])
    assert decision.verdict == "same"
    assert decision.candidate is not None
    assert decision.candidate.candidate_id == "a"


def test_one_same_and_one_ambiguous_link_is_not_attached() -> None:
    action, _company, phone = classify_links(
        [("exact", "same", True), ("other", "ambiguous", False)]
    )
    assert action == "review"
    assert phone is False


def _factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _add(session, *, name, city, phone, address, platform, external_id, id_2gis=None):
    company = Company(
        id_2gis=id_2gis,
        name=name,
        city=city,
        niche="салон",
        source="identity-test",
        dnc=False,
    )
    session.add(company)
    session.flush()
    session.add(
        CompanyPlatformId(
            company_id=company.id,
            platform=platform,
            external_id=external_id,
            public_url=f"https://example.test/{external_id}",
        )
    )
    session.add(
        CardSnapshot(
            company_id=company.id,
            platform=platform,
            match_status="found",
            url=f"https://example.test/{external_id}",
            fields={"name": name, "address": address, "phones": [phone] if phone else []},
            field_sources={},
            purpose="prospect",
            collected_at=WHEN,
            collected_by="test",
            status="collected",
        )
    )
    session.flush()
    return company


def _place(**overrides) -> MapPlace:
    values = {
        "platform": "2gis",
        "external_id": "new-card",
        "name": "North",
        "city": "Алматы",
        "shown_city": "Алматы",
        "address": "улица Абая, 10",
        "address_reason": None,
        "phones": ("77010000001",),
        "phone_display": ("+7 701 000 00 01",),
        "phones_reason": None,
        "whatsapp": None,
        "whatsapp_reason": None,
        "instagram": None,
        "instagram_reason": None,
        "website": None,
        "website_reason": None,
        "rating": None,
        "rating_reason": None,
        "review_count": None,
        "review_reason": None,
        "category": "салон",
        "category_reason": None,
        "url": "https://example.test/new-card",
        "query": "салон",
        "collected_at": "2026-10-16T09:00:00+05:00",
    }
    values.update(overrides)
    return MapPlace(**values)


def _branch(name, address, phone, external_id) -> Branch:
    evidence = PlatformEvidence(
        "yandex",
        f"https://example.test/{external_id}",
        external_id,
        None,
        "2026-10-16T09:00:00+05:00",
        "saved",
        bool(phone),
    )
    return Branch(
        name=name,
        category="салон",
        subcategory=None,
        sector=None,
        address=address,
        city="Алматы",
        phones=(phone,) if phone else (),
        platforms={"yandex": evidence},
    )


def _card(session, *, name, city, phone, address, platform, id_2gis=None):
    """A company and one snapshot, without a platform id on that snapshot's platform."""
    company = Company(id_2gis=id_2gis, name=name, city=city, niche="салон", source="identity-test", dnc=False)
    session.add(company)
    session.flush()
    session.add(
        CardSnapshot(
            company_id=company.id,
            platform=platform,
            match_status="found",
            url=f"https://example.test/{platform}-{company.id}",
            fields={"name": name, "address": address, "phones": [phone] if phone else []},
            field_sources={},
            purpose="prospect",
            collected_at=WHEN,
            collected_by="test",
            status="collected",
        )
    )
    session.flush()
    return company


def test_the_same_phone_on_two_platforms_stays_one_company(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        stored = _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
            id_2gis="firm-1",
        )
        session.add(
            CardSnapshot(
                company_id=stored.id,
                platform="2gis",
                match_status="found",
                url="https://example.test/firm-1",
                fields={"name": "North", "address": "улица Абая, 10", "phones": ["77010000001"]},
                field_sources={},
                purpose="prospect",
                collected_at=WHEN,
                collected_by="test",
                status="collected",
            )
        )
        stored_id = stored.id
    with factory() as session:
        found, reason = _find_company(
            session,
            _place(
                platform="yandex",
                external_id="ya-1b",
                phones=("8 701 000 00 01",),
                phone_display=("+7 701 000 00 01",),
            ),
        )
        assert reason == "same_identity"
        assert found is not None and found.id == stored_id


def test_same_phone_and_address_is_one_company(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
            id_2gis="firm-2",
        )
    with factory() as session:
        found, reason = _find_company(session, _place(platform="yandex", external_id="ya-2"))
        assert reason == "same_identity"
        assert found is not None


def test_same_phone_at_another_address_is_not_merged(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Гоголя, 5",
            platform="2gis",
            external_id="firm-3",
            id_2gis="firm-3",
        )
    with factory() as session:
        found, reason = _find_company(session, _place(external_id="firm-3b", address="улица Абая, 10"))
        assert found is None
        assert reason == "ambiguous"


def test_same_name_and_different_phone_is_not_merged(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000002",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-4",
            id_2gis="firm-4",
        )
    with factory() as session:
        found, reason = _find_company(session, _place(external_id="firm-4b", phones=("77010000001",)))
        assert found is None
        assert reason == "ambiguous"
        relation = relate(
            Profile("North", "улица Абая, 10", ("77010000001",), ()),
            Profile("North", "улица Абая, 10", ("77010000002",), ()),
        )
        assert relation.kind == "ambiguous"


def test_two_plausible_companies_stay_unresolved(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-5a",
            id_2gis="firm-5a",
        )
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="ул. Абая, 10",
            platform="2gis",
            external_id="firm-5b",
            id_2gis="firm-5b",
        )
    with factory() as session:
        found, reason = _find_company(session, _place(platform="2gis", external_id="firm-5c"))
        assert found is None
        assert reason == "ambiguous"


def test_one_exact_and_one_ambiguous_candidate_is_not_attached(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="yandex",
            id_2gis="firm-6a",
        )
        _card(
            session,
            name="North",
            city="Алматы",
            phone="77010000002",
            address="улица Абая, 10",
            platform="yandex",
            id_2gis="firm-6b",
        )
    with factory() as session:
        found, reason = _find_company(session, _place(platform="yandex", external_id="ya-6"))
        assert found is None
        assert reason == "ambiguous"
        marked = relate_stored(
            session,
            "Алматы",
            "yandex",
            Profile("North", "улица Абая, 10", ("77010000001",), (Ref("yandex", "ya-6"),)),
        )
        assert classify_links(marked)[0] == "review"


def test_branches_that_share_a_phone_stay_separate(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-7a",
            id_2gis="firm-7a",
        )
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Гоголя, 5",
            platform="2gis",
            external_id="firm-7b",
            id_2gis="firm-7b",
        )
    with factory() as session:
        found, reason = _find_company(
            session,
            _place(platform="2gis", external_id="firm-7c", address="проспект Достык, 1"),
        )
        assert found is None
        assert reason == "ambiguous"


def test_a_known_platform_id_stays_on_its_company(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        owner = _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-8",
            id_2gis="firm-8",
        )
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000002",
            address="улица Абая, 10",
            platform="yandex",
            external_id="ya-8",
        )
        owner_id = owner.id
    with factory() as session:
        first, first_reason = _find_company(session, _place(external_id="firm-8"))
        second, second_reason = _find_company(session, _place(external_id="firm-8"))
        assert first_reason == "platform_id"
        assert second_reason == "platform_id"
        assert first is not None and second is not None
        assert first.id == second.id == owner_id


def test_a_missing_phone_does_not_confirm_the_link(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-9",
            id_2gis="firm-9",
        )
    with factory() as session:
        found, reason = _find_company(session, _place(external_id="firm-9b", phones=(), phone_display=()))
        assert found is None
        assert reason == "ambiguous"


def test_lookup_does_not_scan_every_company_in_the_city(migrated_engine) -> None:
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
        for number in range(30):
            _add(
                session,
                name=f"Other {number}",
                city="Алматы",
                phone=f"7702000{number:04d}",
                address=f"улица Другая, {number + 1}",
                platform="2gis",
                external_id=f"firm-other-{number}",
                id_2gis=f"firm-other-{number}",
            )
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    try:
        with factory() as session:
            found, reason = _find_company(session, _place(platform="yandex", external_id="ya-new"))
            assert reason == "same_identity"
            assert found is not None
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)
    combined = "\n".join(statements).lower()
    assert "company_match_keys" in combined
    assert "from companies where companies.city" not in combined
    assert combined.count("from companies") <= 2


def test_conflicting_stored_profiles_are_reported_and_left_in_place(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        company = _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-10",
            id_2gis="firm-10",
        )
        session.add(
            CardSnapshot(
                company_id=company.id,
                platform="google",
                match_status="found",
                url="https://example.test/google-10",
                fields={"name": "North", "address": "улица Абая, 10", "phones": ["77010000009"]},
                field_sources={},
                purpose="prospect",
                collected_at=WHEN,
                collected_by="test",
                status="collected",
            )
        )
        company_id = company.id
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Company))
        report = render(session)
        after = session.scalar(select(func.count()).select_from(Company))
        assert before == after
        assert f"Компания {company_id}" in report
        assert "не изменялись" in report
        assert session.get(Company, company_id) is not None


def test_match_key_migration_keeps_company_rows(migrated_engine, postgres_url: str) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        _add(
            session,
            name="Kept",
            city="Алматы",
            phone="77010000077",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-kept",
            id_2gis="firm-kept",
        )
    with migrated_engine.connect() as conn:
        before = conn.execute(text("SELECT id, id_2gis, name FROM companies ORDER BY id")).all()
        assert conn.execute(text("SELECT count(*) FROM company_match_keys")).scalar_one() > 0
    command.downgrade(alembic_config(postgres_url), "e5a9b3c20f41")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('public.company_match_keys')")).scalar_one() is None
        assert conn.execute(text("SELECT id, id_2gis, name FROM companies ORDER BY id")).all() == before
    command.upgrade(alembic_config(postgres_url), "head")
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "b6d2f4a81c30"
        assert conn.execute(text("SELECT id, id_2gis, name FROM companies ORDER BY id")).all() == before
        assert conn.execute(text("SELECT count(*) FROM company_match_keys")).scalar_one() > 0
