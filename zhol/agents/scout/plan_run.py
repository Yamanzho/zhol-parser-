"""Walk a stored plan on the single collector. One source, one Chrome profile, one batch.

Committed cards stay in the batch. A later combination does not insert them again.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from zhol.browser.discovery.types import RunControl
from zhol.db.models import CollectionBatch


_HOLD = frozenset({"paused", "stopped", "interrupted", "awaiting_manual_verification"})


def open_plan_batch(
    factory: sessionmaker,
    *,
    source_mode: str,
    platform: str,
    limit: int,
    out: Path,
    plan: dict[str, Any],
) -> int:
    first = plan["combinations"][0]
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city=str(first["city"]),
            category=str(first["query"]),
            platforms=[platform],
            item_limit=limit,
            source_mode=source_mode,
            status="running",
            out_path=str(out),
            catalog_path=source_mode,
            progress={
                "phase": "collect",
                "active_query": first["query"],
                "plan": plan,
                "seen_ids": [],
                "hits": 0,
                "duplicates": 0,
                "errors": 0,
            },
        )
        session.add(batch)
        session.flush()
        return int(batch.id)


def run_source_plan(
    factory: sessionmaker,
    *,
    plan: dict[str, Any],
    platform: str,
    source_mode: str,
    limit: int,
    out: Path,
    resume: int | None,
    control: RunControl,
    on_open: Callable[[int], None] | None,
    run_one: Callable[..., int],
) -> int:
    """run_one is the existing single-query collector for this source."""
    batch_id = resume or open_plan_batch(
        factory,
        source_mode=source_mode,
        platform=platform,
        limit=limit,
        out=out,
        plan=plan,
    )
    if on_open is not None:
        on_open(batch_id)
    stored = _progress(factory, batch_id)
    current = stored.get("plan") if isinstance(stored.get("plan"), dict) else plan
    combinations = list(current.get("combinations") or [])
    if not combinations:
        return batch_id
    cursor = int(current.get("cursor") or 0)
    if cursor < 0:
        cursor = 0
    slice_limit = current.get("combination_limit")
    slice_value = int(slice_limit) if isinstance(slice_limit, int) else None
    first_step = True
    while cursor < len(combinations):
        if control._stopped or control._paused:
            break
        _focus(factory, batch_id, cursor, clear_checkpoint=not first_step)
        first_step = False
        combo = combinations[cursor]
        hold = cursor < len(combinations) - 1
        run_one(
            city=str(combo["city"]),
            category=str(combo["query"]),
            limit=limit,
            resume=batch_id,
            hold_completion=hold,
            slice_limit=slice_value,
        )
        status = _status(factory, batch_id)
        if status in _HOLD:
            _mark_combo(factory, batch_id, cursor, status)
            break
        if status == "completed":
            _mark_combo(factory, batch_id, cursor, "completed")
            _mark_rest(factory, batch_id, cursor + 1, "skipped_cap")
            break
        _mark_combo(factory, batch_id, cursor, "completed")
        cursor += 1
        _set_cursor(factory, batch_id, cursor)
    if cursor >= len(combinations) and _status(factory, batch_id) == "running":
        from zhol.agents.scout.live_2gis import _mark

        _mark(factory, batch_id, "completed", finish=True)
    return batch_id


def _progress(factory: sessionmaker, batch_id: int) -> dict[str, Any]:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or not isinstance(batch.progress, dict):
            return {}
        return dict(batch.progress)


def _status(factory: sessionmaker, batch_id: int) -> str:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        return batch.status if batch is not None else "interrupted"


def _focus(factory: sessionmaker, batch_id: int, cursor: int, *, clear_checkpoint: bool) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or not isinstance(batch.progress, dict):
            return
        progress = dict(batch.progress)
        plan = dict(progress.get("plan") or {})
        combinations = list(plan.get("combinations") or [])
        if cursor >= len(combinations):
            return
        combo = combinations[cursor]
        batch.city = str(combo["city"])
        batch.category = str(combo["query"])
        plan["cursor"] = cursor
        plan["current_city"] = combo["city"]
        plan["current_query"] = combo["query"]
        progress["plan"] = plan
        progress["active_query"] = combo["query"]
        if clear_checkpoint:
            progress.pop("discovery_checkpoint", None)
        batch.progress = progress
        flag_modified(batch, "progress")


def _set_cursor(factory: sessionmaker, batch_id: int, cursor: int) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or not isinstance(batch.progress, dict):
            return
        progress = dict(batch.progress)
        plan = dict(progress.get("plan") or {})
        plan["cursor"] = cursor
        progress["plan"] = plan
        batch.progress = progress
        flag_modified(batch, "progress")


def _mark_combo(factory: sessionmaker, batch_id: int, cursor: int, status: str) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or not isinstance(batch.progress, dict):
            return
        progress = dict(batch.progress)
        plan = dict(progress.get("plan") or {})
        combinations = [dict(item) for item in plan.get("combinations") or []]
        if cursor < len(combinations):
            combinations[cursor]["status"] = status
        plan["combinations"] = combinations
        plan["cursor"] = cursor
        progress["plan"] = plan
        batch.progress = progress
        flag_modified(batch, "progress")


def _mark_rest(factory: sessionmaker, batch_id: int, start: int, status: str) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or not isinstance(batch.progress, dict):
            return
        progress = dict(batch.progress)
        plan = dict(progress.get("plan") or {})
        combinations = [dict(item) for item in plan.get("combinations") or []]
        for item in combinations[start:]:
            if item.get("status") == "pending":
                item["status"] = status
        plan["combinations"] = combinations
        progress["plan"] = plan
        batch.progress = progress
        flag_modified(batch, "progress")
