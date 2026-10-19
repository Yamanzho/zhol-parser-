from datetime import datetime, timezone

import pytest

from zhol.timeutil import now, parse_now, reset_clock, set_clock, set_now


@pytest.fixture(autouse=True)
def _reset_clock() -> None:
    reset_clock()
    yield
    reset_clock()


def test_now_is_timezone_aware() -> None:
    assert now().tzinfo is not None


def test_set_now_freezes_clock() -> None:
    frozen = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    set_now(frozen)
    assert now() == frozen


def test_set_now_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        set_now(datetime(2026, 11, 2, 9, 0))


def test_injected_naive_clock_is_rejected() -> None:
    set_clock(lambda: datetime(2026, 11, 2, 9, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        now()


def test_parse_now_treats_naive_iso_as_utc() -> None:
    assert parse_now("2026-11-02T09:00") == datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
