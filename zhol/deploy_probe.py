"""Read-only release probe for scripts/zhol_deploy.py. Prints one JSON line.

Runs inside a freshly built image (or the macOS collector .venv) before any
running service is replaced. It does not connect to the database, does not
print settings, and does not start jobs.

--startup  also runs the API startup checks and lists the OpenAPI paths.
"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import metadata
from pathlib import Path

import zhol


def code_digest() -> str:
    package = Path(zhol.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(p for p in package.rglob("*.py") if "__pycache__" not in p.parts):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def alembic_heads() -> list[str]:
    from zhol.schema_check import script_directory

    return sorted(script_directory().get_heads())


def startup_report() -> dict[str, object]:
    from zhol.llm.config import check_startup
    from zhol.runtime_config import validate_process_environment

    check_startup()
    validate_process_environment()
    from zhol.api.main import app

    return {"app_version": app.version, "paths": sorted(app.openapi().get("paths", {}))}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        version = metadata.version("zhol")
    except metadata.PackageNotFoundError:
        version = ""
    report: dict[str, object] = {"dist_version": version, "code": code_digest(), "heads": alembic_heads()}
    if "--startup" in args:
        try:
            report.update(startup_report())
        except Exception as exc:  # the message names the setting, never its value
            report["startup_error"] = f"{type(exc).__name__}: {exc}"
            print(json.dumps(report, ensure_ascii=False))
            return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
