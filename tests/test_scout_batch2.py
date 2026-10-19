from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.golden import run_extract, run_match, run_scout_fields
from zhol.agents.scout.pilot import compare_reference
from zhol.browser.config import load_platforms
from zhol.browser.twogis.visible import parse_visible
from zhol.jobs import claim, enqueue
from zhol.worker import serve
from zhol.db.models import Job

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _live():
    return load_platforms()["platforms"]["2gis"]["live"]


def _parse(text: str, **kwargs: object):
    spec = _live()
    return parse_visible(
        text,
        name=kwargs.get("name"),  # type: ignore[arg-type]
        hrefs=list(kwargs.get("hrefs") or []),  # type: ignore[arg-type]
        patterns=spec["patterns"],
        labels=spec["labels"],
        prices_text=kwargs.get("prices_text"),  # type: ignore[arg-type]
        price_phrases=spec["price_phrases"],
        reviews_text=kwargs.get("reviews_text"),  # type: ignore[arg-type]
    )


def test_visible_parser_keeps_a_shown_zero_and_drops_ambiguous_counts() -> None:
    fields, notes = _parse("0 отзывов\n10 фото\n20 фото\n", name="Nord")
    assert fields["reviews_count"] == "0"
    assert "photos_count" not in fields
    assert notes["photos_count"] == "ambiguous"
    assert "rating" not in fields
    assert notes["rating"] == "not_shown"


def test_visible_parser_reads_labeled_counts_and_does_not_invent_a_phone() -> None:
    fields, notes = _parse("4.9\n1078 оценок\n774 отзыва\n333 фото\n1 филиал\nWhatsApp\n", name="Montebello")
    assert fields["name"] == "Montebello"
    assert fields["rating"] == "4.9"
    assert fields["ratings_count"] == "1078"
    assert fields["reviews_count"] == "774"
    assert fields["photos_count"] == "333"
    assert fields["branches"] == "1"
    assert fields["has_whatsapp"] == "1"
    assert "phones" not in fields
    assert notes["phones"] == "not_shown"
    assert "has_phone" not in fields


def test_tab_badge_count_after_the_label_is_visible_and_a_masked_phone_is_not() -> None:
    fields, notes = _parse("Отзывы\n775\nФото\n334\n+7‒776‒111... Показать телефон\n", name="Montebello")
    assert fields["reviews_count"] == "775"
    assert fields["photos_count"] == "334"
    assert "phones" not in fields
    assert notes["phones"] == "not_shown"


def test_prices_stay_empty_until_the_tab_text_says_so() -> None:
    fields, notes = _parse("Салон\n", name="Nord", prices_text=None)
    assert "prices" not in fields
    assert notes["prices"] == "not_shown"
    fields, notes = _parse("Салон\n", name="Nord", prices_text="Маникюр 5000 ₸\nСтрижка 7000 тенге")
    assert fields["prices"] == "company_price"
    fields, notes = _parse("Салон\n", name="Nord", prices_text="Здесь пока нет цен")
    assert fields["prices"] == "empty"


def test_reference_comparison_separates_missing_from_changed() -> None:
    diffs = compare_reference(
        {"rating": 4.9, "photos_count": 10, "name": "Montebello"},
        {"2гис_рейтинг": "4.9", "2гис_фото": "333", "название": "Montebello", "2гис_цены": "пусто"},
    )
    kinds = {item["field"]: item["kind"] for item in diffs}
    assert kinds["photos_count"] == "different"
    assert kinds["prices"] == "missing"
    assert "rating" not in kinds
    assert "name" not in kinds


def test_golden_sets_pass_offline() -> None:
    extract = run_extract(ROOT)
    match = run_match(ROOT)
    fields = run_scout_fields(ROOT)
    assert extract.ok, extract.problems
    assert match.ok, match.problems
    assert fields.ok, fields.problems
    assert extract.unsupported == []
    assert match.unsupported
    assert fields.unsupported
    assert extract.passed == 10
    assert match.passed == 10
    assert fields.passed == 10
    assert fields.caught == 5


def test_worker_claims_only_its_scout_jobs(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        other = enqueue(session, "manager.digest", {"slot": "2026-11-02"}, queue="main")
        verify = enqueue(session, "snapshot.verify", {"snapshot_ids": [], "company_id": 1}, queue="main")
        foreign = enqueue(session, "reviews.collect", {}, queue="browser")
    serve(factory, "main", notifier=lambda _text: None, poll_seconds=0, max_cycles=1)
    with factory() as session:
        assert session.get(Job, other).status == "queued"
        assert session.get(Job, verify).status == "done"
        assert session.get(Job, foreign).status == "queued"
    with factory() as session, session.begin():
        assert claim(session, "browser", types=("scout.collect", "review.snapshot_recheck")) is None
