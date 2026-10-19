"""Page access states. A robot check is never a collected company card."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CARD_VISIBLE = "card_visible"
ROBOT_CHECK = "robot_check"
ACCESS_DENIED = "access_denied"
NAVIGATION_ERROR = "navigation_error"
ACCESS_BLOCKED = "access_blocked"

DEFAULT_ROBOT_TEXTS = (
    "не робот",
    "i'm not a robot",
    "подозрительную активность",
    "recaptcha",
)
DEFAULT_DENIED_TEXTS = (
    "доступ ограничен",
    "access denied",
    "sorry, you have been blocked",
)


def _hit(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    folded = text.lower()
    return any(phrase.lower() in folded for phrase in phrases)


def classify_access(
    *,
    http_status: int | None,
    url: str,
    text: str,
    navigation_error: str | None = None,
    robot_texts: tuple[str, ...] | list[str] | None = None,
    denied_texts: tuple[str, ...] | list[str] | None = None,
) -> str:
    if navigation_error:
        return NAVIGATION_ERROR
    robots = tuple(robot_texts or DEFAULT_ROBOT_TEXTS)
    denied = tuple(denied_texts or DEFAULT_DENIED_TEXTS)
    address = url.lower()
    if _hit(text, robots) or "captcha." in address or "/captcha" in address:
        return ROBOT_CHECK
    if (http_status is not None and http_status >= 400) or _hit(text, denied) or any(
        part in address for part in ("/login", "passport.", "accounts.google")
    ):
        return ACCESS_DENIED
    if not text.strip():
        return NAVIGATION_ERROR
    return CARD_VISIBLE


def extraction_allowed(access: str) -> bool:
    return access == CARD_VISIBLE


def outcome_for(access: str) -> str:
    if access == CARD_VISIBLE:
        return "collected"
    if access == ROBOT_CHECK:
        return ACCESS_BLOCKED
    return access


def write_access(folder: Path, record: dict[str, Any]) -> Path:
    path = Path(folder) / "access.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def diagnose_saved(folder: Path, *, http_status: int | None = None, url: str = "") -> dict[str, Any]:
    """Classify a page that was already saved. Does not open the network."""
    folder = Path(folder)
    text_path = folder / "text.txt"
    text = text_path.read_text(encoding="utf-8") if text_path.is_file() else ""
    access = classify_access(http_status=http_status, url=url, text=text)
    record = {
        "access": access,
        "outcome": outcome_for(access),
        "http_status": http_status,
        "url": url,
        "screenshot": str(folder / "screenshot.png"),
        "text": str(text_path),
        "extracted": False,
        "source": "saved_page",
    }
    write_access(folder, record)
    return record
