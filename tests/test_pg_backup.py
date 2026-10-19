"""Restore a dump into an empty database on the disposable test Postgres.

This does not connect to the application database and does not delete its volume.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from zhol.db.models import Company

ROOT = Path(__file__).resolve().parents[1]
HEAD = "e5a9b3c20f41"
TARGET = "zhol_restore_drill"


def test_compose_backup_is_outside_pgdata_and_workers_restart() -> None:
    main = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = main["services"]
    backup = services["backup"]
    assert backup["profiles"] == ["backup"]
    assert backup["restart"] == "unless-stopped"
    assert all("pgdata" not in str(item) for item in backup["volumes"])
    assert "./data/backups:/backups" in backup["volumes"]
    for name in ("worker", "browser-worker", "collector", "scheduler"):
        assert services[name]["restart"] == "unless-stopped"
    script = (ROOT / "scripts" / "zhol-pg-restore-drill.sh").read_text(encoding="utf-8")
    loop = (ROOT / "scripts" / "zhol-pg-backup-loop.sh").read_text(encoding="utf-8")
    assert "docker compose down" not in script
    assert "docker compose down" not in loop
    runbook = (ROOT / "docs" / "ops" / "postgres-backup.md").read_text(encoding="utf-8")
    assert "docker compose down -v" in runbook


def test_restore_script_refuses_the_application_database() -> None:
    env = os.environ.copy()
    env.update(
        {
            "ZHOL_PG_CONTAINER": "not-started",
            "ZHOL_RESTORE_DUMP": "/tmp/zhol-drill.dump",
            "ZHOL_RESTORE_TARGET": "zhol",
            "ZHOL_PG_SOURCE_DB": "zhol_test",
        }
    )
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "zhol-pg-restore-drill.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "refusing to restore into zhol" in result.stderr


def test_restore_drill_keeps_revision_and_fixture_row(migrated_engine, postgres_url: str) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with factory() as session, session.begin():
        session.add(Company(name="backup-drill-row", city="Алматы", source="backup-drill"))
    container = f"zhol-s03-test-{os.getpid()}"
    dump = "/tmp/zhol-p1-drill.dump"
    subprocess.run(
        ["docker", "exec", container, "pg_dump", "-U", "zhol", "-d", "zhol_test", "-Fc", "-f", dump],
        check=True,
        capture_output=True,
        text=True,
    )
    env = os.environ.copy()
    env.update(
        {
            "ZHOL_PG_CONTAINER": container,
            "ZHOL_RESTORE_DUMP": dump,
            "ZHOL_RESTORE_TARGET": TARGET,
            "ZHOL_PG_USER": "zhol",
            "ZHOL_PG_SOURCE_DB": "zhol_test",
        }
    )
    try:
        subprocess.run(
            ["sh", str(ROOT / "scripts" / "zhol-pg-restore-drill.sh")],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        url = postgres_url.rsplit("/", 1)[0] + "/" + TARGET
        restored = create_engine(url, future=True)
        try:
            with restored.connect() as conn:
                revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                name = conn.execute(
                    text("SELECT name FROM companies WHERE name = 'backup-drill-row'")
                ).scalar_one()
            assert revision == HEAD
            assert name == "backup-drill-row"
        finally:
            restored.dispose()
    finally:
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "zhol",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'zhol_restore_drill' AND pid <> pg_backend_pid()",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "zhol",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "DROP DATABASE IF EXISTS zhol_restore_drill",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
