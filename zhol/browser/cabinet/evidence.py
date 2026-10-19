"""Store a saved cabinet page next to the order. No screenshot is invented."""

from __future__ import annotations

import json
from pathlib import Path

from zhol.timeutil import now


def save_page(folder: Path, html: str, *, status: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / "page.html"
    page.write_text(html, encoding="utf-8")
    record = {"status": status, "submitted": False, "at": now().isoformat()}
    (folder / "access.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(page)
