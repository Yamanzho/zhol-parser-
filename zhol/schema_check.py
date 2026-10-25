"""Compare the database revision with the revision this code expects. Read-only.

Exit codes used by `zhol schema-check` and the launcher:
0  the schema matches this code
3  the schema is older; writers must not start, the explicit upgrade command may migrate it
5  the schema is newer, unknown, or unstamped; writers must not start and upgrade must not run
4  the database has no tables yet; a new installation may create them
2  the database could not be read
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

MATCH = 0
MISMATCH = 3
NEEDS_HUMAN = 5
EMPTY = 4
UNREADABLE = 2


@dataclass(frozen=True)
class SchemaState:
    code: int
    current: tuple[str, ...]
    expected: tuple[str, ...]
    message: str


def project_root() -> Path:
    """The directory with alembic.ini and alembic/versions.

    In the Docker image the package lives in site-packages and the migrations in
    /app, so the package parent alone is not enough.
    """
    candidates = []
    if os.environ.get("ZHOL_ROOT", "").strip():
        candidates.append(Path(os.environ["ZHOL_ROOT"].strip()))
    candidates += [Path(__file__).resolve().parents[1], Path.cwd()]
    for root in candidates:
        if (root / "alembic.ini").is_file() and (root / "alembic" / "versions").is_dir():
            return root
    raise FileNotFoundError("alembic.ini and alembic/versions were not found")


def script_directory() -> ScriptDirectory:
    root = project_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return ScriptDirectory.from_config(cfg)


def inspect(database_url: str) -> SchemaState:
    script = script_directory()
    expected = tuple(sorted(script.get_heads()))
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            has_version = conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar()
            if not has_version:
                tables = conn.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
                ).scalar_one()
                if tables == 0:
                    return SchemaState(EMPTY, (), expected, "База пустая: таблиц ещё нет. Новая установка может их создать.")
                return SchemaState(
                    NEEDS_HUMAN,
                    (),
                    expected,
                    "В базе есть таблицы, но нет отметки ревизии Alembic. Службы не запускаются. Нужна проверка человеком.",
                )
            current = tuple(sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()))
            conn.rollback()
    finally:
        engine.dispose()
    return _compare(script, current, expected)


def _compare(script: ScriptDirectory, current: tuple[str, ...], expected: tuple[str, ...]) -> SchemaState:
    shown_current = ", ".join(current) or "нет"
    shown_expected = ", ".join(expected)
    if current == expected:
        return SchemaState(MATCH, current, expected, f"Схема базы совместима: ревизия {shown_current}.")
    known = set()
    for revision in script.walk_revisions():
        known.add(revision.revision)
    if current and all(item in known for item in current):
        return SchemaState(
            MISMATCH,
            current,
            expected,
            f"Схема базы устарела: в базе {shown_current}, программе нужна {shown_expected}. "
            "Службы не запускались, данные не менялись. Обновите базу отдельной командой: "
            "дважды щелкните «Обновить базу Zhol.command» (или scripts/zhol-upgrade-db.sh).",
        )
    return SchemaState(
        NEEDS_HUMAN,
        current,
        expected,
        f"Ревизия базы {shown_current} неизвестна этой версии программы (нужна {shown_expected}). "
        "Похоже, база новее программы. Службы не запускались, данные не менялись. "
        "Верните версию программы, которая обновила базу, или откатите схему по docs/RELEASE_RU.md.",
    )
