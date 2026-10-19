from __future__ import annotations

import os
import subprocess
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from tests.conftest import REQUIRED_TABLES, alembic_config
from zhol.api.main import app
from zhol.db.base import Base


UNIQUE_COLUMNS = {
    ("companies", ("id_2gis",)),
    ("deals", ("syntony_deal_id",)),
    ("invoices", ("idempotency_key",)),
    ("jobs", ("dedup_key",)),
    ("change_items", ("idempotency_key",)),
    ("company_profiles", ("company_id", "version")),
    ("browser_profiles", ("platform", "account_label")),
    ("change_packages", ("order_id", "version")),
    ("reviews", ("platform", "external_id")),
    ("listing_improvement_plans", ("company_id", "source_key")),
    ("listing_submissions", ("idempotency_key",)),
    ("listing_submissions", ("order_id", "package_id", "platform", "operation")),
}

FOREIGN_KEYS = {
    ("company_platform_ids", "companies"),
    ("company_profiles", "companies"),
    ("card_snapshots", "companies"),
    ("audits", "companies"),
    ("deals", "companies"),
    ("orders", "deals"),
    ("orders", "companies"),
    ("invoices", "orders"),
    ("intake", "orders"),
    ("client_messages", "companies"),
    ("client_messages", "orders"),
    ("client_files", "companies"),
    ("client_files", "orders"),
    ("credentials", "companies"),
    ("change_packages", "orders"),
    ("change_items", "change_packages"),
    ("change_log", "change_items"),
    ("reviews", "companies"),
    ("reviews", "orders"),
    ("content_items", "orders"),
    ("reports", "orders"),
    ("events_out", "deals"),
    ("council_items", "council_runs"),
    ("review_quality", "council_runs"),
    ("listing_improvement_plans", "companies"),
    ("listing_improvement_plans", "audits"),
    ("listing_approvals", "orders"),
    ("listing_approvals", "change_packages"),
    ("listing_submissions", "orders"),
    ("listing_submissions", "change_packages"),
    ("listing_submissions", "listing_approvals"),
    ("listing_submission_attempts", "listing_submissions"),
}

CRITICAL_COLUMNS = {
    "card_snapshots": {
        "match_status": ("text", False),
        "fields": ("jsonb", False),
        "field_sources": ("jsonb", False),
        "collected_at": ("timestamp with time zone", False),
    },
    "jobs": {
        "dedup_key": ("text", True),
        "run_after": ("timestamp with time zone", True),
        "payload": ("jsonb", False),
    },
    "credentials": {
        "ciphertext": ("bytea", False),
    },
    "audits": {
        "snapshot_ids": ("ARRAY", False),
        "discrepancies": ("jsonb", False),
    },
    "llm_calls": {
        "cost_usd": ("numeric", True),
    },
    "council_runs": {
        "level": ("text", False),
        "members": ("jsonb", False),
        "cost_usd": ("numeric", True),
    },
}


def _table_names(engine: Engine) -> set[str]:
    insp = inspect(engine)
    return set(insp.get_table_names()) - {"alembic_version"}


def test_upgrade_creates_required_tables(migrated_engine: Engine) -> None:
    assert _table_names(migrated_engine) == REQUIRED_TABLES


def test_primary_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert insp.get_pk_constraint("companies")["constrained_columns"] == ["id"]
    assert set(insp.get_pk_constraint("company_platform_ids")["constrained_columns"]) == {
        "company_id",
        "platform",
    }
    assert insp.get_pk_constraint("intake")["constrained_columns"] == ["order_id"]
    assert insp.get_pk_constraint("client_messages")["constrained_columns"] == ["id"]
    assert insp.get_pk_constraint("council_runs")["constrained_columns"] == ["id"]


def test_foreign_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    found: set[tuple[str, str]] = set()
    for table in REQUIRED_TABLES:
        for fk in insp.get_foreign_keys(table):
            found.add((table, fk["referred_table"]))
    assert FOREIGN_KEYS <= found


def test_unique_constraints_and_indexes(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    found: set[tuple[str, tuple[str, ...]]] = set()
    for table in REQUIRED_TABLES:
        for uq in insp.get_unique_constraints(table):
            found.add((table, tuple(uq["column_names"])))
        for idx in insp.get_indexes(table):
            if idx.get("unique"):
                found.add((table, tuple(idx["column_names"])))
    assert UNIQUE_COLUMNS <= found


def test_critical_columns_and_types(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        for table, columns in CRITICAL_COLUMNS.items():
            rows = conn.execute(
                text(
                    """
                    SELECT column_name, data_type, udt_name, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :table
                    """
                ),
                {"table": table},
            ).mappings()
            by_name = {row["column_name"]: row for row in rows}
            for name, (expected_type, nullable) in columns.items():
                assert name in by_name, f"{table}.{name} missing"
                row = by_name[name]
                actual = row["data_type"] if row["data_type"] != "ARRAY" else "ARRAY"
                if expected_type == "jsonb":
                    assert row["udt_name"] == "jsonb"
                elif expected_type == "bytea":
                    assert row["udt_name"] == "bytea"
                else:
                    assert actual == expected_type
                assert (row["is_nullable"] == "YES") is nullable


def test_match_status_is_column_not_json(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT data_type, udt_name
                FROM information_schema.columns
                WHERE table_name = 'card_snapshots' AND column_name = 'match_status'
                """
            )
        ).one()
    assert row[0] == "text"


def test_downgrade_then_upgrade(postgres_url: str, migrated_engine: Engine) -> None:
    cfg = alembic_config(postgres_url)
    command.downgrade(cfg, "base")
    assert _table_names(migrated_engine) == set()
    command.upgrade(cfg, "head")
    assert REQUIRED_TABLES <= _table_names(migrated_engine)


def test_alembic_metadata_matches_migration(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        context = MigrationContext.configure(
            connection=conn,
            opts={"compare_type": True},
        )
        diffs = compare_metadata(context, Base.metadata)
    assert diffs == []


def test_health_route_still_present() -> None:
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/health" in paths


def test_docker_compose_config() -> None:
    env = os.environ.copy()
    env["POSTGRES_USER"] = "compose-check-user"
    env["POSTGRES_PASSWORD"] = "compose-check-secret"
    env["POSTGRES_DB"] = "compose-check-db"
    result = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
