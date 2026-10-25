"""PostgreSQL jobs for admin scout batches.

The API enqueues the job and returns. Fixture and Catalog API batches stay on
the main worker. Browser batches go to the collector queue. Pause and stop
are rows in the batch progress, so they survive a worker restart.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from zhol.agents.scout.batch import run_batch
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.discover import _mark_batch, run_discovery
from zhol.api.filesafe import AdminPathError, export_destination, inside, job_export_path, resolve_source, source_roots
from zhol.browser.discovery.types import RunControl
from zhol.db.models import CollectionBatch, Job
from zhol.jobs.queue import StaleJob, enqueue, worker_queue
from zhol.timeutil import now

JOB_TYPE = "admin.scout_batch"


class AdminJobError(ValueError):
    pass


class BatchBusy(BatchError):
    """Another running job already owns this batch."""


_ACTIVE_STOPS: list[Any] = []


def stop_active_batches() -> None:
    """Ask the in-flight batch to stop so its browser can quit."""
    for stop in list(_ACTIVE_STOPS):
        stop()


def public_job(job: Job) -> dict[str, Any]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "attempts": job.attempts,
        "batch_id": payload.get("batch_id"),
        "source_mode": payload.get("source_mode"),
        "city": payload.get("city"),
        "category": payload.get("category"),
        "platforms": list(payload.get("platforms") or []),
        "duplicate": False,
        "error": job.last_error,
        "live_mass_discovery": False,
    }


def start_scout_job(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    dedup = str(payload["dedup_key"])
    existing = session.scalar(select(Job).where(Job.dedup_key == dedup))
    if existing is not None:
        body = public_job(existing)
        body["duplicate"] = True
        session.commit()
        return body
    queue = "collector" if str(payload.get("source_mode") or "").startswith("browser_") else "main"
    job_id = enqueue(session, JOB_TYPE, payload, queue=queue, dedup_key=dedup)
    session.commit()
    job = session.get(Job, job_id)
    if job is None:
        raise AdminJobError("job was not stored")
    return public_job(job)


def signal_batch(session: Session, batch_id: int, signal: str) -> CollectionBatch:
    from zhol.agents.scout.selenium_collector import (
        AWAITING_MANUAL_VERIFICATION,
        MESSAGE_STOP,
        VERIFY_FAILED,
        VERIFY_PASSED,
    )

    allowed = {"pause", "stop", VERIFY_PASSED, VERIFY_FAILED}
    if signal not in allowed:
        raise AdminJobError("unknown batch signal")
    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        raise AdminJobError("batch not found")
    if batch.source_mode == "live":
        raise AdminJobError("live discovery is closed")
    if batch.status in {"completed", "stopped"}:
        raise AdminJobError(f"batch is {batch.status}")
    if batch.status == AWAITING_MANUAL_VERIFICATION and signal == "pause":
        raise AdminJobError("Сбор уже ждет ручную проверку в браузере. Пауза здесь не нужна.")
    progress = dict(batch.progress or {})
    if signal in {VERIFY_PASSED, VERIFY_FAILED}:
        if batch.status != AWAITING_MANUAL_VERIFICATION:
            raise AdminJobError("Сбор не ждет проверку в браузере.")
        record = progress.get("manual_verification")
        browser = record.get("browser") if isinstance(record, dict) else None
        if browser != "open":
            raise AdminJobError("Сначала откройте видимый Chrome и пройдите проверку в этом окне.")
        progress["admin_signal"] = signal
        batch.progress = progress
        flag_modified(batch, "progress")
        return batch
    waiting = batch.status == AWAITING_MANUAL_VERIFICATION
    progress["admin_signal"] = signal
    if signal == "stop" and not _batch_job_active(session, batch_id):
        batch.status = "stopped"
        batch.finished_at = now()
        if waiting:
            record = dict(progress.get("manual_verification") or {})
            record["outcome"] = "failed"
            record["browser"] = "closed"
            record["message"] = MESSAGE_STOP
            progress["manual_verification"] = record
            progress["error"] = MESSAGE_STOP
    elif signal == "pause" and batch.status != "running" and not _batch_job_active(session, batch_id):
        batch.status = "paused"
        batch.finished_at = None
    batch.progress = progress
    flag_modified(batch, "progress")
    return batch


def resume_batch(session: Session, batch_id: int) -> dict[str, Any]:
    from zhol.agents.scout.selenium_collector import AWAITING_MANUAL_VERIFICATION, MESSAGE_RESUME_BLOCKED

    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        raise AdminJobError("batch not found")
    if batch.status in {"completed", "stopped", "running"}:
        raise AdminJobError(f"batch is {batch.status}")
    progress = batch.progress if isinstance(batch.progress, dict) else {}
    record = progress.get("manual_verification")
    if isinstance(record, dict) and record.get("outcome") == "unavailable":
        message = record.get("message")
        if not isinstance(message, str) or not message:
            message = "Ручная проверка недоступна. Сбор не продолжается."
        raise AdminJobError(message)
    if batch.status == AWAITING_MANUAL_VERIFICATION:
        progress = batch.progress if isinstance(batch.progress, dict) else {}
        record = progress.get("manual_verification")
        outcome = record.get("outcome") if isinstance(record, dict) else None
        if outcome != "passed":
            raise AdminJobError(MESSAGE_RESUME_BLOCKED)
    if _batch_job_active(session, batch_id):
        raise AdminJobError("batch already has a job")
    if batch.source_mode == "live_2gis":
        progress = dict(batch.progress or {})
        progress.pop("admin_signal", None)
        batch.progress = progress
        flag_modified(batch, "progress")
        out = _stored_export(batch.out_path, job_export_path(_next_placeholder(session)))
        batch.out_path = str(out)
        payload = {
            "action": "resume",
            "source_mode": "live_2gis",
            "city": batch.city,
            "category": batch.category,
            "platforms": ["2gis"],
            "limit": batch.item_limit,
            "catalog": "live_2gis",
            "fixtures_dir": None,
            "resume": batch.id,
            "batch_id": batch.id,
            "out": str(out),
            "dedup_key": f"admin.scout_batch:resume:{batch.id}",
        }
        return start_scout_job(session, payload)
    if batch.source_mode in {"browser_yandex", "browser_google"}:
        from zhol.agents.scout.browser_google import collection_ready as google_ready
        from zhol.agents.scout.browser_google import refuse_browser_collection as refuse_google
        from zhol.agents.scout.browser_yandex import collection_ready as yandex_ready
        from zhol.agents.scout.browser_yandex import refuse_browser_collection as refuse_yandex

        ready = yandex_ready() if batch.source_mode == "browser_yandex" else google_ready()
        if not ready:
            try:
                refuse_yandex() if batch.source_mode == "browser_yandex" else refuse_google()
            except Exception as exc:
                raise AdminJobError(str(exc)) from exc
        progress = dict(batch.progress or {})
        progress.pop("admin_signal", None)
        batch.progress = progress
        flag_modified(batch, "progress")
        out = _stored_export(batch.out_path, job_export_path(_next_placeholder(session)))
        batch.out_path = str(out)
        platform = "yandex" if batch.source_mode == "browser_yandex" else "google"
        payload = {
            "action": "resume",
            "source_mode": batch.source_mode,
            "city": batch.city,
            "category": batch.category,
            "platforms": [platform],
            "limit": batch.item_limit,
            "catalog": batch.source_mode,
            "fixtures_dir": None,
            "resume": batch.id,
            "batch_id": batch.id,
            "out": str(out),
            "dedup_key": f"admin.scout_batch:resume:{batch.id}",
        }
        return start_scout_job(session, payload)
    if batch.source_mode == "browser_2gis":
        from zhol.agents.scout.browser_2gis import BLOCKER, browser_collection_authorized

        if not browser_collection_authorized():
            raise AdminJobError(BLOCKER)
        progress = dict(batch.progress or {})
        progress.pop("admin_signal", None)
        batch.progress = progress
        flag_modified(batch, "progress")
        out = _stored_export(batch.out_path, job_export_path(_next_placeholder(session)))
        batch.out_path = str(out)
        payload = {
            "action": "resume",
            "source_mode": "browser_2gis",
            "city": batch.city,
            "category": batch.category,
            "platforms": ["2gis"],
            "limit": batch.item_limit,
            "catalog": "browser_2gis",
            "fixtures_dir": None,
            "resume": batch.id,
            "batch_id": batch.id,
            "out": str(out),
            "dedup_key": f"admin.scout_batch:resume:{batch.id}",
        }
        return start_scout_job(session, payload)
    if batch.source_mode not in {"fixture", "manual"}:
        raise AdminJobError("live discovery is closed")
    progress = dict(batch.progress or {})
    progress.pop("admin_signal", None)
    batch.progress = progress
    flag_modified(batch, "progress")
    catalog = _allowed(batch.catalog_path)
    fixtures = None if not batch.fixtures_dir else str(_allowed(batch.fixtures_dir))
    out = _stored_export(batch.out_path, job_export_path(_next_placeholder(session)))
    batch.out_path = str(out)
    payload = {
        "action": "resume",
        "source_mode": batch.source_mode,
        "city": batch.city,
        "category": batch.category,
        "platforms": list(batch.platforms or []),
        "limit": batch.item_limit,
        "catalog": str(catalog),
        "fixtures_dir": fixtures,
        "resume": batch.id,
        "batch_id": batch.id,
        "out": str(out),
        "dedup_key": f"admin.scout_batch:resume:{batch.id}",
    }
    return start_scout_job(session, payload)


def start_verification_browser(session: Session, batch_id: int) -> dict[str, Any]:
    """Tell the operator the original window is already open. A new Chrome is not started."""
    from zhol.agents.scout.selenium_collector import (
        AWAITING_MANUAL_VERIFICATION,
        MESSAGE_ALREADY_OPEN,
        MESSAGE_NO_DISPLAY,
        MESSAGE_SESSION_CLOSED,
        _verification_refusal,
        visible_chrome_available,
    )

    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        raise AdminJobError("batch not found")
    if batch.source_mode not in {"browser_2gis", "browser_yandex", "browser_google"}:
        raise AdminJobError("Видимая проверка относится к сборщику в браузере.")
    if batch.status != AWAITING_MANUAL_VERIFICATION:
        raise AdminJobError(f"batch is {batch.status}")
    progress = dict(batch.progress or {})
    record = progress.get("manual_verification")
    if isinstance(record, dict) and record.get("outcome") == "unavailable":
        message = record.get("message")
        text = message if isinstance(message, str) and message else "Ручная проверка недоступна. Новое окно не открывается."
        return {
            "id": None,
            "status": batch.status,
            "batch_id": batch.id,
            "message": text,
            "duplicate": False,
        }
    if isinstance(record, dict) and record.get("outcome") == "passed":
        raise AdminJobError("Проверка уже пройдена. Можно продолжить сбор.")
    if (isinstance(record, dict) and record.get("browser") == "open") or _batch_job_active(session, batch_id):
        raise AdminJobError(MESSAGE_ALREADY_OPEN)
    if not visible_chrome_available():
        stored = dict(record) if isinstance(record, dict) else {}
        stored["outcome"] = "pending"
        stored["browser"] = "closed"
        stored["headed"] = False
        stored["message"] = MESSAGE_NO_DISPLAY
        progress["manual_verification"] = stored
        progress["error"] = MESSAGE_NO_DISPLAY
        batch.progress = progress
        flag_modified(batch, "progress")
        return {
            "id": None,
            "status": batch.status,
            "batch_id": batch.id,
            "message": MESSAGE_NO_DISPLAY,
            "duplicate": False,
        }
    refusal = _verification_refusal() if batch.source_mode == "browser_2gis" else None
    if refusal:
        stored = dict(record) if isinstance(record, dict) else {}
        stored["outcome"] = "not_permitted"
        stored["browser"] = "closed"
        stored["headed"] = False
        stored["message"] = refusal
        progress["manual_verification"] = stored
        progress["error"] = refusal
        batch.progress = progress
        flag_modified(batch, "progress")
        batch.status = "stopped"
        batch.finished_at = now()
        return {
            "id": None,
            "status": "stopped",
            "batch_id": batch.id,
            "message": refusal,
            "duplicate": False,
        }
    stored = dict(record) if isinstance(record, dict) else {}
    stored["outcome"] = "pending"
    stored["browser"] = "closed"
    stored["headed"] = False
    stored["message"] = MESSAGE_SESSION_CLOSED
    progress["manual_verification"] = stored
    progress["error"] = MESSAGE_SESSION_CLOSED
    batch.progress = progress
    flag_modified(batch, "progress")
    return {
        "id": None,
        "status": batch.status,
        "batch_id": batch.id,
        "message": MESSAGE_SESSION_CLOSED,
        "duplicate": False,
    }


def kick_job(session: Session, job_id: int) -> dict[str, Any] | None:
    job = session.get(Job, job_id)
    if job is None or job.type != JOB_TYPE:
        return None
    return public_job(job)


def handle_scout_batch(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    """Run one admin scout batch. Called by the main worker, not by the API process."""
    job_id, attempt = _running_attempt(factory, payload)
    signal = _job_admin_signal(factory, job_id)
    if signal == "stop":
        return
    if signal == "pause":
        from zhol.integration.live import raise_if_paused_before_start

        raise_if_paused_before_start(signal)
    control = RunControl()
    stop_poll = threading.Event()
    holder: dict[str, int | None] = {"batch_id": _payload_batch(payload)}
    _ACTIVE_STOPS.append(control.stop)

    def poll() -> None:
        while not stop_poll.wait(0.2):
            job_signal = _job_admin_signal(factory, job_id)
            if job_signal == "stop":
                control.stop()
            elif job_signal == "pause":
                control.pause()
            batch_id = holder["batch_id"]
            if not isinstance(batch_id, int):
                continue
            signal = _signal(factory, batch_id)
            if signal == "pause":
                control.pause()
            elif signal == "stop":
                control.stop()

    watcher = threading.Thread(target=poll, name="admin-scout-signal", daemon=True)
    watcher.start()
    try:
        _handle(factory, payload, control, holder, job_id, attempt)
    finally:
        if control.stop in _ACTIVE_STOPS:
            _ACTIVE_STOPS.remove(control.stop)
        stop_poll.set()
        watcher.join(1)


def _handle(
    factory: sessionmaker[Session],
    payload: dict[str, Any],
    control: RunControl,
    holder: dict[str, int | None],
    job_id: int,
    attempt: int,
) -> None:
    if str(payload.get("action") or "") == "manual_verification":
        from zhol.agents.scout.selenium_collector import run_visible_verification

        if worker_queue.get() not in {None, "collector"}:
            raise BatchError("Проверка в видимом Chrome выполняется только очередью collector.")
        batch_raw = payload.get("batch_id") or payload.get("resume")
        batch_id = int(batch_raw) if batch_raw else 0
        holder["batch_id"] = batch_id
        run_visible_verification(factory, batch_id, control=control)
        return
    mode = str(payload.get("source_mode") or "")
    if mode in {"browser_yandex", "browser_google"}:
        from zhol.agents.scout.browser_google import collection_ready as google_ready
        from zhol.agents.scout.browser_google import refuse_browser_collection as refuse_google
        from zhol.agents.scout.browser_yandex import collection_ready as yandex_ready
        from zhol.agents.scout.browser_yandex import refuse_browser_collection as refuse_yandex

        current = worker_queue.get()
        if current not in {None, "collector"}:
            raise BatchError("Сбор через браузер выполняется только очередью collector.")
        ready = yandex_ready() if mode == "browser_yandex" else google_ready()
        if not ready:
            refuse_yandex() if mode == "browser_yandex" else refuse_google()
        from zhol.browser.profile_gate import exclusive_chrome_profile

        with exclusive_chrome_profile():
            _run_maps(factory, payload, control, holder, job_id, attempt)
        return
    if mode == "browser_2gis":
        from zhol.agents.scout.browser_2gis import browser_collection_authorized, refuse_browser_collection

        current = worker_queue.get()
        if current not in {None, "collector"}:
            raise BatchError("Сбор через браузер выполняется только очередью collector.")
        if not browser_collection_authorized():
            refuse_browser_collection()
        from zhol.browser.profile_gate import exclusive_chrome_profile

        with exclusive_chrome_profile():
            _run_browser(factory, payload, control, holder, job_id, attempt)
        return
    if mode == "live_2gis":
        _run_live(factory, payload, control, holder, job_id, attempt)
        return
    if mode not in {"fixture", "manual"}:
        raise BatchError("live discovery is closed")
    catalog = _allowed(str(payload.get("catalog") or ""))
    fixtures_text = payload.get("fixtures_dir")
    fixtures = None if not fixtures_text else _allowed(str(fixtures_text))
    resume = payload.get("resume") or payload.get("batch_id")
    resume_id = int(resume) if resume else None
    out = _stored_export(str(payload.get("out") or ""), _running_export(factory))
    if resume_id is not None:
        _force_out(factory, resume_id, out)

    def opened(batch_id: int) -> None:
        holder["batch_id"] = batch_id
        _require_owner(factory, job_id, attempt)
        _stamp(factory, batch_id, job_id)
        _force_out(factory, batch_id, out)

    platforms = tuple(str(item) for item in payload.get("platforms") or ())
    if mode == "fixture":
        _run_fixture(factory, payload, catalog, fixtures, out, platforms, resume_id, opened, job_id, attempt)
        return

    def remember(batch_id: int) -> None:
        opened(batch_id)

    run_discovery(
        factory,
        city=payload.get("city"),
        category=payload.get("category"),
        platforms=platforms,
        limit=payload.get("limit"),
        out=out,
        source_mode="manual",
        root=catalog,
        fixtures_dir=fixtures,
        resume=resume_id,
        control=control,
        on_open=remember,
    )


def _job_plan(factory: sessionmaker[Session], payload: dict[str, Any], resume_id: int | None) -> dict[str, Any] | None:
    direct = payload.get("plan")
    if isinstance(direct, dict) and direct.get("combinations"):
        return direct
    if resume_id is None:
        return None
    with factory() as session:
        batch = session.get(CollectionBatch, resume_id)
    if batch is None or not isinstance(batch.progress, dict):
        return None
    stored = batch.progress.get("plan")
    if isinstance(stored, dict) and stored.get("combinations"):
        return stored
    return None


def _run_live(
    factory: sessionmaker[Session],
    payload: dict[str, Any],
    control: RunControl,
    holder: dict[str, int | None],
    job_id: int,
    attempt: int,
) -> None:
    from zhol.agents.scout.live_2gis import run_live_batch

    resume = payload.get("resume") or payload.get("batch_id")
    resume_id = int(resume) if resume else None
    out = _stored_export(str(payload.get("out") or ""), _running_export(factory))
    if resume_id is not None:
        _force_out(factory, resume_id, out)

    def opened(batch_id: int) -> None:
        holder["batch_id"] = batch_id
        _require_owner(factory, job_id, attempt)
        _stamp(factory, batch_id, job_id)
        _force_out(factory, batch_id, out)

    _require_owner(factory, job_id, attempt)
    run_live_batch(
        factory,
        city=str(payload.get("city") or ""),
        category=str(payload.get("category") or ""),
        limit=int(payload.get("limit") or 0),
        out=out,
        resume=resume_id,
        control=control,
        on_open=opened,
    )


def _run_browser(
    factory: sessionmaker[Session],
    payload: dict[str, Any],
    control: RunControl,
    holder: dict[str, int | None],
    job_id: int,
    attempt: int,
) -> None:
    from zhol.agents.scout.selenium_collector import run_browser_batch

    if list(payload.get("platforms") or ["2gis"]) != ["2gis"]:
        raise BatchError("Для существующего сборщика 2ГИС доступен только источник 2ГИС.")
    resume = payload.get("resume") or payload.get("batch_id")
    resume_id = int(resume) if resume else None
    out = _stored_export(str(payload.get("out") or ""), _running_export(factory))
    if resume_id is not None:
        _force_out(factory, resume_id, out)

    def opened(batch_id: int) -> None:
        holder["batch_id"] = batch_id
        _require_owner(factory, job_id, attempt)
        _stamp(factory, batch_id, job_id)
        acquire_batch_owner(factory, batch_id, job_id)
        _force_out(factory, batch_id, out)

    _require_owner(factory, job_id, attempt)
    plan = _job_plan(factory, payload, resume_id)
    if plan is not None:
        from zhol.agents.scout.plan_run import run_source_plan

        def run_one(**kwargs: Any) -> int:
            return run_browser_batch(
                factory,
                out=out,
                control=control,
                on_open=opened,
                ensure_owner=lambda: _require_owner(factory, job_id, attempt),
                job_attempt=attempt,
                **kwargs,
            )

        run_source_plan(
            factory,
            plan=plan,
            platform="2gis",
            source_mode="browser_2gis",
            limit=int(payload.get("limit") or plan.get("source_cap") or 0),
            out=out,
            resume=resume_id,
            control=control,
            on_open=opened,
            run_one=run_one,
        )
        return
    run_browser_batch(
        factory,
        city=str(payload.get("city") or ""),
        category=str(payload.get("category") or ""),
        limit=int(payload.get("limit") or 0),
        out=out,
        resume=resume_id,
        control=control,
        on_open=opened,
        ensure_owner=lambda: _require_owner(factory, job_id, attempt),
        job_attempt=attempt,
    )


def _run_maps(
    factory: sessionmaker[Session],
    payload: dict[str, Any],
    control: RunControl,
    holder: dict[str, int | None],
    job_id: int,
    attempt: int,
) -> None:
    from zhol.agents.scout.maps_selenium import run_maps_batch

    platforms = list(payload.get("platforms") or [])
    if platforms not in (["yandex"], ["google"]):
        raise BatchError("Для этого сборщика доступен только его собственный источник.")
    resume = payload.get("resume") or payload.get("batch_id")
    resume_id = int(resume) if resume else None
    out = _stored_export(str(payload.get("out") or ""), _running_export(factory))
    if resume_id is not None:
        _force_out(factory, resume_id, out)

    def opened(batch_id: int) -> None:
        holder["batch_id"] = batch_id
        _require_owner(factory, job_id, attempt)
        _stamp(factory, batch_id, job_id)
        acquire_batch_owner(factory, batch_id, job_id)
        _force_out(factory, batch_id, out)

    _require_owner(factory, job_id, attempt)
    plan = _job_plan(factory, payload, resume_id)
    if plan is not None:
        from zhol.agents.scout.plan_run import run_source_plan

        def run_one(**kwargs: Any) -> int:
            return run_maps_batch(
                factory,
                platform=platforms[0],
                out=out,
                control=control,
                on_open=opened,
                **kwargs,
            )

        run_source_plan(
            factory,
            plan=plan,
            platform=platforms[0],
            source_mode=str(payload.get("source_mode") or ""),
            limit=int(payload.get("limit") or plan.get("source_cap") or 0),
            out=out,
            resume=resume_id,
            control=control,
            on_open=opened,
            run_one=run_one,
        )
        return
    run_maps_batch(
        factory,
        platform=platforms[0],
        city=str(payload.get("city") or ""),
        category=str(payload.get("category") or ""),
        limit=int(payload.get("limit") or 0),
        out=out,
        resume=resume_id,
        control=control,
        on_open=opened,
    )


def _run_fixture(
    factory: sessionmaker[Session],
    payload: dict[str, Any],
    catalog: Path,
    fixtures: Path | None,
    out: Path,
    platforms: tuple[str, ...],
    resume_id: int | None,
    opened,
    job_id: int,
    attempt: int,
) -> None:
    batch_id = resume_id
    while True:
        _require_owner(factory, job_id, attempt)
        signal = _signal(factory, batch_id) if batch_id is not None else None
        if signal == "pause" and batch_id is not None:
            _mark_batch(factory, batch_id, "paused", finish=False)
            return
        if signal == "stop" and batch_id is not None:
            _mark_batch(factory, batch_id, "stopped", finish=True)
            return
        counts = run_batch(
            factory,
            city=payload.get("city"),
            category=payload.get("category"),
            platforms=platforms,
            limit=payload.get("limit"),
            out=out,
            source_mode="fixture",
            catalog=catalog,
            fixtures_dir=fixtures,
            resume=batch_id,
            stop_after=1,
            on_open=opened,
        )
        batch_id = counts.batch_id
        if counts.pending == 0:
            return


def _allowed(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        for root in source_roots():
            if inside(resolved, root) and resolved.exists():
                return resolved
        raise BatchError("source path is not allowed")
    try:
        return resolve_source(value)
    except AdminPathError as exc:
        raise BatchError(str(exc)) from exc


def _stored_export(stored: str, fallback: Path) -> Path:
    if not stored:
        return fallback
    try:
        return export_destination(stored)
    except AdminPathError:
        return fallback


def _running_export(factory: sessionmaker[Session]) -> Path:
    with factory() as session:
        job = session.scalar(
            select(Job).where(Job.type == JOB_TYPE, Job.status == "running").order_by(Job.id.desc()).limit(1)
        )
    if job is None:
        raise BatchError("scout job is not running")
    return job_export_path(job.id)


def _job_admin_signal(factory: sessionmaker[Session], job_id: int) -> str | None:
    with factory() as session:
        job = session.get(Job, job_id)
    if job is None or not isinstance(job.payload, dict):
        return None
    signal = job.payload.get("admin_signal")
    if signal in {"pause", "stop"}:
        return str(signal)
    return None


def _payload_batch(payload: dict[str, Any]) -> int | None:
    raw = payload.get("resume") or payload.get("batch_id")
    return int(raw) if raw else None


def _running_attempt(factory: sessionmaker[Session], payload: dict[str, Any]) -> tuple[int, int]:
    dedup = payload.get("dedup_key")
    with factory() as session:
        rows = session.scalars(select(Job).where(Job.type == JOB_TYPE, Job.status == "running")).all()
    for job in rows:
        body = job.payload if isinstance(job.payload, dict) else {}
        if dedup is None or body.get("dedup_key") == dedup:
            return job.id, job.attempts
    raise BatchError("scout job is not running")


def _owns(factory: sessionmaker[Session], job_id: int, attempt: int) -> bool:
    with factory() as session:
        job = session.get(Job, job_id)
    return job is not None and job.status == "running" and job.attempts == attempt


def _require_owner(factory: sessionmaker[Session], job_id: int, attempt: int) -> None:
    if not _owns(factory, job_id, attempt):
        raise StaleJob(f"job {job_id} attempt {attempt} is no longer running")


def _stamp(factory: sessionmaker[Session], batch_id: int, job_id: int) -> None:
    with factory() as session, session.begin():
        job = session.get(Job, job_id)
        if job is None or job.status != "running":
            return
        payload = dict(job.payload or {})
        payload["batch_id"] = batch_id
        job.payload = payload
        flag_modified(job, "payload")
        batch = session.get(CollectionBatch, batch_id)
        if batch is not None:
            progress = dict(batch.progress or {})
            progress["admin_job_id"] = job.id
            batch.progress = progress
            flag_modified(batch, "progress")


def _force_out(factory: sessionmaker[Session], batch_id: int, out: Path) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            return
        try:
            current = export_destination(batch.out_path)
        except AdminPathError:
            current = None
        batch.out_path = str(current or out)


def _signal(factory: sessionmaker[Session], batch_id: int) -> str | None:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        if batch is None or not isinstance(batch.progress, dict):
            return None
        signal = batch.progress.get("admin_signal")
    return signal if signal in {"pause", "stop"} else None


def acquire_batch_owner(factory: sessionmaker[Session], batch_id: int, job_id: int) -> None:
    """Lock the batch so a second worker cannot open a browser for it."""
    with factory() as session, session.begin():
        batch = session.execute(
            select(CollectionBatch).where(CollectionBatch.id == batch_id).with_for_update()
        ).scalar_one_or_none()
        if batch is None:
            raise BatchError("batch not found")
        rows = session.scalars(
            select(Job).where(Job.type == JOB_TYPE, Job.status == "running", Job.id != job_id)
        ).all()
        for job in rows:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if payload.get("batch_id") == batch_id or payload.get("resume") == batch_id:
                raise BatchBusy("этот сбор уже выполняет другая задача")
        progress = dict(batch.progress or {})
        progress["owner_job_id"] = job_id
        batch.progress = progress
        flag_modified(batch, "progress")


def _batch_job_active(session: Session, batch_id: int) -> bool:
    rows = session.scalars(
        select(Job).where(Job.type == JOB_TYPE, Job.status.in_(("queued", "running", "failed")))
    ).all()
    for job in rows:
        payload = job.payload if isinstance(job.payload, dict) else {}
        if payload.get("batch_id") == batch_id or payload.get("resume") == batch_id:
            return True
    return False


def _next_placeholder(session: Session) -> int:
    current = session.scalar(select(Job.id).order_by(Job.id.desc()).limit(1))
    return int(current or 0) + 1
