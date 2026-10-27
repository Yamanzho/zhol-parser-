from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.collect import collect_company
from zhol.agents.scout.extract import extract_card
from zhol.agents.scout.persist import save_verified_services
from zhol.agents.scout.pilot import run_visible
from zhol.browser.config import load_platforms
from zhol.browser.fixtures import LoadedPage
from zhol.browser.page import AccessRestricted, PageDocument
from zhol.browser.twogis.access import ACCESS_BLOCKED, ROBOT_CHECK
from zhol.browser.twogis.capture import open_sequence
from zhol.browser.twogis.visible import parse_visible, service_names
from zhol.db.models import CardSnapshot, Company, CompanyProfile
from zhol.export.mailing import build_row, classify, load_categories, services_cell

BADI = "70000001025141518"
VELARA = "70000001109698727"
CARD = """Montebello
Парикмахерская
4.9
0 отзывов
Бостандыкский район, Алматы, 050046
Ежедневно с 08:00 до 20:00
desk@badi.example
"""
INFO = "Салон работает по записи и принимает гостей ежедневно в этом зале."
PRICES = "Стрижка\n5 000 ₸\nОкрашивание\n8 000 ₸\nMontebello\n1 000 ₸\n"


def _parse(text: str, **kwargs: object):
    spec = load_platforms()["platforms"]["2gis"]["live"]
    return parse_visible(
        text,
        name=kwargs.get("name"),  # type: ignore[arg-type]
        hrefs=list(kwargs.get("hrefs") or []),  # type: ignore[arg-type]
        patterns=spec["patterns"],
        labels=spec["labels"],
        prices_text=kwargs.get("prices_text"),  # type: ignore[arg-type]
        price_phrases=spec["price_phrases"],
        reviews_text=kwargs.get("reviews_text"),  # type: ignore[arg-type]
        info_text=kwargs.get("info_text"),  # type: ignore[arg-type]
    )


def test_category_and_subcategory_come_from_the_rubric_line() -> None:
    fields, notes = _parse(CARD, name="Montebello", info_text=INFO, prices_text=PRICES)
    table = load_categories()
    sector, category, subcategory = classify([fields["category"]], table)
    assert fields["category"] == "Парикмахерская"
    assert notes["category"] == "shown"
    assert (sector, category, subcategory) == ("Красота", "Салон красоты", "Парикмахерская")
    assert fields["district"] == "Бостандыкский район"
    assert fields["city"] == "Алматы"
    assert fields["email"] == "desk@badi.example"
    assert fields["hours"] == "Ежедневно с 08:00 до 20:00"
    assert fields["description"] == INFO
    assert notes["service_names"] == "Стрижка; Окрашивание"


def test_several_priced_names_are_kept_and_the_company_name_is_not() -> None:
    names, why = service_names(PRICES, "Montebello")
    assert why == "shown"
    assert names == ["Стрижка", "Окрашивание"]
    names, why = service_names("Маникюр 5000 ₸\nСтрижка 7000 тенге", "Badi")
    assert names == ["Маникюр", "Стрижка"]
    assert service_names("Montebello\n5 000 ₸", "Montebello") == ([], "not_shown")
    assert service_names("Фото прайса", "Montebello") == ([], "not_shown")


def test_one_decimal_rating_survives_a_tab_badge_and_two_decimals_do_not() -> None:
    fields, notes = _parse("Badi\nСалон красоты\n4.8\nЦены\n3\n", name="Badi")
    assert fields["rating"] == "4.8"
    assert notes["rating"] == "shown"
    fields, notes = _parse("Badi\n4.8\n4.9\n", name="Badi")
    assert "rating" not in fields and notes["rating"] == "ambiguous"


def test_hours_ignore_invisible_characters_and_ads_do_not_supply_a_phone() -> None:
    text = "Badi\nСалон красоты\n\u200bЕжедневно с 09:00 до 22:00\u200bпо предварительной записи\nПохожие организации\n+7 747 196 96 92\n"
    fields, notes = _parse(text, name="Badi")
    assert fields["hours"] == "Ежедневно с 09:00 до 22:00 по предварительной записи"
    assert "phones" not in fields and notes["phones"] == "not_shown"
    assert "has_phone" not in fields
    assert "is_ad" not in fields
    advertised = _parse("Velara\nСалон красоты\n4.8\nПохожие организации\nРеклама\n+7 747 196 96 92\n", name="Velara")
    assert "is_ad" not in advertised[0] and "phones" not in advertised[0] and advertised[0]["rating"] == "4.8"


def test_unknown_and_ambiguous_rubrics_stay_empty() -> None:
    unknown, notes = _parse("Badi\nПланетарий\n4.8\n", name="Badi")
    ambiguous, other = _parse("Badi\nсалон\n4.8\n", name="Badi")
    split, split_notes = _parse("Badi\nСалон красоты, Ресторан\n4.8\n", name="Badi")
    assert unknown["category"] == "Планетарий" and notes["category"] == "shown"
    assert "category" not in ambiguous and other["category"] == "ambiguous"
    assert "category" not in split and split_notes["category"] == "ambiguous"
    assert classify(["Планетарий"], load_categories()) == (None, None, None)


def test_missing_fields_stay_null_and_a_shown_zero_stays_zero() -> None:
    fields, notes = _parse("Badi\nСалон красоты\n0 отзывов\n", name="Badi")
    assert fields["reviews_count"] == "0"
    assert "photos_count" not in fields
    assert notes["photos_count"] == "not_shown"
    assert "email" not in fields and notes["email"] == "not_shown"
    assert "hours" not in fields and notes["hours"] == "not_shown"
    assert "district" not in fields and notes["district"] == "not_shown"
    assert notes["description"] == "not_shown"
    assert fields.get("email") != 0 and fields.get("hours") != "0"


def test_extracted_fields_keep_source_and_excel_reads_them() -> None:
    fields, notes = _parse(CARD, name="Montebello", info_text=INFO, prices_text=PRICES)
    cfg = load_platforms()["platforms"]["2gis"]
    document = PageDocument(page="card", platform="2gis", external_id=BADI, fields=fields, hrefs={}, notes=notes)
    parsed, sources = extract_card(
        "2gis",
        document,
        url=f"https://2gis.kz/almaty/firm/{BADI}",
        seen_at="2026-10-11T12:00:00+00:00",
        screenshot="shot.png",
        raw_path="page.html",
        code_fields=list(cfg["code_fields"]),
        model_fields=list(cfg["model_fields"]),
    )
    assert parsed["reviews_count"] == 0
    assert parsed["photos_count"] is None
    assert parsed["category"] == "Парикмахерская"
    for key in ("category", "district", "email", "description", "hours"):
        assert sources[key]["read_by"] == "code"
        assert sources[key]["url"].endswith(BADI)
        assert sources[key]["seen_at"]
        assert sources[key]["screenshot"] == "shot.png"
    row = build_row(
        company_id=BADI,
        name="Montebello",
        city="Алматы",
        niche="салон",
        source="etalon_2026_09_30",
        whatsapp=None,
        instagram=None,
        snapshots={
            "2gis": type("Snap", (), {
                "fields": parsed,
                "url": f"https://2gis.kz/almaty/firm/{BADI}",
                "screenshot_path": "shot.png",
                "status": "collected",
                "match_status": "found",
                "collected_at": None,
                "fail_reason": None,
                "field_sources": sources,
            })()
        },
        services=[{"name": name, "needs_confirmation": False} for name in ("Стрижка", "Окрашивание")],
        score=70,
        discrepancies=[],
        categories=load_categories(),
    )
    assert row["Business Subcategory"] == "Парикмахерская"
    assert row["Services / Activities"] == "Стрижка; Окрашивание"
    assert services_cell([{"name": "Montebello", "needs_confirmation": True}]) is None
    assert row["District"] == "Бостандыкский район"
    assert row["Email"] == "desk@badi.example"
    assert row["2GIS Description"] == INFO
    assert row["2GIS Opening Hours"] == "Ежедневно с 08:00 до 20:00"
    assert row["2GIS Review Count"] == 0
    assert row["2GIS Photo Count"] is None


def test_rerun_does_not_create_another_company_or_profile(migrated_engine: Engine) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    document = PageDocument(
        page="card",
        platform="2gis",
        external_id=BADI,
        fields={"name": "Badi", "category": "Салон красоты"},
        hrefs={},
        notes={"category": "shown", "service_names": "Стрижка; Окрашивание"},
    )
    page = LoadedPage("https://2gis.kz/almaty/firm/" + BADI, "<html></html>", "page.html", "page.html", "shot.png", "card", document)

    class Ready:
        def card(self, platform: str, external_id: str, url: str) -> LoadedPage:
            return page

        def search(self, platform: str, id_2gis: str, url: str) -> LoadedPage:
            raise AssertionError(platform)

    with factory() as session, session.begin():
        assert save_verified_services(session, BADI, ["Стрижка"], forbidden=[]) == "компании нет в базе, услуги не записаны"
        assert session.scalar(select(func.count()).select_from(Company)) == 0
        company = Company(id_2gis=BADI, name="Badi", source="test", city="Алматы")
        session.add(company)
        session.flush()
        company_id = company.id
        assert save_verified_services(session, BADI, ["Badi"], forbidden=["Badi"]) == "услуги не показаны"
        cfg = load_platforms()
        for _ in range(2):
            collect_company(
                session,
                company=company,
                purpose="prospect",
                collection_key=f"e2-2gis:{BADI}",
                pages=Ready(),  # type: ignore[arg-type]
                cfg=cfg,
                platforms=("2gis",),
            )
            save_verified_services(session, BADI, ["Стрижка", "Окрашивание"], forbidden=["Badi"])
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        snapshots = session.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company_id, CardSnapshot.platform == "2gis")).all()
        profiles = session.scalars(select(CompanyProfile).where(CompanyProfile.company_id == company_id)).all()
        assert len(snapshots) == 1
        assert snapshots[0].fields["category"] == "Салон красоты"
        assert snapshots[0].field_sources["category"]["read_by"] == "code"
        assert len(profiles) == 1
        assert [item["name"] for item in profiles[0].data["services"]] == ["Стрижка", "Окрашивание"]


def test_robot_check_stops_the_second_card_and_does_not_export(tmp_path: Path) -> None:
    opened: list[str] = []

    def reader(url: str, folder: Path, spec: dict, *, headed: bool, pause_seconds: float) -> LoadedPage:
        assert headed is True
        assert pause_seconds > 0
        opened.append(url)
        raise AccessRestricted(
            "access=robot_check outcome=access_blocked",
            screenshot=str(folder / "screenshot.png"),
            url=url,
            access=ROBOT_CHECK,
            outcome=ACCESS_BLOCKED,
        )

    exported: list[str] = []
    report = tmp_path / "e2.md"
    code = run_visible(
        [BADI, VELARA],
        output=tmp_path / "out",
        report_path=report,
        reader=reader,
        exporter=lambda: exported.append("xlsx") or "xlsx",
    )
    text = report.read_text(encoding="utf-8")
    assert code == 2
    assert opened == [f"https://2gis.kz/almaty/firm/{BADI}"]
    assert exported == []
    assert "не читались" in text
    assert "Не открывалась" in text
    assert VELARA in text
    assert "файл не перезаписан" in text


def test_open_sequence_does_not_call_the_next_reader_after_a_block(tmp_path: Path) -> None:
    calls: list[str] = []

    def reader(url: str, folder: Path) -> LoadedPage:
        calls.append(url)
        raise AccessRestricted("blocked", url=url, access=ROBOT_CHECK, outcome=ACCESS_BLOCKED)

    results = open_sequence(
        [(f"https://2gis.kz/almaty/firm/{BADI}", tmp_path / BADI), (f"https://2gis.kz/almaty/firm/{VELARA}", tmp_path / VELARA)],
        reader,
    )
    assert calls == [f"https://2gis.kz/almaty/firm/{BADI}"]
    assert results[0].error is not None
    assert len(results) == 1
