"""Discovery batch: saved or manual pages, then the existing collector and Excel file."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.batch import (
    BatchCounts,
    _claim,
    _counts,
    _export,
    _mark_failed,
    _platforms,
    _process_item,
    live_discovery_report,
)
from zhol.agents.scout.catalog import BatchError
from zhol.browser.collector import SavedHtmlCollector
from zhol.browser.config import load_platforms
from zhol.agents.scout.identity_store import COMPANY_SOURCE, save_branch
from zhol.browser.discovery.types import Branch, DiscoveryRefused, RunControl
from zhol.browser.fixtures import FixtureStore
from zhol.browser.live import live_enabled
from zhol.db.models import CollectionBatch, CollectionBatchItem
from zhol.timeutil import now


def run_discovery(
    factory: sessionmaker[Session],
    *,
    city: str | None,
    category: str | None,
    platforms: tuple[str, ...],
    limit: int | None,
    out: Path,
    source_mode: str,
    root: Path,
    fixtures_dir: Path | None = None,
    resume: int | None = None,
    pages: FixtureStore | None = None,
    stop_after: int | None = None,
    control: RunControl | None = None,
    on_open: Callable[[int], None] | None = None,
) -> BatchCounts:
    if source_mode == "live":
        if not live_enabled():
            raise BatchError("live discovery requires ZHOL_SCOUT_LIVE=1")
        raise BatchError(live_discovery_report())
    if source_mode not in COMPANY_SOURCE:
        raise BatchError(f"unknown source mode {source_mode}")
    chosen = _platforms(platforms)
    if limit is not None and limit < 1:
        raise BatchError("limit must be a positive number or omitted")
    control = control or RunControl()
    if resume is None and (not city or not category):
        raise BatchError("city and category are required")
    if resume is not None:
        with factory() as session:
            existing = session.get(CollectionBatch, resume)
            if existing is None:
                raise BatchError(f"batch {resume} was not found")
            if existing.progress is None and existing.source_mode == "fixture":
                from zhol.agents.scout.batch import run_batch

                return run_batch(
                    factory,
                    city=city,
                    category=category,
                    platforms=chosen,
                    limit=limit,
                    out=out,
                    source_mode="fixture",
                    catalog=Path(existing.catalog_path),
                    fixtures_dir=fixtures_dir,
                    resume=resume,
                    pages=pages,
                    stop_after=stop_after,
                    on_open=on_open,
                )
            city = existing.city
            category = existing.category
            root = Path(existing.catalog_path)
            if fixtures_dir is None and existing.fixtures_dir:
                fixtures_dir = Path(existing.fixtures_dir)
            out = Path(existing.out_path)
            chosen = tuple(existing.platforms or chosen)
            source_mode = existing.source_mode
    pages_dir = fixtures_dir or (root / "pages")
    batch_id = resume or _open(
        factory,
        city=city or "",
        category=category or "",
        platforms=chosen,
        limit=limit,
        out=out,
        root=root,
        fixtures_dir=pages_dir,
        source_mode=source_mode,
    )
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            raise BatchError(f"batch {batch_id} was not found")
        progress = dict(batch.progress or {})
        status = batch.status
    if on_open is not None:
        on_open(batch_id)
    if status == "completed":
        _export(factory, batch_id, out)
        return _counts(factory, batch_id, out, duplicate=_completed(factory, batch_id))
    if progress.get("phase") != "collect":
        resume_state = progress if progress.get("visited") or progress.get("pending") else None
        try:
            collected = SavedHtmlCollector(root).collect(
                city=city or "",
                category=category or "",
                platforms=chosen,
                source_mode=source_mode,
                evidence_dir=out.parent / "discovery-evidence" / str(batch_id),
                control=control,
                limit=limit,
                resume_state=resume_state,
            )
            walked = collected.walk
        except (DiscoveryRefused, PermissionError) as exc:
            raise BatchError(str(exc)) from exc
        _save_progress(factory, batch_id, walked.progress)
        _sync_items(factory, batch_id, walked.branches, source_mode)
        _sync_traces(factory, batch_id, list(walked.progress.get("unmatched") or []))
        if walked.status in ("paused", "stopped"):
            _mark_batch(factory, batch_id, walked.status, finish=walked.status == "stopped")
            _export(factory, batch_id, out)
            return _counts(factory, batch_id, out, duplicate=0)
    already = _completed(factory, batch_id) if resume is not None else 0
    store = pages or FixtureStore(pages_dir)
    _note_optional_screenshot(factory, batch_id, getattr(store, "screenshot_state", "missing"))
    cfg = load_platforms()
    processed = 0
    while stop_after is None or processed < stop_after:
        if control._stopped:
            break
        item_id = _claim(factory, batch_id)
        if item_id is None:
            break
        try:
            _process_item(factory, item_id, store, cfg)
        except BatchError:
            raise
        except Exception as exc:
            _mark_failed(factory, item_id, exc)
        _export(factory, batch_id, out)
        processed += 1
        if control._paused:
            break
    _save_progress(factory, batch_id, {**_progress(factory, batch_id), "phase": "collect"})
    if control._paused:
        final = "paused"
        finish = False
    elif control._stopped:
        final = "stopped"
        finish = True
    elif _pending(factory, batch_id):
        final = "interrupted"
        finish = True
    elif _access_stopped(_progress(factory, batch_id)):
        final = "interrupted"
        finish = True
    else:
        final = "completed"
        finish = True
    _mark_batch(factory, batch_id, final, finish=finish)
    _export(factory, batch_id, out)
    return _counts(factory, batch_id, out, duplicate=already)


def _open(
    factory: sessionmaker[Session],
    *,
    city: str,
    category: str,
    platforms: tuple[str, ...],
    limit: int | None,
    out: Path,
    root: Path,
    fixtures_dir: Path,
    source_mode: str,
) -> int:
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city=city,
            category=category,
            platforms=list(platforms),
            item_limit=limit,
            source_mode=source_mode,
            status="running",
            out_path=str(out),
            catalog_path=str(root),
            fixtures_dir=str(fixtures_dir),
            progress={"phase": "discover", "pages": 0, "hits": 0, "duplicates": 0, "blocked": [], "missing": []},
        )
        session.add(batch)
        session.flush()
        return batch.id


def _sync_items(factory: sessionmaker[Session], batch_id: int, branches: list[Branch], source_mode: str) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            return
        for branch in branches:
            save_branch(session, batch, branch, source_mode)


def _access_stopped(progress: dict) -> bool:
    blocked = progress.get("blocked") or []
    return any(str(item.get("access") or "") in {"robot_check", "access_denied", "access_blocked"} for item in blocked if isinstance(item, dict))


def _sync_traces(factory: sessionmaker[Session], batch_id: int, traces: list) -> None:
    """Keep Yandex-only and Google-only hits without creating a company row."""
    if not traces:
        return
    with factory() as session, session.begin():
        known = set(
            session.scalars(select(CollectionBatchItem.id_2gis).where(CollectionBatchItem.batch_id == batch_id)).all()
        )
        for raw in traces:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("item_key") or "")
            if not key or key in known:
                continue
            session.add(
                CollectionBatchItem(
                    batch_id=batch_id,
                    id_2gis=key,
                    company_id=None,
                    status="blocked",
                    detail=raw,
                )
            )
            known.add(key)


def _note_optional_screenshot(factory: sessionmaker[Session], batch_id: int, state: object) -> None:
    """Record that card evidence was optional. This does not create a file or fail the batch."""
    status = state if state in {"present", "missing", "unreadable"} else "missing"
    current = _progress(factory, batch_id)
    current["optional_screenshot"] = {"status": status, "required_for_verification": False}
    _save_progress(factory, batch_id, current)


def _save_progress(factory: sessionmaker[Session], batch_id: int, progress: dict) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is not None:
            previous = batch.progress or {}
            kept = {key: value for key, value in previous.items() if str(key).startswith("admin_")}
            batch.progress = {**progress, **kept}


def _progress(factory: sessionmaker[Session], batch_id: int) -> dict:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        return dict(batch.progress or {}) if batch is not None else {}


def _mark_batch(factory: sessionmaker[Session], batch_id: int, status: str, *, finish: bool) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            return
        batch.status = status
        batch.finished_at = now() if finish else None


def _completed(factory: sessionmaker[Session], batch_id: int) -> int:
    with factory() as session:
        items = session.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
    return sum(1 for item in items if item.status == "completed")


def _pending(factory: sessionmaker[Session], batch_id: int) -> int:
    with factory() as session:
        items = session.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
    return sum(1 for item in items if item.status in ("pending", "in_progress"))
