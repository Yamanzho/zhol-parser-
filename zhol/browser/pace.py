"""One pacing clock for permitted collection requests.

Saved HTML walks do not call this. The interval comes from
``config/platforms.yaml``. A robot check is not retried here.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from zhol.browser.config import load_platforms
from zhol.browser.live import wait_seconds

_LOCK = threading.Lock()
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def collection_rate_seconds() -> float:
    return float(load_platforms()["rate_limit_seconds"])


def pace_path() -> Path:
    raw = os.environ.get("ZHOL_COLLECTION_PACE", "").strip()
    if raw:
        return Path(raw)
    return Path("/tmp/zhol-collection-pace")


@contextmanager
def collection_turn() -> Iterator[None]:
    """Keep two collection workers from starting a network call together."""
    path = pace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            _LOCK.acquire()
            try:
                yield
            finally:
                _LOCK.release()
            return
        try:
            with _LOCK:
                yield
        finally:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def reserve_delay(seconds: float, clock: Clock) -> float:
    """Claim the next slot and return how long this caller should wait."""
    with collection_turn():
        handle_path = pace_path()
        raw = ""
        stamp = handle_path.with_name(handle_path.name + ".stamp")
        if stamp.is_file():
            raw = stamp.read_text(encoding="utf-8").strip()
        last = _parsed(raw)
        at = clock()
        delay = wait_seconds(last, rate_limit_seconds=seconds, at=at)
        claimed = at + timedelta(seconds=delay)
        stamp.write_text(claimed.isoformat(), encoding="utf-8")
        return delay


def wait_turn(sleep: Sleeper | None = None, clock: Clock | None = None) -> None:
    """Space one permitted network step. Tests pass a sleep that does not wait."""
    import time

    pause = sleep or time.sleep
    tick = clock or (lambda: datetime.now(timezone.utc))
    delay = reserve_delay(collection_rate_seconds(), tick)
    if delay > 0:
        pause(delay)


def _parsed(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
