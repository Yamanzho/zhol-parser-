"""Google card name and source URL.

Values come only from the Google snapshot. A 2GIS name, a business website,
and a missing field stay empty. Historical rows are not rewritten.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from zhol.db.models import CardSnapshot
from zhol.timeutil import now

_MAPS_URL = re.compile(r"^https://(?:www\.)?google\.[^/?#]+/maps/(?:place|search)/", re.IGNORECASE)
_REQUIRED = ("name", "source_url")


def google_place_ready(name: str | None, url: str | None, external_id: str | None) -> bool:
    """A Google card is acceptable only with its own name and a URL for that card."""
    from zhol.browser.maps.visible import place_id

    title = _text(name)
    link = _http_url(url)
    token = _text(external_id)
    return bool(title and link and token and place_id("google", link) == token)


def assess_card(platform: str, fields: dict[str, Any] | None, url: str | None) -> dict[str, Any]:
    """Read the stored card. Missing name or source URL stays missing."""
    body = fields if isinstance(fields, dict) else {}
    name = _text(body.get("name"))
    source_url = _http_url(url) if platform == "google" else _text(url)
    missing = [key for key, value in (("name", name), ("source_url", source_url)) if not value]
    return {
        "name": name,
        "source_url": source_url,
        "missing": missing,
        "completeness": "incomplete" if missing else "complete",
        "completeness_label": "неполная" if missing else "полная",
    }


def repair_plan(snapshot: CardSnapshot) -> dict[str, Any]:
    """Say whether this Google snapshot's own evidence can fill name and URL.

    The plan does not read another platform and does not change the row.
    """
    if snapshot.platform != "google":
        return {"action": "unchanged", "reason": "not_google", "recoverable": False}
    stored = assess_card(snapshot.platform, snapshot.fields, snapshot.url)
    found = _recorded_google(snapshot)
    if stored["completeness"] == "complete":
        return {"action": "already_complete", "recoverable": False, "missing": []}
    if found["name"] and found["source_url"]:
        return {
            "action": "recoverable",
            "recoverable": True,
            "name": found["name"],
            "source_url": found["source_url"],
            "missing": [],
        }
    missing = [key for key in _REQUIRED if not found[key]]
    return {"action": "incomplete", "recoverable": False, "missing": missing, "name": None, "source_url": None}


def apply_google_repair(session: Session, snapshot_id: int) -> dict[str, Any]:
    """Append a recovered Google snapshot only when that snapshot already recorded both values.

    The original row stays as it was. An incomplete card is not rewritten.
    """
    snapshot = session.get(CardSnapshot, snapshot_id)
    if snapshot is None:
        return {"action": "missing"}
    plan = repair_plan(snapshot)
    if plan["action"] != "recoverable":
        return plan
    fields = dict(snapshot.fields) if isinstance(snapshot.fields, dict) else {}
    fields["name"] = plan["name"]
    sources = dict(snapshot.field_sources) if isinstance(snapshot.field_sources, dict) else {}
    sources["repair"] = {
        "source": "google_snapshot",
        "from_snapshot_id": snapshot.id,
        "fields": ["name", "source_url"],
    }
    created = CardSnapshot(
        company_id=snapshot.company_id,
        platform="google",
        match_status=snapshot.match_status,
        url=plan["source_url"],
        fields=fields,
        field_sources=sources,
        screenshot_path=snapshot.screenshot_path,
        purpose=snapshot.purpose,
        collected_at=now(),
        collected_by="google_repair",
        status="collected",
    )
    session.add(created)
    session.flush()
    return {"action": "recovered", "snapshot_id": created.id, "name": plan["name"], "source_url": plan["source_url"]}


def _recorded_google(snapshot: CardSnapshot) -> dict[str, str | None]:
    fields = snapshot.fields if isinstance(snapshot.fields, dict) else {}
    sources = snapshot.field_sources if isinstance(snapshot.field_sources, dict) else {}
    name = _text(fields.get("name"))
    name_source = sources.get("name") if isinstance(sources.get("name"), dict) else {}
    if name is None:
        name = _text(name_source.get("value"))
    source_url = _http_url(snapshot.url)
    if source_url is None:
        for key in ("name", "match_status"):
            block = sources.get(key) if isinstance(sources.get(key), dict) else {}
            source_url = _maps_url(block.get("hyperlink")) or _maps_url(block.get("url"))
            if source_url:
                break
    return {"name": name, "source_url": source_url}


def _http_url(value: Any) -> str | None:
    text = _text(value)
    if text and text.startswith(("http://", "https://")):
        return text
    return None


def _maps_url(value: Any) -> str | None:
    text = _text(value)
    if text and _MAPS_URL.match(text):
        return text
    return None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (list, dict, bool)):
        return None
    text = " ".join(str(value).split())
    return text or None
