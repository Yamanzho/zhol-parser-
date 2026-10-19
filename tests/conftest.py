from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TABLES = {
    "companies",
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


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    name = f"zhol-s03-test-{os.getpid()}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_USER=zhol",
        "-e",
        "POSTGRES_PASSWORD=zhol",
        "-e",
        "POSTGRES_DB=zhol_test",
        "-p",
        "127.0.0.1:0:5432",
        "postgres:16",
    )
    try:
        port = None
        for _ in range(40):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "zhol", "-d", "zhol_test"],
                capture_output=True,
                text=True,
            )
            if ready.returncode == 0:
                published = _docker("port", name, "5432").stdout.strip()
                port = published.rsplit(":", 1)[1]
                break
            time.sleep(0.25)
        if port is None:
            raise RuntimeError("test PostgreSQL did not become ready")
        yield f"postgresql+psycopg://zhol:zhol@127.0.0.1:{port}/zhol_test"
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


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
