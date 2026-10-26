"""Monotonic phase timings of one browser collection run.

Per card this costs a few `perf_counter` calls and no extra database write.
A card commit is timed from opening the session to the end of the commit.
Its duration is written in the next card's transaction, and the last one when
the run ends, so the stored persistence time covers the writes and the commit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

PERSISTENCE_SCOPE = "commit"
SUMMED_KEYS = (
    "run_ms",
    "browser_start_ms",
    "link_discovery_ms",
    "card_read_ms",
    "card_pacing_ms",
    "verification_wait_ms",
    "persistence_ms",
    "cards_opened",
    "cards_persisted",
)
_PERSISTENCE_METRICS = ("persistence_ms_total", "persistence_samples", "average_persistence_ms")


def _ms(seconds: float) -> float:
    return round(max(0.0, seconds) * 1000, 3)


class PersistTimer:
    """Full commit durations that are not yet stored in the batch metrics."""

    def __init__(self) -> None:
        self.pending_ms = 0.0
        self.pending_samples = 0
        self.run_ms = 0.0
        self.run_samples = 0
        self._applied = False

    def apply(self, metrics: dict[str, Any]) -> None:
        metrics["persistence_ms_total"] = float(metrics.get("persistence_ms_total") or 0) + self.pending_ms
        metrics["persistence_samples"] = int(metrics.get("persistence_samples") or 0) + self.pending_samples
        self._applied = True

    def committed(self, elapsed_ms: float) -> None:
        self.drained()
        self.pending_ms += elapsed_ms
        self.pending_samples += 1
        self.run_ms += elapsed_ms
        self.run_samples += 1

    def drained(self) -> None:
        if self._applied:
            self.pending_ms = 0.0
            self.pending_samples = 0
        self._applied = False

    def rolled_back(self) -> None:
        self._applied = False

    def timed(self, commit: Callable[[], str]) -> str:
        started = time.perf_counter()
        try:
            kind = commit()
        except BaseException:
            self.rolled_back()
            raise
        self.committed((time.perf_counter() - started) * 1000)
        return kind


class RunClock:
    """Phase boundaries come from the collector callbacks, not from the scraping engine."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.persist = PersistTimer()
        self.browser_start_ms = 0.0
        self.cards_opened = 0
        self._search_started: float | None = None
        self._links_done: float | None = None
        self._search_done: float | None = None
        self._card_pacing = 0.0
        self._verification_wait = 0.0
        self._card_verification_wait = 0.0

    def browser_started(self, since: float) -> None:
        self.browser_start_ms = _ms(time.perf_counter() - since)

    def search_began(self) -> None:
        self._search_started = time.perf_counter()

    def search_ended(self) -> None:
        if self._search_started is not None and self._search_done is None:
            self._search_done = time.perf_counter()

    def progress(self, _event: dict[str, Any]) -> None:
        if self._search_started is not None and self._links_done is None:
            self._links_done = time.perf_counter()

    def card_opened(self) -> None:
        self.cards_opened += 1

    def paced(self, pace: Callable[[], Any] | None) -> Callable[[], Any] | None:
        if pace is None:
            return None

        def wrapped() -> Any:
            started = time.perf_counter()
            try:
                return pace()
            finally:
                if self._links_done is not None:
                    self._card_pacing += time.perf_counter() - started

        return wrapped

    def paused(self, pause: Callable[..., Any] | None) -> Callable[..., Any] | None:
        if pause is None:
            return None

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return pause(*args, **kwargs)
            finally:
                spent = time.perf_counter() - started
                self._verification_wait += spent
                if self._links_done is not None:
                    self._card_verification_wait += spent

        return wrapped

    def summary(self) -> dict[str, float | int]:
        finished = time.perf_counter()
        discovery = 0.0
        cards = 0.0
        if self._search_started is not None:
            done = self._search_done if self._search_done is not None else finished
            links = self._links_done if self._links_done is not None else done
            discovery = links - self._search_started
            cards = done - links
        summary: dict[str, float | int] = {
            "run_ms": _ms(finished - self.started),
            "browser_start_ms": self.browser_start_ms,
            "persistence_ms": round(self.persist.run_ms, 3),
            "cards_opened": self.cards_opened,
            "cards_persisted": self.persist.run_samples,
        }
        if self._search_started is None:
            return summary
        persisted = self.persist.run_ms / 1000
        read = cards - self._card_pacing - self._card_verification_wait - persisted
        summary.update(
            {
                "link_discovery_ms": _ms(discovery),
                "card_read_ms": _ms(read),
                "card_pacing_ms": _ms(self._card_pacing),
                "verification_wait_ms": _ms(self._verification_wait),
            }
        )
        return summary


def begin_timings(state: dict[str, Any]) -> None:
    """A batch without commit-scoped timings drops its older partial persistence numbers once."""
    timings = state.get("timings")
    if isinstance(timings, dict) and timings.get("persistence_scope") == PERSISTENCE_SCOPE:
        return
    metrics = state.get("metrics")
    if isinstance(metrics, dict):
        metrics = dict(metrics)
        for key in _PERSISTENCE_METRICS:
            metrics.pop(key, None)
        state["metrics"] = metrics
    state["timings"] = {"persistence_scope": PERSISTENCE_SCOPE, "runs": 0}


def merge_timings(stored: Any, run: dict[str, float | int]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
    merged["persistence_scope"] = PERSISTENCE_SCOPE
    merged["runs"] = int(merged.get("runs") or 0) + 1
    for key in SUMMED_KEYS:
        if key not in run:
            continue
        value = run[key]
        previous = merged.get(key) or 0
        if key.startswith("cards_"):
            merged[key] = int(previous) + int(value)
        else:
            merged[key] = round(float(previous) + float(value), 3)
    return merged


def public_timings(stored: Any) -> dict[str, Any]:
    """Shown numbers. A batch from before the commit-scoped timer reports that scope."""
    if not isinstance(stored, dict) or stored.get("persistence_scope") != PERSISTENCE_SCOPE:
        return {"persistence_scope": "partial_legacy"}
    shown: dict[str, Any] = {"persistence_scope": PERSISTENCE_SCOPE, "runs": int(stored.get("runs") or 0)}
    for key in SUMMED_KEYS:
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            shown[key] = value
    opened = int(shown.get("cards_opened") or 0)
    persisted = int(shown.get("cards_persisted") or 0)
    if opened and "card_read_ms" in shown:
        shown["average_card_read_ms"] = round(float(shown.get("card_read_ms") or 0) / opened, 1)
    if persisted:
        shown["average_persistence_ms"] = round(float(shown.get("persistence_ms") or 0) / persisted, 1)
    return shown
