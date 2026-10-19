"""Walk saved search pages: next page, scroll fragment, firm links, pause and stop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from zhol.browser.config import load_platforms
from zhol.browser.discovery import google as google_adapter
from zhol.browser.discovery import twogis as twogis_adapter
from zhol.browser.discovery import yandex as yandex_adapter
from zhol.agents.scout.identity import profile_from_branch, profile_from_hit, relate
from zhol.agents.scout.normalize import phone_parts, phones_overlap
from zhol.browser.discovery.normalize import (
    clean_address,
    clean_name,
    normalize_category,
    verified_email,
    verified_instagram,
    verified_website,
    verified_whatsapp,
)
from zhol.browser.discovery.session import LocalHtmlPage, document_url, show_saved
from zhol.browser.evidence import usable_screenshot
from zhol.browser.discovery.types import Branch, DiscoveryRefused, PlatformEvidence, RunControl, WalkResult
from zhol.browser.page import parse_html
from zhol.browser.twogis.access import classify_access, extraction_allowed
from zhol.export.mailing import _key, load_categories
from zhol.timeutil import now

_ADAPTERS = {
    "2gis": twogis_adapter,
    "yandex": yandex_adapter,
    "google": google_adapter,
}
_PHONES = "phones"
_STOP_ACCESS = frozenset({"robot_check", "access_denied", "access_blocked"})


def walk_saved(
    root: str | Path,
    *,
    city: str,
    category: str,
    platforms: tuple[str, ...],
    source: str,
    evidence_dir: str | Path,
    control: RunControl | None = None,
    page: Any = None,
    limit: int | None = None,
    resume_state: dict | None = None,
    cfg: dict[str, Any] | None = None,
) -> WalkResult:
    for name in platforms:
        adapter = _ADAPTERS.get(name)
        if adapter is None:
            raise DiscoveryRefused(f"unsupported platform {name}")
        adapter.refuse_live(source)
    folder = Path(root).resolve()
    manifest = _manifest(folder, city=city, category=category)
    config = cfg or load_platforms()
    markers = config["markers"]
    evidence = Path(evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    driver = page or LocalHtmlPage()
    control = control or RunControl()
    labels = load_categories()
    state = _state(resume_state, manifest, platforms)
    branches = [Branch.from_progress(item) for item in state.get("branches") or []]
    query_key = _key(category)
    status = "running"
    for name in platforms:
        if status in ("paused", "stopped"):
            break
        spec = config["platforms"][name]["discovery"]
        pending = list(state["pending"].get(name) or [])
        while pending and status == "running":
            item = pending.pop(0)
            kind, rel = item["kind"], item["path"]
            path = _inside(folder, folder / rel)
            state["pending"][name] = pending
            if str(path) in state["visited"]:
                continue
            state["visited"].append(str(path))
            if not path.is_file():
                state["missing"].append(str(path))
                break
            html = path.read_text(encoding="utf-8")
            shot = evidence / name / f"{path.stem}.png"
            text = show_saved(driver, html, document_url(path), shot)
            recorded_shot = usable_screenshot(shot)
            access = classify_access(
                http_status=200,
                url=document_url(path),
                text=text,
                robot_texts=config["platforms"][name]["live"].get("robot_texts"),
                denied_texts=config["platforms"][name]["live"].get("denied_texts"),
            )
            if not extraction_allowed(access):
                state["blocked"].append({"platform": name, "path": str(path), "access": access})
                break
            document = parse_html(html, markers)
            if document.page != "search" or document.platform != name:
                state["skipped"].append({"path": str(path), "reason": "unexpected_page"})
                break
            seen = _seen(document.fields.get(spec["seen_field"]))
            _take(
                document,
                platform=name,
                spec=spec,
                city=city,
                query_key=query_key,
                labels=labels,
                screenshot=recorded_shot,
                seen_at=seen,
                source_path=str(path),
                branches=branches,
                state=state,
                limit=limit,
            )
            if name == "2gis" and limit is not None and _accepted_count(branches) >= limit:
                pending.clear()
                state["pending"][name] = []
                break
            scroll_href = document.hrefs.get(spec["scroll_field"])
            scroll_path = _local_href(folder, path, scroll_href)
            if scroll_path is not None:
                pending.insert(0, {"kind": "scroll", "path": str(scroll_path.relative_to(folder))})
            next_href = document.hrefs.get(spec["next_field"])
            next_path = _local_href(folder, path, next_href)
            if next_path is not None:
                pending.append({"kind": "page", "path": str(next_path.relative_to(folder))})
            elif next_href and str(next_href).lower().startswith(("http://", "https://")):
                state["skipped"].append({"path": str(path), "reason": "network_link"})
            state["pending"][name] = pending
            if kind == "scroll":
                state["scrolls"] = int(state.get("scrolls") or 0) + 1
                continue
            state["pages"] = int(state.get("pages") or 0) + 1
            decision = control.after_page()
            if decision == "pause":
                status = "paused"
                break
            if decision == "stop":
                status = "stopped"
                break
        else:
            state["pending"][name] = pending
    if status == "running" and not any(state["pending"].get(name) for name in platforms):
        status = "interrupted" if _access_stopped(state) else "discover_done"
    state["branches"] = [branch.as_progress() for branch in branches]
    state["phase"] = "discover" if status != "discover_done" else "collect"
    state["status"] = status
    return WalkResult(branches=branches, progress=state, status=status, evidence_dir=evidence)


def _manifest(folder: Path, *, city: str, category: str) -> dict:
    file = folder / "manifest.yaml"
    if not file.is_file():
        raise DiscoveryRefused(f"{file} was not found")
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("starts"), dict):
        raise DiscoveryRefused(f"{file} needs starts")
    if _key(str(data.get("city") or "")) != _key(city):
        raise DiscoveryRefused("saved pages are for a different city")
    if _key(str(data.get("category") or "")) != _key(category):
        raise DiscoveryRefused("saved pages are for a different category")
    return data


def _state(resume_state: dict | None, manifest: dict, platforms: tuple[str, ...]) -> dict:
    if resume_state:
        pending = {name: list((resume_state.get("pending") or {}).get(name) or []) for name in platforms}
        state = dict(resume_state)
        state["pending"] = pending
        state.setdefault("visited", [])
        state.setdefault("missing", [])
        state.setdefault("blocked", [])
        state.setdefault("skipped", [])
        state.setdefault("branches", [])
        state.setdefault("unmatched", [])
        return state
    starts = manifest["starts"]
    return {
        "phase": "discover",
        "pending": {
            name: [{"kind": "page", "path": str(starts[name])}] for name in platforms if starts.get(name)
        },
        "visited": [],
        "pages": 0,
        "scrolls": 0,
        "hits": 0,
        "duplicates": 0,
        "blocked": [],
        "missing": [],
        "skipped": [],
        "branches": [],
        "unmatched": [],
    }


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise DiscoveryRefused("page link escapes the saved directory")
    return resolved


def _local_href(root: Path, current: Path, href: str | None) -> Path | None:
    if not href or str(href).lower().startswith(("http://", "https://", "//")):
        return None
    return _inside(root, current.parent / str(href))


def _optional_rating(value: str | None) -> float | None:
    text = " ".join(str(value or "").split()).replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0:
        return None
    return number


def _optional_count(value: str | None) -> int | None:
    text = "".join(str(value or "").split())
    if not text.isdigit():
        return None
    return int(text)


def _seen(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return now().isoformat()
    try:
        from zhol.timeutil import parse_now

        return parse_now(text).isoformat()
    except ValueError:
        return now().isoformat()


def _accepted_count(branches: list[Branch]) -> int:
    """Distinct company identities accepted in this walk, from any platform."""
    return len(branches)


def _access_stopped(state: dict) -> bool:
    return any(str(item.get("access") or "") in _STOP_ACCESS for item in state.get("blocked") or [])


def _take(
    document: Any,
    *,
    platform: str,
    spec: dict[str, str],
    city: str,
    query_key: str,
    labels: Any,
    screenshot: str | None,
    seen_at: str,
    source_path: str,
    branches: list[Branch],
    state: dict,
    limit: int | None,
) -> None:
    adapter = _ADAPTERS[platform]
    needle = spec["firm_href_contains"]
    for candidate in document.candidates or []:
        fields = candidate.get("fields") or {}
        hrefs = candidate.get("hrefs") or {}
        url = adapter.firm_url(candidate.get("url"), needle)
        if url is None:
            state["skipped"].append({"path": source_path, "reason": "no_firm_url"})
            continue
        shown_city = clean_name(fields.get("city"))
        if shown_city and _key(shown_city) != _key(city):
            state["skipped"].append({"path": source_path, "reason": "other_city"})
            continue
        sector, category, subcategory = normalize_category(fields.get("category"), labels)
        if category is None:
            state["skipped"].append({"path": source_path, "reason": "unclassified", "label": fields.get("category")})
            continue
        if _key(category) != query_key and _key(subcategory or "") != query_key:
            state["skipped"].append({"path": source_path, "reason": "other_category", "label": fields.get("category")})
            continue
        name = clean_name(fields.get("name"))
        address = clean_address(fields.get("address"))
        firm = twogis_adapter.firm_id(url) if platform == "2gis" else None
        external = firm or adapter.external_id(url)
        phones, phone_display = _phones(fields.get(_PHONES))
        hit = Branch(
            name=name,
            category=category,
            subcategory=subcategory or None,
            sector=sector,
            address=address,
            city=shown_city or city,
            phones=phones,
            phone_display=phone_display,
            whatsapp=verified_whatsapp(hrefs.get("whatsapp"), fields.get("whatsapp")),
            instagram=verified_instagram(hrefs.get("instagram"), fields.get("instagram")),
            website=verified_website(hrefs.get("website") or hrefs.get("link"), fields.get("website") or fields.get("link")),
            email=verified_email(fields.get("email")),
            firm_id=firm,
            rating=_optional_rating(fields.get("rating")),
            review_count=_optional_count(fields.get("reviews_count")),
        )
        state["hits"] = int(state.get("hits") or 0) + 1
        evidence = PlatformEvidence(platform, url, external, screenshot, seen_at, source_path)
        if not external:
            _remember(state, hit, evidence, "unmatched")
            continue
        relation, target, phone_confirmed = _relate(branches, hit, evidence)
        if relation == "same" and target is not None:
            if hit.firm_id and not target.firm_id:
                target.firm_id = hit.firm_id
            _fill(target, hit)
            evidence.phone_confirmed = phone_confirmed
            target.platforms.setdefault(evidence.platform, evidence)
            state["duplicates"] = int(state.get("duplicates") or 0) + 1
            continue
        if relation == "ambiguous" and target is not None:
            target.phone_conflict = True
            target.alternatives.append(_trace(hit, evidence, "ambiguous"))
            _remember(state, hit, evidence, "ambiguous")
            continue
        if limit is not None and _accepted_count(branches) >= limit:
            _remember(state, hit, evidence, "limit", relation=relation if relation != "distinct" else None)
            continue
        evidence.phone_confirmed = True
        hit.platforms[platform] = evidence
        branches.append(hit)


def _phones(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    numbers: list[str] = []
    shown: list[str] = []
    for part in str(value or "").split(";"):
        number, display = phone_parts(part)
        if number and number not in numbers:
            numbers.append(number)
            if display:
                shown.append(display)
    return tuple(numbers), tuple(shown)


def _relate(
    branches: list[Branch],
    hit: Branch,
    evidence: PlatformEvidence,
) -> tuple[str, Branch | None, bool]:
    """One clear same-company match is attached. Two candidates stay unresolved."""
    same: list[tuple[Branch, bool]] = []
    ambiguous: list[Branch] = []
    other: list[Branch] = []
    incoming = profile_from_hit(hit, evidence)
    for branch in branches:
        relation = relate(profile_from_branch(branch), incoming)
        if relation.kind == "same":
            same.append((branch, relation.phone_confirmed))
        elif relation.kind == "ambiguous":
            ambiguous.append(branch)
        elif relation.kind == "other_address":
            other.append(branch)
    if len(same) == 1 and not ambiguous:
        branch, phone = same[0]
        return "same", branch, phone
    if same or ambiguous:
        return "ambiguous", (ambiguous or [same[0][0]])[0], False
    if other:
        return "other_address", other[0], False
    return "distinct", None, False


def _remember(state: dict, hit: Branch, evidence: PlatformEvidence, reason: str, relation: str | None = None) -> None:
    rows = list(state.get("unmatched") or [])
    item = _trace(hit, evidence, reason, relation)
    if any(row.get("item_key") == item["item_key"] for row in rows):
        state["duplicates"] = int(state.get("duplicates") or 0) + 1
        return
    rows.append(item)
    state["unmatched"] = rows


def _trace(hit: Branch, evidence: PlatformEvidence, reason: str, relation: str | None = None) -> dict:
    external = evidence.external_id or hit.firm_id or "unknown"
    row = {
        "item_key": f"trace:{evidence.platform}:{external}",
        "platform": evidence.platform,
        "external_id": evidence.external_id,
        "url": evidence.url,
        "name": hit.name,
        "address": hit.address,
        "phones": list(hit.phones),
        "phone_display": list(hit.phone_display),
        "reason": reason,
        "screenshot_path": evidence.screenshot_path,
        "seen_at": evidence.seen_at,
        "source_path": evidence.source_path,
    }
    if relation:
        row["relation"] = relation
    return row


def _fill(branch: Branch, hit: Branch) -> None:
    for name in ("name", "category", "subcategory", "sector", "address", "city", "whatsapp", "instagram", "website", "email"):
        if getattr(branch, name) in (None, "") and getattr(hit, name) not in (None, ""):
            setattr(branch, name, getattr(hit, name))
    if hit.phones and not branch.phones:
        branch.phones = hit.phones
        branch.phone_display = hit.phone_display
    elif hit.phones and phones_overlap(list(branch.phones), list(hit.phones)):
        merged = list(branch.phones)
        shown = list(branch.phone_display)
        for number, display in zip(hit.phones, hit.phone_display or hit.phones):
            if number not in merged:
                merged.append(number)
                shown.append(display)
        branch.phones = tuple(merged)
        branch.phone_display = tuple(shown)
    if branch.rating is None and hit.rating is not None:
        branch.rating = hit.rating
    if branch.review_count is None and hit.review_count is not None:
        branch.review_count = hit.review_count
