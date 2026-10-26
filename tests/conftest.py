from __future__ import annotations

import os
import signal
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from tests import pg_resources

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TABLES = {
    "companies",
    "company_match_keys",
    "company_platform_ids",
    "company_profiles",
    "card_snapshots",
    "audits",
    "deals",
    "orders",
    "invoices",
    "intake",
    "client_messages",
    "client_files",
    "credentials",
    "browser_profiles",
    "change_packages",
    "change_items",
    "change_log",
    "reviews",
    "content_items",
    "reports",
    "human_tasks",
    "jobs",
    "llm_calls",
    "events_out",
    "council_runs",
    "council_items",
    "review_quality",
    "collection_batches",
    "collection_batch_items",
    "listing_improvement_plans",
    "listing_approvals",
    "listing_submissions",
    "listing_submission_attempts",
}


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    return cfg


def _sigterm_as_interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def pytest_configure(config) -> None:
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _sigterm_as_interrupt)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    pg_resources.reap_stale()
    name = pg_resources.container_name()
    url = pg_resources.start(name)
    try:
        yield url
    finally:
        pg_resources.remove(name)


@pytest.fixture
def migrated_engine(postgres_url: str) -> Iterator[Engine]:
    engine = create_engine(postgres_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(postgres_url), "head")
    try:
        yield engine
    finally:
        engine.dispose()
