"""Confined admin files. Callers never receive a path they chose on disk."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

_EXPORT_NAME = re.compile(r"^(?:job|batch)-\d+\.xlsx$|^platforms\.xlsx$")
_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$")
MAX_XLSX_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 1024 * 1024


class AdminPathError(ValueError):
    pass


def repo_root() -> Path:
    env = os.environ.get("ZHOL_ROOT", "").strip()
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def admin_root() -> Path:
    env = os.environ.get("ZHOL_ADMIN_FILES", "").strip()
    root = Path(env).resolve() if env else repo_root() / "data" / "admin"
    root.mkdir(parents=True, exist_ok=True)
    for name in ("sources", "exports", "uploads", "reports", "content", "listings", "pages"):
        (root / name).mkdir(exist_ok=True)
    return root


def source_roots() -> tuple[Path, ...]:
    roots = [admin_root() / "sources"]
    fixtures = repo_root() / "tests" / "fixtures"
    if fixtures.is_dir():
        roots.append(fixtures)
    return tuple(path.resolve() for path in roots)


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_source(name: str) -> Path:
    """A catalog or saved-page directory under an allowed root. Not a URL."""
    text = name.strip()
    if not text or "://" in text or "\\" in text or text.startswith("/"):
        raise AdminPathError("source path is not allowed")
    parts = Path(text).parts
    if ".." in parts or not _RELATIVE.match(text):
        raise AdminPathError("source path is not allowed")
    matches = []
    for root in source_roots():
        candidate = (root / text).resolve()
        if inside(candidate, root) and candidate.exists():
            matches.append(candidate)
    if len(matches) != 1:
        raise AdminPathError("source was not found in the allowed directories")
    return matches[0]


def job_export_path(job_id: int) -> Path:
    root = (admin_root() / "exports").resolve()
    path = (root / f"job-{int(job_id)}.xlsx").resolve()
    if path.parent != root or not _EXPORT_NAME.match(path.name):
        raise AdminPathError("export path is not allowed")
    return path


def export_destination(stored: str) -> Path:
    root = (admin_root() / "exports").resolve()
    candidate = Path(stored).resolve()
    if candidate.parent != root or not _EXPORT_NAME.match(candidate.name):
        raise AdminPathError("export path is not allowed")
    return candidate


def open_export(stored: str) -> Path:
    candidate = export_destination(stored)
    if not candidate.is_file():
        raise AdminPathError("export file was not found")
    return candidate


def order_dir(kind: str, order_id: int) -> Path:
    if kind not in {"reports", "content", "listings", "pages"}:
        raise AdminPathError("directory is not allowed")
    root = (admin_root() / kind).resolve()
    folder = (root / str(int(order_id))).resolve()
    if not inside(folder, root) or folder.parent != root:
        raise AdminPathError("directory is not allowed")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def report_download(order_id: int, kind: str) -> Path:
    if kind not in {"json", "html"}:
        raise AdminPathError("report file was not found")
    folder = order_dir("reports", order_id)
    name = "completion.json" if kind == "json" else "completion.html"
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise AdminPathError("report file was not found")
    return path


def save_upload(data: bytes, suffix: str) -> Path:
    if suffix not in {".xlsx", ".html"}:
        raise AdminPathError("file type is not allowed")
    folder = (admin_root() / ("uploads" if suffix == ".xlsx" else "pages")).resolve()
    path = (folder / f"{uuid.uuid4().hex}{suffix}").resolve()
    if path.parent != folder:
        raise AdminPathError("file type is not allowed")
    path.write_bytes(data)
    return path


def read_upload(file: object, limit: int) -> bytes:
    handle = getattr(file, "file", None)
    if handle is None or not hasattr(handle, "read"):
        raise AdminPathError("file is missing")
    data = handle.read(limit + 1)
    if not isinstance(data, bytes):
        raise AdminPathError("file is missing")
    if len(data) > limit:
        raise AdminPathError("file is too large")
    if not data:
        raise AdminPathError("file is empty")
    return data


def xlsx_name(filename: str | None) -> None:
    raw = filename or ""
    if not raw or "/" in raw or "\\" in raw or ".." in raw or not raw.lower().endswith(".xlsx"):
        raise AdminPathError("only an .xlsx file is accepted")


def xlsx_bytes(data: bytes) -> None:
    if not data.startswith(b"PK\x03\x04"):
        raise AdminPathError("file is not an xlsx workbook")


def html_bytes(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace").lstrip()
    if text.startswith(("http://", "https://", "file://")) or "<" not in text:
        raise AdminPathError("a saved HTML page is required")
