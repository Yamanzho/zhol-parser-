"""Screenshot files are stored evidence. A missing file is recorded and never invented."""

from __future__ import annotations

from pathlib import Path

_STATES = frozenset({"present", "missing", "unreadable"})


def screenshot_state(path: str | Path | None) -> str:
    """Return present, missing, or unreadable. This does not create a file."""
    if path is None:
        return "missing"
    text = str(path).strip()
    if not text or "://" in text:
        return "missing"
    candidate = Path(text)
    try:
        if not candidate.is_file():
            return "missing"
        with candidate.open("rb") as handle:
            handle.read(1)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    return "present"


def usable_screenshot(path: str | Path | None) -> str | None:
    """A path that can be opened, or nothing. A missing path is not returned."""
    if screenshot_state(path) != "present":
        return None
    return str(Path(str(path).strip()))


def known_screenshot_state(value: object) -> str | None:
    if not isinstance(value, str) or value not in _STATES:
        return None
    return value
