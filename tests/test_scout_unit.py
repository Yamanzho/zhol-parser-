from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zhol.agents.scout.extract import extract_card
from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.formula import find_formulas
from zhol.agents.scout.match import Candidate, Identity, choose, match_status
from zhol.agents.scout.normalize import addresses_same, names_compatible, normalize_phone, phones_overlap
from zhol.agents.scout.sample import selected_for_recheck
from zhol.browser.config import load_platforms
from zhol.browser.live import LiveCollectionDisabled, fetch_public, wait_seconds
from zhol.browser.yandex.public import read_card, read_search
from zhol.cli import main
from zhol.guards import unseen_must_be_null

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def test_names_addresses_and_phones() -> None:
    assert names_compatible("«AOE» Beauty", "салон aoe beauty")
    assert names_compatible("АОЕ", "AOE")
    assert names_compatible("Монтебелло", "Montebello")
    assert not names_compatible("AOE", "AOE Nails")
    assert not names_compatible("АОЕ Нейлс", "AOE")
    assert addresses_same("улица Абая, 10", "ул. Абая, 10")
    assert addresses_same("проспект Абая, 10", "пр-т Абая, 10")
    assert not addresses_same("улица Абая, 10", "проспект Абая, 10")
    assert not addresses_same("улица Абая, 10", "улица Абая, 12")
    assert phones_overlap(["+7 701 000 00 11"], ["8 701 000 00 11"])
    assert normalize_phone("87010000011") == "77010000011"
    assert not phones_overlap(["+7 701 000 00 11"], ["+7 702 999 00 11"])


def test_match_rules_do_not_merge_uncertain_candidates() -> None:
    identity = Identity("AOE Beauty", "улица Абая, 10", ["+7 701 000 00 11"])
    same = Candidate("1", "100011", "https://yandex.example/1", "AOE", "ул. Абая, 10", ["87010000011"])
    other = Candidate("2", "100099", "https://yandex.example/2", "AOE Beauty", "улица Абая, 99", ["+7 777 000 00 00"])
    stranger = Candidate("3", "100077", "https://yandex.example/3", "Nord", "улица Гоголя, 5", ["+7 701 000 00 99"])
    assert choose(identity, [stranger, same, other]).verdict == "same"
    assert choose(identity, [other, stranger]).verdict == "other_address"
    assert choose(identity, [stranger]).verdict == "not_match"
    twin = Candidate("4", "100012", "https://yandex.example/4", "AOE Beauty", "ул. Абая, 10", ["+7 701 000 00 11"])
    assert choose(identity, [same, twin]).verdict == "ambiguous"


def test_shared_phone_different_house_is_never_merged() -> None:
    identity = Identity("Montebello", "проспект Абая, 164/6", ["+7 776 111 25 85"])
    other_house = Candidate("b", "2", "https://yandex.example/b", "Montebello", "улица Гоголя, 5", ["+7 776 111 25 85"])
    decision = choose(identity, [other_house])
    assert decision.verdict == "ambiguous"
    assert decision.candidate is None
    assert decision.phone_match is False
    assert match_status(decision.verdict) == "not_found"


def test_network_matches_are_escalated_instead_of_merged() -> None:
    identity = Identity("Montebello", "проспект Абая, 164/6", ["+7 776 111 25 85"])
    other_branch = Candidate("b", "2", "https://yandex.example/b", "Montebello", "улица Гоголя, 5", ["+7 776 111 25 85"])
    assert choose(identity, [other_branch]).verdict == "ambiguous"
    lookalike = Candidate("n", "3", "https://yandex.example/n", "Montebello Nails", "проспект Абая, 164/6", ["+7 700 000 00 01"])
    assert choose(identity, [lookalike]).verdict == "not_match"
    latin = Candidate("l", "4", "https://yandex.example/l", "Монтебелло", "пр-т Абая, 164/6", [])
    decision = choose(identity, [latin])
    assert decision.verdict == "same"
    assert decision.phone_match is False
    twin_branch = Candidate("c", "5", "https://yandex.example/c", "Montebello", "микрорайон Самал-2, 32", ["8 776 111 25 85"])
    assert choose(identity, [other_branch, twin_branch]).verdict == "ambiguous"


def test_code_reader_keeps_zero_and_leaves_unseen_null() -> None:
    cfg = load_platforms()
    html = """
    <article data-page="card" data-platform="2gis" data-external-id="1">
      <span data-field="reviews_count">0</span>
      <span data-field="prices">empty</span>
      <span data-field="category">Салон красоты</span>
    </article>
    """
    from zhol.browser.twogis.public import read_card as read_twogis

    document = read_twogis(html, cfg["markers"])
    fields, sources = extract_card(
        "2gis",
        document,
        url="https://2gis.kz/almaty/firm/1",
        seen_at="2026-11-02T09:00:00+00:00",
        screenshot="shot.png",
        raw_path="page.html",
        code_fields=list(cfg["platforms"]["2gis"]["code_fields"]),
        model_fields=list(cfg["platforms"]["2gis"]["model_fields"]),
    )
    assert fields["reviews_count"] == 0
    assert fields["rating"] is None
    assert sources["rating"]["reason"] == "not_shown"
    assert fields["category"] == "Салон красоты"
    assert sources["category"]["read_by"] == "code"
    assert sources["category"]["url"].endswith("/1")
    assert fields["email"] is None
    assert sources["email"]["reason"] == "not_shown"
    unseen_must_be_null(False, None)


def test_visible_text_that_is_not_a_number_is_not_turned_into_zero() -> None:
    cfg = load_platforms()
    html = """
    <article data-page="card" data-platform="2gis" data-external-id="1">
      <span data-field="reviews_count">нет отзывов</span>
    </article>
    """
    from zhol.agents.scout.errors import InvalidField
    from zhol.browser.twogis.public import read_card as read_twogis

    document = read_twogis(html, cfg["markers"])
    with pytest.raises(InvalidField) as caught:
        extract_card(
            "2gis",
            document,
            url="https://2gis.kz/almaty/firm/1",
            seen_at="2026-11-02T09:00:00+00:00",
            screenshot="shot.png",
            raw_path="page.html",
            code_fields=list(cfg["platforms"]["2gis"]["code_fields"]),
            model_fields=["category"],
        )
    assert caught.value.key == "reviews_count"


def test_search_keeps_five_candidates() -> None:
    cfg = load_platforms()
    items = []
    for number in range(1, 7):
        items.append(
            f'<a data-candidate="{number}" data-external-id="{number}" href="https://yandex.example/{number}">'
            f'<span data-field="name">Place {number}</span></a>'
        )
    html = '<section data-page="search" data-platform="yandex">' + "".join(items) + "</section>"
    document = read_search(html, cfg["markers"])
    assert [item["candidate_id"] for item in document.candidates] == ["1", "2", "3", "4", "5"]
    with pytest.raises(Exception):
        read_card(html, cfg["markers"])


def test_platform_fields_match_the_dictionary() -> None:
    cfg = load_platforms()
    for platform in ("2gis", "yandex", "google"):
        spec = cfg["platforms"][platform]
        code, model = set(spec["code_fields"]), set(spec["model_fields"])
        assert not (code & model)
        assert code | model == set(field_keys(platform))


def test_live_fetch_stays_off_for_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    with pytest.raises(LiveCollectionDisabled):
        fetch_public("https://2gis.kz/almaty/firm/1")
    monkeypatch.setenv("ZHOL_SCOUT_LIVE", "1")
    with pytest.raises(LiveCollectionDisabled):
        fetch_public("https://yandex.kz/maps/?text=aoe")
    with pytest.raises(LiveCollectionDisabled):
        fetch_public("https://www.google.com/maps/search/?query=aoe")
    import zhol.browser.twogis.capture as capture

    monkeypatch.setattr(capture, "fetch_html", lambda url: "ok")
    assert fetch_public("https://2gis.kz/almaty/firm/1") == "ok"
    assert wait_seconds(None, rate_limit_seconds=2, at=T0) == 0
    assert wait_seconds(T0, rate_limit_seconds=2, at=T0 + timedelta(seconds=1)) == 1


def test_sample_is_about_one_in_ten() -> None:
    hits = sum(selected_for_recheck(str(number)) for number in range(1000))
    assert 70 <= hits <= 130


def test_formula_needs_the_same_delta_on_every_found_pair() -> None:
    class Row:
        def __init__(self, company_id: int, platform: str, fields: dict, *, match_status: str = "found", row_id: int) -> None:
            self.company_id = company_id
            self.platform = platform
            self.fields = fields
            self.match_status = match_status
            self.status = "verified"
            self.id = row_id

    rows = []
    for index, (left, right, photos) in enumerate(((4.5, 4.4, (10, 4)), (4.0, 3.9, (8, 8)), (3.2, 3.1, (3, 7))), start=1):
        rows.append(Row(index, "2gis", {"rating": left, "photos_count": photos[0]}, row_id=index))
        rows.append(Row(index, "yandex", {"rating": right, "photos_count": photos[1]}, row_id=100 + index))
    assert find_formulas(rows) == ["yandex.rating - 2gis.rating = -0.1"]
    rows.append(Row(4, "2gis", {"rating": 5.0, "photos_count": 1}, row_id=4))
    rows.append(Row(4, "yandex", {"rating": 5.0, "photos_count": 9}, row_id=104))
    assert find_formulas(rows) == []


def test_scout_live_flag_requires_the_switch_and_three_ids(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    ids = tmp_path / "ids.txt"
    ids.write_text("1\n2\n3\n", encoding="utf-8")
    assert main(["scout", "--live", "--ids-file", str(ids)]) == 2
    assert "ZHOL_SCOUT_LIVE=1" in capsys.readouterr().err
    assert main(["scout", "--live", "--etalon"]) == 2
    assert "all 50" in capsys.readouterr().err
    one = tmp_path / "one.txt"
    one.write_text("70000001066660465\n", encoding="utf-8")
    assert main(["scout", "--manual", "--ids-file", str(one)]) == 2
    assert "requires --live" in capsys.readouterr().err
    two = tmp_path / "two.txt"
    two.write_text("70000001025141518\n70000001109698727\n", encoding="utf-8")
    assert main(["scout", "--visible", "--ids-file", str(two)]) == 2
    assert "requires --live" in capsys.readouterr().err
    monkeypatch.setenv("ZHOL_SCOUT_LIVE", "1")
    assert main(["scout", "--live", "--manual", "--etalon"]) == 2
    assert "all 50" in capsys.readouterr().err
    assert main(["scout", "--live", "--manual", "--ids-file", str(ids)]) == 2
    assert "exactly 1" in capsys.readouterr().err
    assert main(["scout", "--live", "--visible", "--ids-file", str(ids)]) == 2
    assert "exactly 2" in capsys.readouterr().err


def test_browser_code_has_no_evasion() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "zhol").rglob("*.py") if "browser" in path.parts or "scout" in path.parts)
    folded = text.lower()
    for word in ("playwright_stealth", "undetected", "2captcha", "anticaptcha", "navigator.webdriver"):
        assert word not in folded
    assert "playwright" in folded
