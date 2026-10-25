"""Startup schema check on disposable PostgreSQL: old, current, newer, unstamped and empty schemas."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.conftest import ROOT, alembic_config
from zhol import schema_check

PREVIOUS = "e5a9b3c20f41"


@pytest.fixture
def blank_url(postgres_url: str):
    engine = create_engine(postgres_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return postgres_url


def _revision(url: str) -> list[str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    finally:
        engine.dispose()


def _cli(url: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "zhol.cli", "schema-check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_empty_database(blank_url: str) -> None:
    assert schema_check.inspect(blank_url).code == schema_check.EMPTY
    assert _cli(blank_url).returncode == 4


def test_previous_revision_is_outdated_and_left_alone(blank_url: str) -> None:
    command.upgrade(alembic_config(blank_url), PREVIOUS)
    state = schema_check.inspect(blank_url)
    assert state.code == schema_check.MISMATCH
    assert state.current == (PREVIOUS,)
    result = _cli(blank_url)
    assert result.returncode == 3
    assert "Схема базы устарела" in result.stderr
    assert "Обновить базу Zhol.command" in result.stderr
    assert _revision(blank_url) == [PREVIOUS]


def test_current_revision_matches(blank_url: str) -> None:
    command.upgrade(alembic_config(blank_url), "head")
    assert schema_check.inspect(blank_url).code == schema_check.MATCH
    result = _cli(blank_url)
    assert result.returncode == 0
    assert "Схема базы совместима" in result.stdout


def test_unknown_revision_needs_a_human(blank_url: str) -> None:
    command.upgrade(alembic_config(blank_url), "head")
    engine = create_engine(blank_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    engine.dispose()
    assert schema_check.inspect(blank_url).code == schema_check.NEEDS_HUMAN
    result = _cli(blank_url)
    assert result.returncode == 5
    assert "Обновить базу" not in result.stderr


def test_tables_without_revision_need_a_human(blank_url: str) -> None:
    engine = create_engine(blank_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE companies (id integer)"))
    engine.dispose()
    assert schema_check.inspect(blank_url).code == schema_check.NEEDS_HUMAN


def _collector_schema_gate(url: str) -> subprocess.CompletedProcess[str]:
    script = (ROOT / "scripts" / "zhol-collector-macos.sh").read_text(encoding="utf-8")
    start = script.index("schema_matches() {")
    body = script[start : script.index("\n}\n", start) + 3].replace('"${ROOT}/.venv/bin/python"', '"$PY"')
    snippet = (
        "set -euo pipefail\n"
        'log_info() { echo "INFO $*"; }\n'
        'log_error() { echo "ERROR $*"; }\n' + body + "if schema_matches; then echo GATE_OPEN; else echo GATE_CLOSED; fi\n"
    )
    env = {**os.environ, "DATABASE_URL": url, "PY": sys.executable}
    return subprocess.run(["bash", "-c", snippet], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


def test_collector_pauses_on_an_outdated_schema(blank_url: str) -> None:
    command.upgrade(alembic_config(blank_url), PREVIOUS)
    result = _collector_schema_gate(blank_url)
    assert "GATE_CLOSED" in result.stdout, result.stdout + result.stderr
    assert "schema check failed status=3" in result.stdout
    command.upgrade(alembic_config(blank_url), "head")
    assert "GATE_OPEN" in _collector_schema_gate(blank_url).stdout


def test_collector_checks_the_schema_before_starting_the_worker() -> None:
    script = (ROOT / "scripts" / "zhol-collector-macos.sh").read_text(encoding="utf-8")
    run = script[script.index("cmd_run() {") :]
    gate = run.index("if ! schema_matches; then")
    assert run.index("exit 0", gate) < run.index("zhol.worker", gate)


def test_unreachable_database_does_not_print_the_password() -> None:
    result = _cli("postgresql+psycopg://zhol:do-not-print-me@127.0.0.1:1/none")
    assert result.returncode == 2
    assert "do-not-print-me" not in result.stdout + result.stderr
