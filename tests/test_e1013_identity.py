"""E10.13 platform-independent company identity. Saved pages only. No live map site."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from zhol.agents.scout.discover import run_discovery
from zhol.agents.scout.identity import Profile, Ref, relate
from zhol.agents.scout.identity_store import save_branch, save_discovered_company
from zhol.agents.scout.maps_selenium import MapPlace, _find_company, _stored_profile
from zhol.api.main import app
from zhol.browser.discovery.engine import walk_saved
from zhol.browser.discovery.types import Branch, PlatformEvidence
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, CompanyPlatformId

ROOT = Path("tests/fixtures/discovery/almaty-salon")
BLANK = ROOT / "pages" / "blank.png"
SALON = "Салон красоты"
TOKEN = "test-admin-token"
STUB = Path("tests/golden/review-engine/config")


def _factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _candidate(external: str, name: str, address: str, phone: str) -> str:
    return (
        f'<a data-candidate="{external}" href="https://yandex.kz/maps/org/place/{external}">'
        f'<span data-field="name">{name}</span>'
        f'<span data-field="address">{address}</span>'
        f'<span data-field="category">{SALON}</span>'
        f'<span data-field="phones">{phone}</span>'
        "</a>"
    )


def _page(platform: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f'<section data-page="search" data-platform="{platform}">'
        f"{body}</section></body></html>"
    )


def _saved(tmp_path: Path, pages: dict[str, str]) -> Path:
    root = tmp_path / "saved"
    starts = {}
    for platform, html in pages.items():
        folder = root / platform
        folder.mkdir(parents=True)
        (folder / "page-1.html").write_text(html, encoding="utf-8")
        starts[platform] = f"{platform}/page-1.html"
    (root / "manifest.yaml").write_text(
        yaml.safe_dump({"city": "Алматы", "category": SALON, "starts": starts}, allow_unicode=True),
        encoding="utf-8",
    )
    fixture_pages = root / "pages"
    fixture_pages.mkdir()
    (fixture_pages / "blank.png").write_bytes(BLANK.read_bytes())
    return root


def _run(engine, root: Path, tmp_path: Path, platforms: tuple[str, ...], limit: int | None = None):
    factory = _factory(engine)
    run_discovery(
        factory,
        city="Алматы",
        category=SALON,
        platforms=platforms,
        limit=limit,
        out=tmp_path / "out.xlsx",
        source_mode="fixture",
        root=root,
        fixtures_dir=root / "pages",
    )
    return factory


def test_name_and_address_without_a_shared_phone_is_ambiguous() -> None:
    same_place = Profile("North", "улица Абая, 10", (), ())
    assert relate(same_place, Profile("North", "улица Абая, 10", (), ())).kind == "ambiguous"
    different = relate(
        Profile("North", "улица Абая, 10", ("77010000001",), ()),
        Profile("North", "улица Абая, 10", ("77010000002",), ()),
    )
    assert different.kind == "ambiguous"
    assert different.phone_confirmed is False


def test_name_alone_is_not_a_match() -> None:
    left = Profile("North", None, (), ())
    right = Profile("North", None, (), ())
    assert relate(left, right).kind == "ambiguous"
    different = Profile("North Nails", "улица Абая, 10", (), ())
    same_place = Profile("North", "улица Абая, 10", (), ())
    assert relate(different, same_place).kind == "distinct"
    branches = Profile("North", "улица Абая, 10", ("77010000001",), ())
    other_house = Profile("North", "улица Абая, 20", ("77010000001",), ())
    assert relate(branches, other_house).kind == "ambiguous"
    other_phone = Profile("North", "улица Абая, 20", ("77010000002",), ())
    assert relate(branches, other_phone).kind == "other_address"
    same = Profile("North", "улица Абая, 10", ("77010000001",), (Ref("yandex", "1"),))
    again = Profile("North", "улица Абая, 10", ("77010000001",), (Ref("google", "ChIJ1"),))
    linked = relate(same, again)
    assert linked.kind == "same"
    assert linked.phone_confirmed is True


def test_new_2gis_company_keeps_its_firm_id(tmp_path: Path, migrated_engine) -> None:
    firm = "70000001000000801"
    root = _saved(
        tmp_path,
        {
            "2gis": _page(
                "2gis",
                '<a data-candidate="1" href="https://2gis.kz/almaty/firm/70000001000000801">'
                '<span data-field="name">Firm Salon</span>'
                '<span data-field="address">улица Абая, 10</span>'
                f'<span data-field="category">{SALON}</span>'
                '<span data-field="phones">8 701 000 08 01</span></a>',
            )
        },
    )
    card_dir = root / "pages" / "2gis"
    card_dir.mkdir()
    (card_dir / f"{firm}.html").write_text(
        "<html><body><article data-page=\"card\" data-platform=\"2gis\" "
        f'data-external-id="{firm}">'
        '<span data-field="name">Firm Salon</span>'
        '<span data-field="address">улица Абая, 10</span>'
        f'<span data-field="category">{SALON}</span>'
        '<span data-field="phones">+7 701 000 08 01</span>'
        "</article></body></html>",
        encoding="utf-8",
    )
    factory = _run(migrated_engine, root, tmp_path, ("2gis",))
    with factory() as session:
        company = session.scalar(select(Company).where(Company.id_2gis == firm))
        assert company is not None
        assert company.id > 0
        row = session.scalar(
            select(CompanyPlatformId).where(
                CompanyPlatformId.company_id == company.id,
                CompanyPlatformId.platform == "2gis",
            )
        )
        assert row is not None
        assert row.external_id == firm
        snapshot = session.scalar(select(CardSnapshot).where(CardSnapshot.company_id == company.id))
        assert snapshot is not None
        assert snapshot.status == "collected"
        assert snapshot.platform == "2gis"


def test_new_yandex_and_google_companies_have_no_2gis_id(tmp_path: Path, migrated_engine) -> None:
    yandex_root = _saved(
        tmp_path / "yandex",
        {"yandex": _page("yandex", _candidate("4101", "Yandex Only", "улица Абая, 10", "8 701 000 41 01"))},
    )
    factory = _run(migrated_engine, yandex_root, tmp_path / "yandex", ("yandex",))
    google_html = _page(
        "google",
        '<a data-candidate="1" href="https://www.google.com/maps/place/Google+Only/data=!4m2!3m1!1sChIJgoogleonly41">'
        '<span data-field="name">Google Only</span>'
        '<span data-field="address">улица Розыбакиева, 30</span>'
        f'<span data-field="category">{SALON}</span>'
        '<span data-field="phones">8 701 000 41 02</span></a>',
    )
    google_root = _saved(tmp_path / "google", {"google": google_html})
    _run(migrated_engine, google_root, tmp_path / "google", ("google",))
    with factory() as session:
        companies = list(session.scalars(select(Company).order_by(Company.id)).all())
        assert [company.id_2gis for company in companies] == [None, None]
        assert [company.name for company in companies] == ["Yandex Only", "Google Only"]
        ids = {
            (row.platform, row.external_id)
            for row in session.scalars(select(CompanyPlatformId)).all()
        }
        assert ("yandex", "4101") in ids
        assert ("google", "ChIJgoogleonly41") in ids
        snapshots = list(session.scalars(select(CardSnapshot)).all())
        assert {(row.platform, row.status, row.match_status) for row in snapshots} == {
            ("yandex", "collected", "found"),
            ("google", "collected", "found"),
        }
        yandex = snapshots[0] if snapshots[0].platform == "yandex" else snapshots[1]
        source = (yandex.field_sources or {})["name"]
        assert source["url"] == "https://yandex.kz/maps/org/place/4101"
        assert source["source_path"]


def test_same_company_on_two_platforms_is_one_row(tmp_path: Path, migrated_engine) -> None:
    root = _saved(
        tmp_path,
        {
            "yandex": _page("yandex", _candidate("5101", "Shared Salon", "улица Абая, 10", "8 701 000 51 01")),
            "google": _page(
                "google",
                '<a data-candidate="1" href="https://www.google.com/maps/place/Shared+Salon/data=!4m2!3m1!1sChIJshared5101">'
                '<span data-field="name">Shared Salon</span>'
                '<span data-field="address">улица Абая, 10</span>'
                f'<span data-field="category">{SALON}</span>'
                '<span data-field="phones">+7 701 000 51 01</span></a>',
            ),
        },
    )
    factory = _run(migrated_engine, root, tmp_path, ("yandex", "google"))
    with factory() as session:
        companies = list(session.scalars(select(Company)).all())
        assert len(companies) == 1
        assert companies[0].id_2gis is None
        ids = {
            row.platform: row.external_id
            for row in session.scalars(select(CompanyPlatformId).where(CompanyPlatformId.company_id == companies[0].id)).all()
        }
        assert ids == {"yandex": "5101", "google": "ChIJshared5101"}
        assert session.scalar(select(func.count()).select_from(CardSnapshot)) == 2


def test_same_name_at_different_addresses_stays_two_companies(tmp_path: Path, migrated_engine) -> None:
    root = _saved(
        tmp_path,
        {
            "yandex": _page(
                "yandex",
                _candidate("6101", "Branch Salon", "улица Абая, 10", "8 701 000 61 01")
                + _candidate("6102", "Branch Salon", "улица Абая, 20", "8 701 000 61 02"),
            )
        },
    )
    factory = _run(migrated_engine, root, tmp_path, ("yandex",))
    with factory() as session:
        companies = list(session.scalars(select(Company).order_by(Company.id)).all())
        assert len(companies) == 2
        assert len({company.id for company in companies}) == 2
        assert all(company.id_2gis is None for company in companies)
        assert session.scalar(select(func.count()).select_from(CompanyPlatformId)) == 2


def test_shared_phone_at_different_addresses_is_not_merged(tmp_path: Path, migrated_engine) -> None:
    root = _saved(
        tmp_path,
        {
            "yandex": _page(
                "yandex",
                _candidate("7101", "Switch Salon", "улица Абая, 10", "8 701 000 71 01")
                + _candidate("7102", "Switch Salon", "улица Абая, 20", "8 701 000 71 01"),
            )
        },
    )
    factory = _run(migrated_engine, root, tmp_path, ("yandex",))
    with factory() as session:
        companies = list(session.scalars(select(Company)).all())
        assert len(companies) == 1
        external = list(session.scalars(select(CompanyPlatformId.external_id)).all())
        assert external == ["7101"]
        blocked = session.scalar(select(CollectionBatchItem).where(CollectionBatchItem.status == "blocked"))
        assert blocked is not None
        assert blocked.company_id is None
        assert (blocked.detail or {}).get("reason") == "ambiguous"
        held = session.scalar(
            select(CardSnapshot).where(CardSnapshot.company_id == companies[0].id, CardSnapshot.match_status == "not_found")
        )
        assert held is not None
        assert ((held.field_sources or {}).get("_collection") or {}).get("hold") == "ambiguous_match"


def test_repeated_collection_keeps_snapshot_history(tmp_path: Path, migrated_engine) -> None:
    root = _saved(
        tmp_path,
        {"yandex": _page("yandex", _candidate("8101", "Repeat Salon", "улица Абая, 10", "8 701 000 81 01"))},
    )
    factory = _run(migrated_engine, root, tmp_path, ("yandex",))
    with factory() as session, session.begin():
        snapshot = session.scalar(select(CardSnapshot))
        assert snapshot is not None
        snapshot.status = "verified"
        first_id = snapshot.id
        company_id = snapshot.company_id
    _run(migrated_engine, root, tmp_path / "again", ("yandex",))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        rows = list(session.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company_id).order_by(CardSnapshot.id)).all())
        assert [row.id for row in rows][0] == first_id
        assert rows[0].status == "verified"
        assert len(rows) == 2
        assert rows[1].status == "collected"
        assert rows[1].platform == "yandex"


def test_ambiguous_name_and_address_with_two_phones_is_not_merged(tmp_path: Path, migrated_engine) -> None:
    root = _saved(
        tmp_path,
        {
            "yandex": _page(
                "yandex",
                _candidate("9101", "Dual Phone", "улица Абая, 10", "8 701 000 91 01")
                + _candidate("9102", "Dual Phone", "улица Абая, 10", "8 701 000 91 02"),
            )
        },
    )
    factory = _run(migrated_engine, root, tmp_path, ("yandex",))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        assert list(session.scalars(select(CompanyPlatformId.external_id)).all()) == ["9101"]
        blocked = session.scalar(select(CollectionBatchItem).where(CollectionBatchItem.id_2gis == "trace:yandex:9102"))
        assert blocked is not None
        assert blocked.status == "blocked"


def test_collection_limit_counts_any_platform_and_still_attaches(tmp_path: Path, migrated_engine) -> None:
    root = _saved(
        tmp_path,
        {
            "yandex": _page(
                "yandex",
                _candidate("9201", "Limited Salon", "улица Абая, 10", "8 701 000 92 01")
                + _candidate("9202", "Other Salon", "улица Розыбакиева, 5", "8 701 000 92 02"),
            ),
            "google": _page(
                "google",
                '<a data-candidate="1" href="https://www.google.com/maps/place/Limited+Salon/data=!4m2!3m1!1sChIJlimited9201">'
                '<span data-field="name">Limited Salon</span>'
                '<span data-field="address">улица Абая, 10</span>'
                f'<span data-field="category">{SALON}</span>'
                '<span data-field="phones">8 701 000 92 01</span></a>',
            ),
        },
    )
    walked = walk_saved(
        root,
        city="Алматы",
        category=SALON,
        platforms=("yandex", "google"),
        source="fixture",
        evidence_dir=tmp_path / "evidence",
        limit=1,
    )
    assert len(walked.branches) == 1
    assert set(walked.branches[0].platforms) == {"yandex", "google"}
    assert any(item["reason"] == "limit" and item["external_id"] == "9202" for item in walked.progress["unmatched"])
    factory = _run(migrated_engine, root, tmp_path, ("yandex", "google"), limit=1)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        ids = {row.platform for row in session.scalars(select(CompanyPlatformId)).all()}
        assert ids == {"yandex", "google"}


def test_concurrent_inserts_keep_one_external_id(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city="Алматы",
            category=SALON,
            platforms=["yandex"],
            item_limit=None,
            source_mode="fixture",
            status="running",
            out_path="out.xlsx",
            catalog_path="saved",
            fixtures_dir="pages",
        )
        session.add(batch)
        session.flush()
        batch_id = batch.id
    evidence = PlatformEvidence("yandex", "https://yandex.kz/maps/org/solo/9301", "9301", None, "2026-10-16T00:00:00+05:00", "saved", True)
    branch = Branch(
        name="Solo Salon",
        category=SALON,
        subcategory=None,
        sector=None,
        address="улица Абая, 10",
        city="Алматы",
        phones=("77010009301",),
        platforms={"yandex": evidence},
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def work() -> None:
        barrier.wait()
        try:
            save_discovered_company(factory, batch_id=batch_id, branch=branch, source_mode="fixture")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        assert session.scalar(select(func.count()).select_from(CompanyPlatformId)) == 1
        assert session.scalar(select(CompanyPlatformId.external_id)) == "9301"


def test_google_snapshot_address_does_not_attach_a_2gis_firm(migrated_engine) -> None:
    factory = _factory(migrated_engine)
    earlier = datetime(2026, 10, 1, tzinfo=timezone.utc)
    later = earlier + timedelta(days=2)
    with factory() as session, session.begin():
        company = Company(id_2gis="70000001000000901", name="Shared Salon", city="Алматы", source="test")
        session.add(company)
        session.flush()
        session.add(
            CompanyPlatformId(
                company_id=company.id,
                platform="2gis",
                external_id="70000001000000901",
                public_url="https://2gis.kz/almaty/firm/70000001000000901",
            )
        )
        session.add(
            CompanyPlatformId(
                company_id=company.id,
                platform="google",
                external_id="ChIJotherstreet",
                public_url="https://www.google.com/maps/place/other",
            )
        )
        session.add(
            CardSnapshot(
                company_id=company.id,
                platform="2gis",
                match_status="found",
                url="https://2gis.kz/almaty/firm/70000001000000901",
                fields={"name": "Shared Salon", "address": "улица Абая, 10", "phones": ["77010000001"]},
                field_sources={},
                purpose="prospect",
                collected_at=earlier,
                collected_by="test",
                status="collected",
            )
        )
        session.add(
            CardSnapshot(
                company_id=company.id,
                platform="google",
                match_status="found",
                url="https://www.google.com/maps/place/other",
                fields={"name": "Shared Salon", "address": "улица Розыбакиева, 30", "phones": ["77010000002"]},
                field_sources={},
                purpose="prospect",
                collected_at=later,
                collected_by="test",
                status="collected",
            )
        )
        batch = CollectionBatch(
            city="Алматы",
            category=SALON,
            platforms=["2gis"],
            item_limit=None,
            source_mode="fixture",
            status="running",
            out_path="out.xlsx",
            catalog_path="saved",
            fixtures_dir="pages",
        )
        session.add(batch)
        session.flush()
        batch_id = batch.id
        original_id = company.id
    with factory() as session:
        stored = session.get(Company, original_id)
        assert stored is not None
        profile = _stored_profile(session, stored, "2gis")
        assert profile.address == "улица Абая, 10"
        assert "77010000002" not in profile.phones
        place = MapPlace(
            platform="2gis",
            external_id="70000001000000902",
            name="Shared Salon",
            city="Алматы",
            shown_city="Алматы",
            address="улица Розыбакиева, 30",
            address_reason=None,
            phones=("77010000002",),
            phone_display=("+7 701 000 00 02",),
            phones_reason=None,
            whatsapp=None,
            whatsapp_reason=None,
            instagram=None,
            instagram_reason=None,
            website=None,
            website_reason=None,
            rating=None,
            rating_reason=None,
            review_count=None,
            review_reason=None,
            category=SALON,
            category_reason=None,
            url="https://2gis.kz/almaty/firm/70000001000000902",
            query=SALON,
            collected_at="2026-10-16T00:00:00+05:00",
        )
        found, _reason = _find_company(session, place)
        assert found is None
    evidence = PlatformEvidence(
        "2gis",
        "https://2gis.kz/almaty/firm/70000001000000902",
        "70000001000000902",
        None,
        "2026-10-16T00:00:00+05:00",
        "saved",
        True,
    )
    branch = Branch(
        name="Shared Salon",
        category=SALON,
        subcategory=None,
        sector=None,
        address="улица Розыбакиева, 30",
        city="Алматы",
        phones=("77010000002",),
        firm_id="70000001000000902",
        platforms={"2gis": evidence},
    )
    with factory() as session, session.begin():
        stored_batch = session.get(CollectionBatch, batch_id)
        assert stored_batch is not None
        assert save_branch(session, stored_batch, branch, "fixture") == "created"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 2
        original = session.get(Company, original_id)
        assert original is not None
        assert original.id_2gis == "70000001000000901"
        created = session.scalar(select(Company).where(Company.id_2gis == "70000001000000902"))
        assert created is not None
        assert created.id != original_id
        google = session.scalar(
            select(CardSnapshot).where(CardSnapshot.company_id == original_id, CardSnapshot.platform == "google")
        )
        assert google is not None
        assert google.fields["address"] == "улица Розыбакиева, 30"


def test_company_page_does_not_treat_a_missing_2gis_id_as_an_error(tmp_path: Path, migrated_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _saved(
        tmp_path,
        {"yandex": _page("yandex", _candidate("9401", "Page Salon", "улица Абая, 10", "8 701 000 94 01"))},
    )
    _run(migrated_engine, root, tmp_path, ("yandex",))
    monkeypatch.setenv("ZHOL_ROOT", str(Path(".").resolve()))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    with TestClient(app) as client:
        listed = client.get("/api/v1/admin/companies", headers={"Authorization": f"Bearer {TOKEN}"})
        assert listed.status_code == 200
        body = listed.json()
        row = body["items"][0]
        assert row["id_2gis"] is None
        assert row["id_2gis_label"] == "Номер 2ГИС не указан"
        assert row["source_platform"] == "yandex"
        assert row["source_platform_label"] == "Яндекс Карты"
        assert row["matching_status"] == "не все площадки проверены"
        assert {item["platform"] for item in row["missing_platforms"]} == {"2gis", "google"}
        assert row["review_needed"] is False
        assert "ошибка" not in row["id_2gis_label"].casefold()
        detail = client.get(f"/api/v1/admin/companies/{row['id']}", headers={"Authorization": f"Bearer {TOKEN}"})
        assert detail.status_code == 200
        header = detail.json()["header"]
        assert header["id_2gis_label"] == "Номер 2ГИС не указан"
        assert header["platform_identities"][0]["external_id"] == "9401"
        found = client.get("/api/v1/admin/companies", headers={"Authorization": f"Bearer {TOKEN}"}, params={"q": "9401"})
        assert found.json()["items"][0]["id"] == row["id"]


