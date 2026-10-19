"""Queue workers.

The main queue runs snapshot verification and fixture or Catalog API batches.
The browser queue runs saved-page card reads.
The collector queue runs the existing Selenium 2GIS collector and does not
take work from the other two.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.jobs import BROWSER_JOBS, COLLECTOR_JOBS, MAIN_JOBS, handlers
from zhol.jobs import QUEUES, recover, run_one

QUEUE_JOBS = {"browser": BROWSER_JOBS, "main": MAIN_JOBS, "collector": COLLECTOR_JOBS}
log = logging.getLogger(__name__)
# A dropped database connection is retried with a capped wait. After this many
# consecutive failures the process exits so the service restart policy can
# start a fresh worker. Jobs already committed stay queued.
MAX_DB_DISCONNECTS = 8
MAX_DB_BACKOFF_SECONDS = 30.0


def _database_disconnected(exc: BaseException) -> bool:
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    return isinstance(exc, DBAPIError) and bool(getattr(exc, "connection_invalidated", False))


def _replace_engine(factory: sessionmaker[Session]) -> None:
    """Drop the broken pool and open a new engine on the same database."""
    bind = factory.kw.get("bind")
    if not isinstance(bind, Engine):
        return
    url = bind.url.render_as_string(hide_password=False)
    bind.dispose()
    from zhol.db.base import get_engine

    factory.configure(bind=get_engine(url))


def serve(
    factory: sessionmaker[Session],
    queue: str,
    *,
    notifier: Callable[[str], None],
    stop: Callable[[], bool] | None = None,
    poll_seconds: float = 1.0,
    max_cycles: int | None = None,
    reconnect_sleep: Callable[[float], None] = time.sleep,
    max_db_disconnects: int = MAX_DB_DISCONNECTS,
) -> int:
    if queue not in QUEUES:
        raise ValueError(f"unknown queue {queue!r}")
    allowed = QUEUE_JOBS[queue]
    registered = {name: handlers(factory)[name] for name in allowed}
    cycles = 0
    disconnects = 0
    while stop is None or not stop():
        try:
            _touch_heartbeat()
            recover(factory, notifier=notifier)
            ran = run_one(factory, queue, registered, notifier=notifier, types=allowed)
        except Exception as exc:
            if not _database_disconnected(exc):
                raise
            disconnects += 1
            log.warning(
                "database connection lost queue=%s consecutive=%s error=%s",
                queue,
                disconnects,
                type(exc).__name__,
            )
            _replace_engine(factory)
            if disconnects > max_db_disconnects:
                raise
            reconnect_sleep(min(MAX_DB_BACKOFF_SECONDS, float(2 ** (disconnects - 1))))
            continue
        disconnects = 0
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return cycles
        if ran is None:
            if poll_seconds:
                time.sleep(poll_seconds)
            elif max_cycles is None:
                return cycles
    return cycles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zhol.worker", description="Run Scout jobs from one queue")
    parser.add_argument("--queue", required=True, choices=QUEUES)
    parser.add_argument("--poll", type=float, default=1.0)
    args = parser.parse_args(argv)
    from zhol.db.base import get_session_factory
    from zhol.runtime_config import validate_process_environment

    try:
        validate_process_environment()
        factory = get_session_factory()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    stopped = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True
        from zhol.api.admin_jobs import stop_active_batches

        stop_active_batches()
        log.info("worker stop requested queue=%s", args.queue)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    from zhol.integrations import telegram

    _configure_logs(args.queue)
    print(f"worker queue={args.queue} jobs={','.join(QUEUE_JOBS[args.queue])}", flush=True)
    serve(factory, args.queue, notifier=telegram.notify, stop=lambda: stopped, poll_seconds=args.poll)
    return 0


def _touch_heartbeat() -> None:
    raw = os.environ.get("ZHOL_WORKER_HEARTBEAT", "").strip()
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def _configure_logs(queue: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if queue != "collector":
        return
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import parser_service

    parser_service.setup_logging()
    log.info("collector worker ready chrome_profile=%s", parser_service.chrome_profile_dir())


if __name__ == "__main__":
    sys.exit(main())
