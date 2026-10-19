from __future__ import annotations

import pytest

from zhol.guards import (
    FORBIDDEN_PHRASES,
    client_supplied_fact,
    forbidden_phrases,
    is_nap_field,
    kzt,
    no_long_dash,
    require_verified_owner,
    unseen_must_be_null,
)


@pytest.mark.parametrize("dash", ["\u2013", "\u2014"])
def test_long_dash_rejected(dash: str) -> None:
    assert no_long_dash(f"Цены {dash} пусто") is False


def test_short_hyphen_allowed() -> None:
    assert no_long_dash("Онлайн-запись, цены: есть. 2-3 дня") is True


@pytest.mark.parametrize("field", ["name", "address", "phones", "category"])
def test_nap_fields(field: str) -> None:
    assert is_nap_field(field) is True


@pytest.mark.parametrize("field", ["2gis.name", "yandex.address", "google.phones", "2gis.category"])
def test_nap_field_refs(field: str) -> None:
    assert is_nap_field(field) is True


@pytest.mark.parametrize("field", ["hours", "website", "prices", "phone", "review_reply", "2gis.prices", "other.name"])
def test_non_nap_fields(field: str) -> None:
    assert is_nap_field(field) is False


@pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
def test_each_forbidden_phrase_found(phrase: str) -> None:
    assert forbidden_phrases(f"Наблюдение: {phrase.upper()}.") == [phrase]


def test_negative_acceptance_n4() -> None:
    assert forbidden_phrases("Часть клиентов уходит к соседям") == ["уходит к соседям"]


def test_yo_and_whitespace_normalized() -> None:
    assert forbidden_phrases("У вас  все\nплохо") == ["у вас всё плохо"]


def test_multiple_phrases_reported() -> None:
    assert forbidden_phrases("Гарантируем рост продаж") == ["гарантируем", "рост продаж"]


def test_allowed_wording_passes() -> None:
    assert forbidden_phrases("Вкладка цен в 2ГИС пустая, человеку сложнее выбрать. Лучшие фото в топливной") == []


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(0, "0 ₸"), (500, "500 ₸"), (20000, "20 000 ₸"), (30000, "30 000 ₸"), (75000, "75 000 ₸"), (1250000, "1 250 000 ₸")],
)
def test_kzt_format(amount: int, expected: str) -> None:
    assert kzt(amount) == expected


@pytest.mark.parametrize("amount", [1.5, "30000", True, None])
def test_kzt_rejects_non_integer(amount: object) -> None:
    with pytest.raises(TypeError):
        kzt(amount)  # type: ignore[arg-type]


def test_unseen_field_cannot_be_zero() -> None:
    unseen_must_be_null(True, 0)
    unseen_must_be_null(False, None)
    with pytest.raises(ValueError):
        unseen_must_be_null(False, 0)


def test_kzt_rejects_negative() -> None:
    with pytest.raises(ValueError):
        kzt(-1)


def test_client_fact_is_not_filled_in() -> None:
    client_supplied_fact(True, "ул. Абая 10")
    client_supplied_fact(False, None)
    with pytest.raises(ValueError):
        client_supplied_fact(False, "ул. Абая 10")


def test_listing_change_requires_verified_owner() -> None:
    require_verified_owner("2gis", "verified")
    with pytest.raises(PermissionError):
        require_verified_owner("google", "invited")


@pytest.mark.parametrize(
    "action",
    ["type_password", "solve_captcha", "read_sms", "bypass_login", "rotate_ip", "stealth", "submit_listing_form"],
)
def test_blocked_browser_actions(action: str) -> None:
    from zhol.guards import refuse_blocked_browser_action, refuse_listing_submit

    with pytest.raises(PermissionError):
        refuse_blocked_browser_action(action)
    refuse_listing_submit(False)
    with pytest.raises(PermissionError):
        refuse_listing_submit(True)
    from zhol.guards import refuse_live_listing_submit

    refuse_live_listing_submit("offline")
    with pytest.raises(PermissionError):
        refuse_live_listing_submit("live")


def test_unapproved_fact_and_generated_media_are_refused() -> None:
    from zhol.guards import listing_media_allowed, refuse_unapproved_fact

    refuse_unapproved_fact("hours", True)
    with pytest.raises(PermissionError):
        refuse_unapproved_fact("phones", False)
    listing_media_allowed("cover", status="checked", illustrative=True)
    listing_media_allowed("photo_real")
    with pytest.raises(PermissionError):
        listing_media_allowed("interior")
    with pytest.raises(PermissionError):
        listing_media_allowed("cover", status="done", illustrative=True)
    with pytest.raises(PermissionError):
        listing_media_allowed("video", status="checked", illustrative=False)
