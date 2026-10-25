"""Entry points work in a fresh interpreter, whichever module is imported first.

pytest has already imported most of the package, so an import cycle can stay
hidden in-process. These tests start a new Python for every check.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import ROOT
from tests.test_identity_links import WHEN, _add, _factory
from zhol.db.models import CardSnapshot

FIRST_IMPORTS = (
    "zhol.agents.scout.identity",
    "zhol.agents.scout.identity_store",
    "zhol.agents.scout.identity_report",
    "zhol.browser.discovery.types",
    "zhol.browser.discovery",
    "zhol.browser.discovery.engine",
    "zhol.browser.collector",
    "zhol.agents.scout.discover",
    "zhol.agents.scout.maps_selenium",
    "zhol.agents.scout.selenium_collector",
    "zhol.api.main",
    "zhol.worker",
    "zhol.worker_health",
    "zhol.cli",
    "zhol.demo.server",
)

TABLES = ("companies", "company_platform_ids", "card_snapshots", "company_match_keys")


def _python(*args: str, database_url: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )


def _fingerprint(engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {
            table: conn.execute(
                text(f"SELECT count(*) || ':' || md5(coalesce(string_agg(t::text, ',' ORDER BY t::text), '')) FROM {table} t")
            ).scalar_one()
            for table in TABLES
        }


@pytest.mark.parametrize("module", FIRST_IMPORTS)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = _python("-c", f"import {module}")
    assert result.returncode == 0, result.stderr


def test_discovery_package_still_exports_the_engine() -> None:
    result = _python(
        "-c",
        "from zhol.browser.discovery import DiscoveryRefused, RunControl, WalkResult, walk_saved\n"
        "from zhol.browser.discovery import engine\n"
        "assert walk_saved is engine.walk_saved and WalkResult is engine.WalkResult",
    )
    assert result.returncode == 0, result.stderr


def test_identity_report_cli_runs_in_a_fresh_interpreter(migrated_engine, postgres_url: str) -> None:
    result = _python("-m", "zhol.cli", "identity-report", database_url=postgres_url)
    assert result.returncode == 0, result.stderr
    assert "Записи не изменялись" in result.stdout
    assert "Сомнительных связей внутри одной компании нет." in result.stdout


def test_identity_report_cli_lists_a_conflict_and_writes_nothing(migrated_engine, postgres_url: str) -> None:
    factory = _factory(migrated_engine)
    with factory() as session, session.begin():
        company = _add(
            session,
            name="North",
            city="Алматы",
            phone="77010000001",
            address="улица Абая, 10",
            platform="2gis",
            external_id="firm-10",
            id_2gis="firm-10",
        )
        session.add(
            CardSnapshot(
                company_id=company.id,
                platform="google",
                match_status="found",
                url="https://example.test/google-10",
                fields={"name": "North", "address": "улица Абая, 10", "phones": ["77010000009"]},
                field_sources={},
                purpose="prospect",
                collected_at=WHEN,
                collected_by="test",
                status="collected",
            )
        )
        company_id = company.id
    before = _fingerprint(migrated_engine)

    result = _python("-m", "zhol.cli", "identity-report", database_url=postgres_url)

    assert result.returncode == 0, result.stderr
    assert f"Компания {company_id}" in result.stdout
    assert "Итого сомнительных связей: 1." in result.stdout
    assert _fingerprint(migrated_engine) == before


def test_identity_report_transaction_refuses_writes(migrated_engine, monkeypatch) -> None:
    from zhol.agents.scout import identity_report
    from zhol.cli import cmd_identity_report

    def writing_render(session) -> str:
        session.execute(text("INSERT INTO companies (name, city, source) VALUES ('x', 'Алматы', 'test')"))
        return "wrote"

    monkeypatch.setattr(identity_report, "render", writing_render)
    with pytest.raises(Exception, match="read-only transaction"):
        cmd_identity_report(None, _factory(migrated_engine))
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM companies")).scalar_one() == 0
