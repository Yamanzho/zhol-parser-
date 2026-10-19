from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

Clock = Callable[[], datetime]

# Asia/Almaty has been UTC+5 without DST since 2024-03-01.
ALMATY = timezone(timedelta(hours=5), "Asia/Almaty")

_clock: Clock | None = None


def reset_clock() -> None:
    global _clock
    _clock = None


def set_clock(clock: Clock) -> None:
    global _clock
    _clock = clock


def parse_now(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def set_now(when: datetime) -> None:
    if when.tzinfo is None:
        raise ValueError("now override must be timezone-aware")
    set_clock(lambda: when)


def now() -> datetime:
    current = _clock() if _clock is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return current
