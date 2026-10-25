"""Selenium collector for Yandex Maps and Google Maps.

A loopback fixture needs no grant. A live host opens only when that source's
own scope is set. The 2GIS collector is not used and is not modified.
Each accepted card is committed before the next card is opened.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from browser_ops import BrowserTimeout
from browser_ops import eval_js
from browser_ops import is_browser_crash
from browser_ops import open_url
from browser_ops import wait_js
from zhol.agents.scout.browser_2gis import local_base_url
from zhol.agents.scout.browser_google import SOURCE_MODE as GOOGLE_MODE
from zhol.agents.scout.browser_maps import BrowserCollectionRefused
from zhol.agents.scout.browser_yandex import SOURCE_MODE as YANDEX_MODE
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.identity import Profile, Ref, classify_links
from zhol.agents.scout.identity_store import relate_stored
from zhol.agents.scout.live_2gis import _mark, _progress, _save, _status
from zhol.agents.scout.normalize import phone_parts
from zhol.agents.scout.persist import find_reusable, insert_snapshot, remember_collection_context, remember_platform_id, remember_search_query
from zhol.agents.scout.selenium_collector import (
    _assign_progress,
    _capture,
    _close_driver,
    _commit_failure,
    _empty_metrics,
    _finish_metrics,
    _lock_batch,
    _metrics_of,
    _note_browser_restart,
    _retryable_ids,
)
from zhol.browser.discovery.normalize import clean_address, verified_instagram, verified_website, verified_whatsapp
from zhol.browser.discovery.types import RunControl
from zhol.browser.maps.visible import parse_card, place_id
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company, CompanyPlatformId
from zhol.timeutil import now

NAV_TIMEOUT_MS = 15_000
MAX_SEARCH_PAGES = 20
MAX_SCROLLS = 20
CARD_KIND = "maps_selenium"

_BLOCKED = (
    ("captcha", ("captcha", "recaptcha", "hcaptcha", "smartcaptcha", "капча", "не робот")),
    ("access_restriction", ("доступ ограничен", "доступ запрещ", "access denied")),
)


class MapAccessBlocked(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.checkpoint: dict[str, Any] | None = None
        super().__init__(message)


class _MapCheckSettled(Exception):
    """The captcha or access check already wrote the batch status."""

    def __init__(self, *, still: bool = False, checkpoint: dict[str, Any] | None = None) -> None:
        self.still = still
        self.checkpoint = checkpoint
        super().__init__("manual check settled")


@dataclass(frozen=True)
class MapPlace:
    platform: str
    external_id: str
    name: str
    city: str
    shown_city: str | None
    address: str | None
    address_reason: str | None
    phones: tuple[str, ...]
    phone_display: tuple[str, ...]
    phones_reason: str | None
    whatsapp: str | None
    whatsapp_reason: str | None
    instagram: str | None
    instagram_reason: str | None
    website: str | None
    website_reason: str | None
    rating: float | None
    rating_reason: str | None
    review_count: int | None
    review_reason: str | None
    category: str | None
    category_reason: str | None
    url: str
    query: str
    collected_at: str
    screenshot_path: str | None = None
    city_check: str = "match"


def _adapter(platform: str) -> Any:
    if platform == "yandex":
        from zhol.browser.yandex import selenium as adapter

        return adapter
    if platform == "google":
        from zhol.browser.google import selenium as adapter

        return adapter
    raise BatchError("Неизвестный источник сбора.")


def _source_mode(platform: str) -> str:
    return YANDEX_MODE if platform == "yandex" else GOOGLE_MODE


def _policy(platform: str) -> Any:
    if platform == "yandex":
        from zhol.agents.scout import browser_yandex as policy

        return policy
    from zhol.agents.scout import browser_google as policy

    return policy


def _require_origin(platform: str) -> str:
    origin = local_base_url()
    if origin:
        return origin
    policy = _policy(platform)
    if policy.grant_scope() is None:
        raise BrowserCollectionRefused(policy.BLOCKER)
    return policy.LIVE_ORIGIN


_POSTAL_LOCALITY = re.compile(r"([А-ЯЁ][а-яё\-]{2,})\s+\d{6}\b")
_NOT_LOCALITY = frozenset(
    {
        "улица",
        "ул",
        "проспект",
        "пр",
        "микрорайон",
        "дом",
        "офис",
        "здание",
        "район",
        "угол",
    }
)
UNCONFIRMED_CITY = "Город не подтверждён"


def city_verdict(requested: str, address: str | None, shown: str | None) -> tuple[str, str | None]:
    """Compare the card with the requested city. A missing or foreign city is not stored as that city."""
    from zhol.browser.maps.visible import _CITY

    requested_fold = " ".join(requested.casefold().replace("ё", "е").split())
    found: dict[str, str] = {}

    def add(value: str | None) -> None:
        name = " ".join(str(value or "").split())
        folded = name.casefold().replace("ё", "е")
        if not name or folded in _NOT_LOCALITY:
            return
        found[folded] = name

    for match in _CITY.finditer(address or ""):
        add(match.group(1))
    postal = _POSTAL_LOCALITY.search(address or "")
    if postal:
        add(postal.group(1))
    add(shown)
    if not found:
        return "not_shown", None
    if len(found) > 1:
        return "ambiguous", None
    folded, label = next(iter(found.items()))
    if folded == requested_fold:
        return "match", requested
    return "out_of_city", label


def _apply_city(place: MapPlace, requested: str) -> MapPlace:
    verdict, locality = city_verdict(requested, place.address, place.shown_city)
    if verdict == "match":
        return replace(place, city=requested, shown_city=locality, city_check="match")
    if verdict == "out_of_city" and locality:
        return replace(place, city=locality, shown_city=locality, city_check="out_of_city")
    return replace(place, city=UNCONFIRMED_CITY, shown_city=None, city_check=verdict)


def _city_name(city: str, platform: str) -> str:
    from zhol.agents.scout.localities import require_locality, supports

    try:
        found = require_locality(city)
    except ValueError as exc:
        raise BatchError(str(exc)) from exc
    if not supports(found.name, platform):
        label = "Яндекс Картах" if platform == "yandex" else "Google Maps"
        raise BatchError(
            f"Город «{found.name}» не поддержан в {label}. Сбор не переключается на Алматы."
        )
    return found.name


def _blocked_kind(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    visible = " ".join(str(payload.get("text") or "").split())
    if visible.casefold() == "limited":
        return "access_restriction"
    text = f"{payload.get('access') or ''} {payload.get('text') or ''}".casefold()
    for kind, tokens in _BLOCKED:
        if any(token in text for token in tokens):
            return kind
    return None


def _raise_if_blocked(payload: dict[str, Any] | None) -> None:
    kind = _blocked_kind(payload)
    if kind == "captcha":
        raise MapAccessBlocked(kind, "Площадка показала проверку «не робот». Проверка не обходится.")
    if kind == "access_restriction":
        raise MapAccessBlocked(kind, "Доступ к странице ограничен. Ограничение не обходится.")


def _rating(raw: str, parsed: str | None, note: str) -> tuple[float | None, str]:
    token = (raw or "").replace(",", ".").strip()
    if re.fullmatch(r"[0-5](?:\.\d)?", token):
        return float(token), None if token else "not_shown"
    if note == "shown" and parsed and re.fullmatch(r"[0-5](?:\.\d)?", parsed.replace(",", ".")):
        return float(parsed.replace(",", ".")), None
    if note == "ambiguous":
        return None, "ambiguous"
    return None, "not_shown"


def _count(raw: str, parsed: str | None, note: str) -> tuple[int | None, str | None]:
    if raw:
        digits = re.sub(r"\D", "", raw)
        if digits:
            return int(digits), None
    if note == "shown" and parsed and str(parsed).isdigit():
        return int(parsed), None
    if note == "ambiguous":
        return None, "ambiguous"
    return None, "not_shown"


def _phone_values(raw_list: list[str], parsed: str | None, note: str) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    sources = [item for item in raw_list if str(item).strip()]
    if not sources and note == "shown" and parsed:
        sources = [part.strip() for part in str(parsed).split(",") if part.strip()]
    numbers: list[str] = []
    shown: list[str] = []
    for raw in sources:
        number, display = phone_parts(raw)
        if number and number not in numbers:
            numbers.append(number)
            if display:
                shown.append(display)
    if numbers:
        return tuple(numbers), tuple(shown), None
    if note == "ambiguous":
        return (), (), "ambiguous"
    return (), (), "not_shown"


def _one_contact(value: str | None, note: str) -> tuple[str | None, str | None]:
    if value:
        return value, None
    if note == "ambiguous":
        return None, "ambiguous"
    return None, "not_shown"


def normalize_place(
    raw: dict[str, Any],
    *,
    platform: str,
    external_id: str,
    url: str,
    city: str,
    query: str,
) -> MapPlace | None:
    """Keep a field only when this card showed it. Missing contacts stay null."""
    name = " ".join(str(raw.get("name") or "").split())
    if not external_id or not name:
        return None
    if place_id(platform, url) != external_id:
        return None
    text = str(raw.get("text") or "")
    hrefs = [str(item) for item in raw.get("hrefs") or [] if item]
    parsed, notes = parse_card(text, name=name, hrefs=hrefs, platform=platform)
    address = clean_address(str(raw.get("address") or "")) or None
    address_reason: str | None = None
    if address is None:
        if notes.get("address") == "shown" and parsed.get("address"):
            address = clean_address(parsed.get("address"))
        else:
            address_reason = "ambiguous" if notes.get("address") == "ambiguous" else "not_shown"
    phones, phone_display, phones_reason = _phone_values(
        [str(item) for item in raw.get("phones") or []],
        parsed.get("phones"),
        str(notes.get("phones") or "not_shown"),
    )
    website = verified_website(str(raw.get("website") or "") or None, None)
    if website is None and notes.get("website") == "shown":
        website = verified_website(parsed.get("website"), None)
    website_reason = None if website else ("ambiguous" if notes.get("website") == "ambiguous" else "not_shown")
    whatsapp = verified_whatsapp(None, parsed.get("whatsapp"))
    if whatsapp is None:
        for href in hrefs:
            whatsapp = verified_whatsapp(href, None)
            if whatsapp:
                break
    whatsapp, whatsapp_reason = _one_contact(whatsapp, str(notes.get("whatsapp") or "not_shown"))
    instagram = verified_instagram(None, parsed.get("instagram"))
    if instagram is None:
        for href in hrefs:
            instagram = verified_instagram(href, None)
            if instagram:
                break
    instagram, instagram_reason = _one_contact(instagram, str(notes.get("instagram") or "not_shown"))
    rating, rating_reason = _rating(str(raw.get("rating") or ""), parsed.get("rating"), str(notes.get("rating") or ""))
    review_count, review_reason = _count(
        str(raw.get("reviews") or ""),
        parsed.get("reviews_count"),
        str(notes.get("reviews_count") or ""),
    )
    category = " ".join(str(raw.get("category") or parsed.get("category") or "").split()) or None
    note = str(notes.get("category") or "")
    if category:
        category_reason = None
    elif note == "ambiguous":
        category_reason = "ambiguous"
    elif note == "unknown_label":
        category_reason = "unknown_label"
    else:
        category_reason = "not_shown"
    shown_city = parsed.get("city") if notes.get("city") == "shown" else None
    return MapPlace(
        platform=platform,
        external_id=external_id,
        name=name,
        city=city,
        shown_city=shown_city,
        address=address,
        address_reason=address_reason,
        phones=phones,
        phone_display=phone_display,
        phones_reason=phones_reason,
        whatsapp=whatsapp,
        whatsapp_reason=whatsapp_reason,
        instagram=instagram,
        instagram_reason=instagram_reason,
        website=website,
        website_reason=website_reason,
        rating=rating,
        rating_reason=rating_reason,
        review_count=review_count,
        review_reason=review_reason,
        category=category,
        category_reason=category_reason,
        url=url,
        query=query,
        collected_at=datetime.now().astimezone().isoformat(),
    )


def _discover(
    driver: Any,
    adapter: Any,
    *,
    origin: str,
    platform: str,
    query: str,
    city: str = "Алматы",
    limit: int,
    known: set[str],
    reserved: set[str],
    stopped: Callable[[], bool],
    checkpoint: dict[str, Any] | None = None,
    on_captcha: Callable[[MapAccessBlocked], str] | None = None,
) -> dict[str, Any]:
    queued: list[dict[str, str]] = []
    seen: set[str] = set()
    duplicates = 0
    pages = 0
    scrolls = 0
    ordinary = 0
    stored = checkpoint if isinstance(checkpoint, dict) else {}
    start_page = stored.get("next_page")
    page_cursor = start_page if isinstance(start_page, int) and start_page >= 1 else 1
    followed_pager = bool(stored.get("followed_pager"))

    def pack(*, interrupted: bool, done: bool, stop_reason: str, next_page: int) -> dict[str, Any]:
        return {
            "items": queued,
            "duplicates": duplicates,
            "pages_loaded": pages,
            "scrolls": scrolls,
            "interrupted": interrupted,
            "complete": done,
            "stop_reason": stop_reason,
            "next_page": next_page,
            "followed_pager": followed_pager,
        }

    def take(items: list[dict[str, Any]]) -> bool:
        nonlocal ordinary, duplicates
        for item in items:
            external_id = str(item.get("externalId") or "")
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            if external_id in known and external_id not in reserved:
                continue
            if external_id not in reserved and ordinary >= limit:
                continue
            href = str(item.get("href") or "")
            queued.append(
                {
                    "external_id": external_id,
                    "url": adapter.card_url(origin, external_id, href),
                    "list_name": str(item.get("listName") or ""),
                }
            )
            if external_id not in reserved:
                ordinary += 1
            if ordinary >= limit and reserved <= {row["external_id"] for row in queued}:
                return True
        return False

    def point(page_no: int) -> dict[str, Any]:
        return {
            "pages_loaded": pages,
            "scrolls": scrolls,
            "next_page": page_no,
            "followed_pager": followed_pager,
        }

    def read_results(page_no: int) -> dict[str, Any]:
        try:
            _raise_if_blocked(eval_js(driver, adapter.PAGE_TEXT_JS))
        except MapAccessBlocked as exc:
            exc.checkpoint = point(page_no)
            if exc.kind != "captcha" or on_captcha is None:
                raise
            if on_captcha(exc) != "continue":
                raise _MapCheckSettled() from exc
            try:
                _raise_if_blocked(eval_js(driver, adapter.PAGE_TEXT_JS))
            except MapAccessBlocked as again:
                raise _MapCheckSettled(still=True, checkpoint=point(page_no)) from again
        wait_js(driver, adapter.READY_SEARCH_JS, NAV_TIMEOUT_MS)
        data = eval_js(driver, adapter.READ_SEARCH_JS) or {}
        return data if isinstance(data, dict) else {}

    if platform == "google":
        open_url(driver, adapter.search_page_url(origin, query, 1, city=city), NAV_TIMEOUT_MS)
        pages = 1
        stalls = 0
        previous = -1
        while scrolls <= MAX_SCROLLS:
            if stopped():
                return pack(interrupted=True, done=False, stop_reason="stopped", next_page=1)
            data = read_results(1)
            duplicates += int(data.get("duplicates") or 0)
            if take(list(data.get("items") or [])):
                return pack(interrupted=False, done=False, stop_reason="limit", next_page=1)
            if data.get("done") is True:
                return pack(interrupted=False, done=True, stop_reason="confirmed_end", next_page=1)
            count = len(data.get("items") or [])
            if count == previous:
                stalls += 1
                if stalls >= 2:
                    return pack(interrupted=False, done=False, stop_reason="stall", next_page=1)
            else:
                stalls = 0
            previous = count
            if not eval_js(driver, adapter.SCROLL_JS):
                return pack(interrupted=False, done=False, stop_reason="stall", next_page=1)
            scrolls += 1
        return pack(interrupted=False, done=False, stop_reason="stall", next_page=1)

    for page in range(page_cursor, MAX_SEARCH_PAGES + 1):
        if stopped():
            return pack(interrupted=True, done=False, stop_reason="stopped", next_page=page)
        open_url(driver, adapter.search_page_url(origin, query, page, city=city), NAV_TIMEOUT_MS)
        data = read_results(page)
        pages += 1
        duplicates += int(data.get("duplicates") or 0)
        if take(list(data.get("items") or [])):
            return pack(interrupted=False, done=False, stop_reason="limit", next_page=page)
        if data.get("done") is True:
            return pack(interrupted=False, done=True, stop_reason="confirmed_end", next_page=page)
        next_page = page + 1
        numbers = {int(item) for item in data.get("pageNumbers") or [] if isinstance(item, int) or str(item).isdigit()}
        if next_page in numbers:
            followed_pager = True
            continue
        if numbers and max(numbers) <= page:
            return pack(interrupted=False, done=True, stop_reason="confirmed_end", next_page=page)
        if followed_pager:
            return pack(interrupted=False, done=True, stop_reason="confirmed_end", next_page=page)
        return pack(interrupted=False, done=False, stop_reason="stall", next_page=page)
    return pack(interrupted=False, done=False, stop_reason="stall", next_page=MAX_SEARCH_PAGES)


def _wait(control: RunControl, delay_range: tuple[float, float] | None) -> None:
    low, high = delay_range or (0.0, 0.0)
    if high < low:
        low, high = high, low
    seconds = 0.0 if high <= 0 else random.uniform(max(0.0, low), high)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if control._stopped or control._paused:
            return
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _observed(url: str, seen_at: str, screenshot: str | None, *, reason: str | None, display: tuple[str, ...] = ()) -> dict[str, Any]:
    source: dict[str, Any] = {
        "url": url,
        "seen_at": seen_at,
        "read_by": "code",
        "screenshot": screenshot,
    }
    if reason:
        source["reason"] = reason
    if display:
        source["display"] = list(display)
    return source


def _put(fields: dict[str, Any], sources: dict[str, Any], key: str, value: Any, source: dict[str, Any], reason: str | None) -> None:
    if key not in fields:
        return
    if reason == "not_shown" or reason == "ambiguous":
        fields[key] = None
        return
    fields[key] = value
    sources[key] = source


def _snapshot(session: Session, batch: CollectionBatch, company: Company, place: MapPlace) -> None:
    fields = {key: None for key in field_keys(place.platform)}
    sources: dict[str, Any] = {}
    shot = place.screenshot_path
    seen_at = place.collected_at
    _put(fields, sources, "name", place.name, _observed(place.url, seen_at, shot, reason=None), None)
    _put(
        fields,
        sources,
        "address",
        place.address,
        _observed(place.url, seen_at, shot, reason=place.address_reason),
        place.address_reason,
    )
    city_reason = None if place.city_check in {"match", "out_of_city"} else place.city_check
    _put(
        fields,
        sources,
        "city",
        place.shown_city if city_reason is None else None,
        _observed(place.url, seen_at, shot, reason=city_reason),
        city_reason,
    )
    _put(
        fields,
        sources,
        "phones",
        list(place.phones) if place.phones_reason is None else None,
        _observed(place.url, seen_at, shot, reason=place.phones_reason, display=place.phone_display),
        place.phones_reason,
    )
    _put(
        fields,
        sources,
        "website",
        place.website,
        _observed(place.url, seen_at, shot, reason=place.website_reason),
        place.website_reason,
    )
    _put(
        fields,
        sources,
        "whatsapp",
        place.whatsapp,
        _observed(place.url, seen_at, shot, reason=place.whatsapp_reason),
        place.whatsapp_reason,
    )
    _put(
        fields,
        sources,
        "instagram",
        place.instagram,
        _observed(place.url, seen_at, shot, reason=place.instagram_reason),
        place.instagram_reason,
    )
    _put(
        fields,
        sources,
        "rating",
        place.rating,
        _observed(place.url, seen_at, shot, reason=place.rating_reason),
        place.rating_reason,
    )
    _put(
        fields,
        sources,
        "reviews_count",
        place.review_count,
        _observed(place.url, seen_at, shot, reason=place.review_reason),
        place.review_reason,
    )
    _put(
        fields,
        sources,
        "category",
        place.category,
        _observed(place.url, seen_at, shot, reason=place.category_reason),
        place.category_reason if place.category_reason in {"not_shown", "ambiguous"} else None,
    )
    category_source = sources.get("category")
    if not isinstance(category_source, dict):
        category_source = _observed(place.url, seen_at, shot, reason=place.category_reason)
        sources["category"] = category_source
    category_source["checked"] = True
    if place.category_reason:
        category_source["reason"] = place.category_reason
    remember_platform_id(session, company.id, place.platform, place.external_id, place.url)
    snapshot = insert_snapshot(
        session,
        company_id=company.id,
        platform=place.platform,
        purpose="prospect",
        match_status="found",
        url=place.url,
        fields=fields,
        field_sources=sources,
        screenshot_path=place.screenshot_path,
        collection_key=f"{_source_mode(place.platform)}:{batch.id}:{place.external_id}",
        page_file=None,
        kind=CARD_KIND,
        raw_path=None,
    )
    remember_search_query(snapshot, place.query)
    from zhol.agents.scout.localities import region_of

    remember_collection_context(snapshot, city=batch.city, region=region_of(batch.city))


def _stored_profile(session: Session, company: Company, platform: str) -> Profile:
    """Address and phones come from this platform's own newest snapshot.

    A newer snapshot of another platform is not used as this card's address or phone.
    """
    refs: list[Ref] = []
    if company.id_2gis:
        refs.append(Ref("2gis", company.id_2gis))
    rows = session.scalars(select(CompanyPlatformId).where(CompanyPlatformId.company_id == company.id)).all()
    for row in rows:
        if row.external_id and not any(item.platform == row.platform and item.external_id == row.external_id for item in refs):
            refs.append(Ref(row.platform, row.external_id))
    snap = session.scalar(
        select(CardSnapshot)
        .where(CardSnapshot.company_id == company.id, CardSnapshot.platform == platform)
        .order_by(CardSnapshot.collected_at.desc(), CardSnapshot.id.desc())
    )
    fields = snap.fields if snap is not None and isinstance(snap.fields, dict) else {}
    address = fields.get("address") if isinstance(fields.get("address"), str) else None
    phones: list[str] = []
    raw_phones = fields.get("phones")
    if isinstance(raw_phones, list):
        phones.extend(str(item) for item in raw_phones if item)
    return Profile(company.name, address, tuple(phones), tuple(refs))


def _find_company(session: Session, place: MapPlace) -> tuple[Company | None, str | None]:
    owner = session.scalar(
        select(CompanyPlatformId).where(
            CompanyPlatformId.platform == place.platform,
            CompanyPlatformId.external_id == place.external_id,
        )
    )
    if owner is not None:
        company = session.get(Company, owner.company_id)
        return company, "platform_id"
    incoming = Profile(place.name, place.address, place.phones, (Ref(place.platform, place.external_id),))
    action, company, _phone = classify_links(relate_stored(session, place.city, place.platform, incoming))
    if action == "attach":
        return company, "same_identity"
    if action == "review":
        return None, "ambiguous"
    return None, None


def _google_place_ready(place: MapPlace) -> bool:
    from zhol.agents.scout.google_card import google_place_ready

    return google_place_ready(place.name, place.url, place.external_id)


def _commit_place(factory: sessionmaker[Session], batch_id: int, place: MapPlace) -> str:
    started = time.perf_counter()
    for attempt in range(2):
        try:
            with factory() as session, session.begin():
                return _commit_place_locked(session, batch_id, place, started)
        except IntegrityError:
            if attempt == 1:
                raise
    raise RuntimeError("company commit did not finish")


def _commit_place_locked(session: Session, batch_id: int, place: MapPlace, started: float) -> str:
    batch = _lock_batch(session, batch_id)
    if batch is None:
        return "duplicate"
    if place.platform == "google" and not _google_place_ready(place):
        _reject_locked(
            session,
            batch,
            place.external_id or "google-incomplete",
            "Карточка Google без названия или ссылки на карточку",
            "incomplete_card",
        )
        return "rejected"
    progress = dict(batch.progress or {})
    seen = {str(item) for item in progress.get("seen_ids") or []}
    item = session.scalar(
        select(CollectionBatchItem).where(
            CollectionBatchItem.batch_id == batch_id,
            CollectionBatchItem.id_2gis == place.external_id,
        )
    )
    metrics = _metrics_of(progress)
    elapsed = (time.perf_counter() - started) * 1000
    metrics["persistence_ms_total"] = float(metrics["persistence_ms_total"]) + elapsed
    metrics["persistence_samples"] = int(metrics["persistence_samples"]) + 1
    if item is not None and item.status == "completed":
        seen.add(place.external_id)
        progress["seen_ids"] = sorted(seen)
        progress["hits"] = len(seen)
        metrics["duplicate_companies"] = int(metrics["duplicate_companies"]) + 1
        metrics["companies_processed"] = int(metrics["companies_processed"]) + 1
        _finish_metrics(metrics)
        progress["metrics"] = metrics
        _assign_progress(batch, progress)
        return "duplicate"
    company, match = _find_company(session, place)
    if match == "ambiguous":
        _reject_locked(session, batch, place.external_id, "Совпадение компании неоднозначно", "ambiguous")
        return "ambiguous"
    if item is None:
        metrics["companies_processed"] = int(metrics["companies_processed"]) + 1
    created = company is None
    if company is None:
        company = Company(
            id_2gis=None,
            name=place.name,
            city=place.city or batch.city,
            niche=place.query,
            whatsapp=place.whatsapp,
            instagram=place.instagram,
            source=_source_mode(place.platform),
        )
        session.add(company)
        session.flush()
    key = f"{_source_mode(place.platform)}:{batch.id}:{place.external_id}"
    wrote = find_reusable(session, company.id, place.platform, key) is None
    if wrote:
        _snapshot(session, batch, company, place)
    if item is None:
        session.add(
            CollectionBatchItem(
                batch_id=batch_id,
                id_2gis=place.external_id,
                company_id=company.id,
                status="completed",
                detail={
                    "name": company.name,
                    "city": company.city,
                    "category": place.query,
                    "reason": None if created else "already_present",
                    "platform": place.platform,
                    "city_check": place.city_check,
                },
            )
        )
    else:
        item.status = "completed"
        item.company_id = company.id
        item.detail = {
            "name": company.name,
            "city": company.city,
            "category": place.query,
            "reason": None if created else "already_present",
            "platform": place.platform,
            "city_check": place.city_check,
        }
        flag_modified(item, "detail")
    seen.add(place.external_id)
    rejected = {str(value) for value in progress.get("rejected_ids") or []}
    rejected.discard(place.external_id)
    progress["rejected_ids"] = sorted(rejected)
    progress["seen_ids"] = sorted(seen)
    progress["hits"] = len(seen)
    if place.city_check != "match":
        flags = [item for item in (progress.get("city_flags") or []) if isinstance(item, dict)]
        flags.append(
            {
                "external_id": place.external_id,
                "name": place.name,
                "check": place.city_check,
                "shown": place.shown_city,
            }
        )
        progress["city_flags"] = flags
    if created:
        progress["new_companies"] = int(progress.get("new_companies") or 0) + 1
    else:
        progress["existing_companies"] = int(progress.get("existing_companies") or 0) + 1
        metrics["duplicate_companies"] = int(metrics["duplicate_companies"]) + 1
    metrics["companies_committed"] = int(metrics["companies_committed"]) + 1
    if wrote:
        metrics["snapshots_written"] = int(metrics["snapshots_written"]) + 1
    _finish_metrics(metrics)
    progress["metrics"] = metrics
    _assign_progress(batch, progress)
    return "new" if created else "existing"


def _reject_locked(session: Session, batch: CollectionBatch, external_id: str, reason: str, classification: str) -> None:
    progress = dict(batch.progress or {})
    seen = {str(item) for item in progress.get("seen_ids") or []}
    if external_id in seen:
        return
    item = session.scalar(
        select(CollectionBatchItem).where(
            CollectionBatchItem.batch_id == batch.id,
            CollectionBatchItem.id_2gis == external_id,
        )
    )
    if item is None:
        session.add(
            CollectionBatchItem(
                batch_id=batch.id,
                id_2gis=external_id,
                status="blocked",
                attempts=1,
                detail={"reason": reason[:300], "classification": classification, "retryable": False},
            )
        )
    else:
        item.status = "blocked"
        item.detail = {"reason": reason[:300], "classification": classification, "retryable": False}
        flag_modified(item, "detail")
    rejected = {str(value) for value in progress.get("rejected_ids") or []}
    rejected.add(external_id)
    progress["rejected_ids"] = sorted(rejected)
    metrics = _metrics_of(progress)
    metrics["failed_records"] = int(metrics["failed_records"]) + 1
    progress["metrics"] = metrics
    _assign_progress(batch, progress)


def _open(factory: sessionmaker[Session], *, platform: str, city: str, category: str, limit: int, out: Path) -> int:
    mode = _source_mode(platform)
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city=city,
            category=category,
            platforms=[platform],
            item_limit=limit,
            source_mode=mode,
            status="running",
            out_path=str(out),
            catalog_path=mode,
            progress={
                "phase": "collect",
                "active_query": category,
                "platform": platform,
                "authorization_scope": None if local_base_url() else _policy(platform).grant_scope(),
                "seen_ids": [],
                "hits": 0,
                "duplicates": 0,
                "new_companies": 0,
                "existing_companies": 0,
                "errors": 0,
                "rejected_ids": [],
                "metrics": _empty_metrics(),
            },
        )
        session.add(batch)
        session.flush()
        return int(batch.id)


MESSAGE_MANUAL_UNAVAILABLE = (
    "Ручная проверка недоступна: Chrome запущен без окна. "
    "Программа не решает проверку и не открывает новое окно. "
    "Сбор не продолжается. Сохраненные компании остаются."
)
MESSAGE_NO_DISPLAY = (
    "Ручная проверка недоступна: у процесса сборщика нет экрана. "
    "Программа не решает проверку и не открывает новое окно. "
    "Сбор не продолжается. Сохраненные компании остаются."
)
MESSAGE_SOURCE_CLOSED = (
    "Ручная проверка не предлагается: этот источник не разрешен. "
    "Проверка не обходится. Сбор не продолжается. Сохраненные компании остаются."
)
_VERIFY_WAIT_SECONDS = 20 * 60


def _verification_unavailable(progress: dict[str, Any]) -> bool:
    record = progress.get("manual_verification")
    return isinstance(record, dict) and record.get("outcome") == "unavailable"


def _manual_possible(driver: Any, platform: str) -> tuple[bool, str]:
    """Operator help stays on this session. A headless Chrome cannot show the check."""
    from zhol.agents.scout.selenium_collector import visible_chrome_available

    if getattr(driver, "_zhol_headed", None) is not True:
        return False, "headless"
    if not visible_chrome_available():
        return False, "no_display"
    if local_base_url() is None and _policy(platform).grant_scope() is None:
        return False, "not_permitted"
    return True, "ok"


def _page_url(driver: Any) -> str:
    try:
        return str(getattr(driver, "current_url", "") or "")[:500]
    except Exception:
        return ""


def _write_check(
    factory: sessionmaker[Session],
    batch_id: int,
    *,
    status: str,
    message: str,
    checkpoint: dict[str, Any] | None,
    verification: dict[str, Any] | None,
    interruption: dict[str, Any] | None,
) -> None:
    with factory() as session, session.begin():
        batch = _lock_batch(session, batch_id)
        if batch is None or batch.status == "stopped":
            return
        progress = dict(batch.progress or {})
        progress["error"] = message[:300]
        if checkpoint:
            progress["discovery_checkpoint"] = dict(checkpoint)
        if verification is None:
            progress.pop("manual_verification", None)
        else:
            progress["manual_verification"] = verification
        if interruption is None:
            progress.pop("interruption", None)
        else:
            progress["interruption"] = interruption
        progress.pop("admin_signal", None)
        _assign_progress(batch, progress)
        batch.status = status
        batch.finished_at = now() if status in {"stopped", "completed", "interrupted"} else None


def _unavailable_message(reason: str) -> str:
    if reason == "no_display":
        return MESSAGE_NO_DISPLAY
    if reason == "not_permitted":
        return MESSAGE_SOURCE_CLOSED
    return MESSAGE_MANUAL_UNAVAILABLE


def _record_unavailable(
    factory: sessionmaker[Session],
    batch_id: int,
    exc: MapAccessBlocked,
    checkpoint: dict[str, Any] | None,
    reason: str,
) -> None:
    message = _unavailable_message(reason)
    _write_check(
        factory,
        batch_id,
        status="interrupted",
        message=message,
        checkpoint=checkpoint,
        verification={
            "outcome": "unavailable",
            "reason": str(exc)[:300],
            "url": "",
            "browser": "closed",
            "headed": False,
            "message": message,
        },
        interruption={
            "classification": "captcha",
            "retryable": False,
            "reason": message[:300],
            "manual_verification": "unavailable",
        },
    )


def _respond_to_captcha(
    factory: sessionmaker[Session],
    batch_id: int,
    driver: Any,
    platform: str,
    exc: MapAccessBlocked,
    *,
    control: RunControl,
    sleep: Callable[[float], None],
    max_wait_seconds: float,
) -> str:
    """Wait on the same window when a person can see it. Never solves the check."""
    from zhol.agents.scout.selenium_collector import (
        AWAITING_MANUAL_VERIFICATION,
        MESSAGE_DENIED,
        MESSAGE_FAILED,
        MESSAGE_OPEN,
        MESSAGE_PASSED,
        MESSAGE_STILL,
        MESSAGE_STOP,
        MESSAGE_TIMEOUT,
        VERIFY_FAILED,
        _mark_browser,
        _wait_for_operator,
    )

    checkpoint = exc.checkpoint if isinstance(exc.checkpoint, dict) else None
    possible, why = _manual_possible(driver, platform)
    if not possible:
        _record_unavailable(factory, batch_id, exc, checkpoint, why)
        return "halt"
    scope = None if local_base_url() else _policy(platform).grant_scope()
    source_kind = "localhost" if local_base_url() else _source_mode(platform)
    _write_check(
        factory,
        batch_id,
        status=AWAITING_MANUAL_VERIFICATION,
        message=MESSAGE_OPEN,
        checkpoint=checkpoint,
        verification={
            "outcome": "pending",
            "reason": str(exc)[:300],
            "url": _page_url(driver),
            "browser": "closed",
            "headed": True,
            "authorization_scope": scope,
            "source_kind": source_kind,
            "message": MESSAGE_OPEN,
        },
        interruption=None,
    )
    _mark_browser(factory, batch_id, "open", headed=True)
    try:
        import parser_service

        parser_service.show_owned_window(driver)
    except Exception:
        pass
    if control._stopped:
        signal = "stop"
    else:
        signal = _wait_for_operator(factory, batch_id, control, sleep, max_wait_seconds)
    if signal == "timeout":
        _write_check(
            factory,
            batch_id,
            status=AWAITING_MANUAL_VERIFICATION,
            message=MESSAGE_TIMEOUT,
            checkpoint=checkpoint,
            verification={
                "outcome": "pending",
                "browser": "closed",
                "headed": False,
                "message": MESSAGE_TIMEOUT,
                "authorization_scope": scope,
                "source_kind": source_kind,
            },
            interruption=None,
        )
        return "halt"
    if signal in {"stop", VERIFY_FAILED}:
        message = MESSAGE_STOP if signal == "stop" else MESSAGE_FAILED
        _write_check(
            factory,
            batch_id,
            status="stopped",
            message=message,
            checkpoint=checkpoint,
            verification={"outcome": "failed", "browser": "closed", "headed": False, "message": message},
            interruption=None,
        )
        return "halt"
    adapter = _adapter(platform)
    try:
        payload = eval_js(driver, adapter.PAGE_TEXT_JS)
    except Exception:
        payload = None
    kind = _blocked_kind(payload if isinstance(payload, dict) else None)
    if kind == "captcha":
        _write_check(
            factory,
            batch_id,
            status="stopped",
            message=MESSAGE_STILL,
            checkpoint=checkpoint,
            verification={"outcome": "failed", "browser": "closed", "headed": False, "message": MESSAGE_STILL},
            interruption={"classification": "captcha", "retryable": False, "reason": MESSAGE_STILL[:300]},
        )
        return "halt"
    if kind == "access_restriction":
        _write_check(
            factory,
            batch_id,
            status="stopped",
            message=MESSAGE_DENIED,
            checkpoint=checkpoint,
            verification={
                "outcome": "not_permitted",
                "browser": "closed",
                "headed": False,
                "message": MESSAGE_DENIED,
            },
            interruption={"classification": "access_restriction", "retryable": False, "reason": MESSAGE_DENIED[:300]},
        )
        return "halt"
    _write_check(
        factory,
        batch_id,
        status="running",
        message=MESSAGE_PASSED,
        checkpoint=checkpoint,
        verification={
            "outcome": "passed",
            "browser": "open",
            "headed": True,
            "authorization_scope": scope,
            "source_kind": source_kind,
            "message": MESSAGE_PASSED,
        },
        interruption=None,
    )
    return "continue"


def _record_restriction(
    factory: sessionmaker[Session],
    batch_id: int,
    exc: MapAccessBlocked,
) -> None:
    checkpoint = exc.checkpoint if isinstance(exc.checkpoint, dict) else None
    _write_check(
        factory,
        batch_id,
        status="interrupted",
        message=str(exc)[:300],
        checkpoint=checkpoint,
        verification=None,
        interruption={"classification": exc.kind, "retryable": False, "reason": str(exc)[:300]},
    )


def _finish_discovered(
    factory: sessionmaker[Session],
    batch_id: int,
    *,
    found: dict[str, Any],
    category: str,
    platform: str,
    control: RunControl,
    hold_completion: bool = False,
) -> None:
    stored = _progress(factory, batch_id)
    stored.update(
        {
            "phase": "collect",
            "active_query": category,
            "platform": platform,
            "pages_loaded": found["pages_loaded"],
            "scrolls": found["scrolls"],
            "duplicates": found["duplicates"],
            "link_collection_complete": found["complete"],
            "stop_reason": found["stop_reason"],
            "discovery_checkpoint": {
                "next_page": found["next_page"],
                "scrolls": found["scrolls"],
                "followed_pager": found["followed_pager"],
                "stop_reason": found["stop_reason"],
            },
            "hits": len(stored.get("seen_ids") or []),
        }
    )
    waiting = _retryable_ids(factory, batch_id)
    if waiting:
        stored["retry_pending"] = True
    else:
        stored.pop("retry_pending", None)
    _save(factory, batch_id, stored)
    reason = found["stop_reason"]
    if control._stopped:
        _mark(factory, batch_id, "stopped", finish=True)
    elif control._paused:
        _mark(factory, batch_id, "paused", finish=False)
    elif found["interrupted"] or waiting or reason == "stall":
        _mark(factory, batch_id, "interrupted", finish=True)
    elif reason in {"limit", "confirmed_end"}:
        from zhol.agents.scout.selenium_collector import _source_cap_open

        if hold_completion and _source_cap_open(factory, batch_id):
            _mark(factory, batch_id, "running", finish=False)
        else:
            _mark(factory, batch_id, "completed", finish=True)
    else:
        _mark(factory, batch_id, "interrupted", finish=True)


def run_maps_batch(
    factory: sessionmaker[Session],
    *,
    platform: str,
    city: str,
    category: str,
    limit: int,
    out: Path,
    resume: int | None = None,
    control: RunControl | None = None,
    driver_factory: Callable[[], Any] | None = None,
    run_dir: Path | None = None,
    delay_range: tuple[float, float] | None = None,
    on_open: Callable[[int], None] | None = None,
    verification_wait_seconds: float | None = None,
    operator_sleep: Callable[[float], None] | None = None,
    hold_completion: bool = False,
    slice_limit: int | None = None,
) -> int:
    """Collect one query. A live host is used only when that source's scope is set."""
    if platform not in {"yandex", "google"}:
        raise BatchError("Неизвестный источник сбора.")
    origin = _require_origin(platform)
    if limit < 1:
        raise BatchError("Лимит должен быть от 1 до 500.")
    city_name = _city_name(city, platform)
    adapter = _adapter(platform)
    control = control or RunControl()
    batch_id = resume or _open(factory, platform=platform, city=city_name, category=category, limit=limit, out=out)
    if on_open is not None:
        on_open(batch_id)
    if _status(factory, batch_id) == "stopped":
        return batch_id
    if _verification_unavailable(_progress(factory, batch_id)):
        return batch_id
    if _status(factory, batch_id) == "completed" and not _retryable_ids(factory, batch_id):
        return batch_id
    if resume is not None:
        _note_browser_restart(factory, batch_id)
        stored_limit = None
        with factory() as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is not None and batch.item_limit is not None:
                stored_limit = int(batch.item_limit)
        if stored_limit is not None:
            limit = stored_limit
    if control._stopped:
        _mark(factory, batch_id, "stopped", finish=True)
        return batch_id
    if control._paused:
        _mark(factory, batch_id, "paused", finish=False)
        return batch_id
    state = _progress(factory, batch_id)
    seen = {str(item) for item in state.get("seen_ids") or []}
    rejected = {str(item) for item in state.get("rejected_ids") or []}
    retryable = _retryable_ids(factory, batch_id)
    finished = seen | rejected
    new_slots = limit - len(finished) - len(retryable)
    if slice_limit is not None:
        new_slots = min(new_slots, slice_limit)
    if new_slots < 1 and not retryable:
        _mark(factory, batch_id, "completed", finish=True)
        return batch_id
    if new_slots < 0:
        new_slots = 0
    artifact = run_dir or Path(out).parent / f"maps-run-{batch_id}"
    artifact.mkdir(parents=True, exist_ok=True)
    driver = None
    wait_seconds = _VERIFY_WAIT_SECONDS if verification_wait_seconds is None else verification_wait_seconds
    pause_sleep = operator_sleep or time.sleep

    def halted() -> bool:
        return control._stopped or control._paused

    def on_captcha(exc: MapAccessBlocked) -> str:
        return _respond_to_captcha(
            factory,
            batch_id,
            driver,
            platform,
            exc,
            control=control,
            sleep=pause_sleep,
            max_wait_seconds=wait_seconds,
        )

    try:
        driver = driver_factory() if driver_factory is not None else _start_local_chrome(platform)
        _mark(factory, batch_id, "running", finish=False)
        checkpoint = state.get("discovery_checkpoint")
        from zhol.agents.scout.localities import google_search_text

        lookup = google_search_text(category, city_name) if platform == "google" else category
        found = _discover(
            driver,
            adapter,
            origin=origin,
            platform=platform,
            query=lookup,
            city=city_name,
            limit=new_slots,
            known=finished,
            reserved=retryable,
            stopped=halted,
            checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
            on_captcha=on_captcha,
        )
        for item in found["items"]:
            if halted():
                break
            _wait(control, delay_range)
            if halted():
                break
            external_id = item["external_id"]
            if external_id in seen or external_id in rejected:
                continue
            try:
                open_url(driver, item["url"], NAV_TIMEOUT_MS)
                payload = eval_js(driver, adapter.PAGE_TEXT_JS)
                _raise_if_blocked(payload if isinstance(payload, dict) else None)
                wait_js(driver, adapter.READY_CARD_JS, NAV_TIMEOUT_MS)
                raw = eval_js(driver, adapter.READ_CARD_JS)
            except MapAccessBlocked as exc:
                if not isinstance(exc.checkpoint, dict):
                    exc.checkpoint = {
                        "next_page": found["next_page"],
                        "scrolls": found["scrolls"],
                        "followed_pager": found["followed_pager"],
                        "pages_loaded": found["pages_loaded"],
                    }
                if exc.kind != "captcha" or on_captcha(exc) != "continue":
                    if exc.kind != "captcha":
                        _record_restriction(factory, batch_id, exc)
                    return batch_id
                try:
                    open_url(driver, item["url"], NAV_TIMEOUT_MS)
                    payload = eval_js(driver, adapter.PAGE_TEXT_JS)
                    _raise_if_blocked(payload if isinstance(payload, dict) else None)
                    wait_js(driver, adapter.READY_CARD_JS, NAV_TIMEOUT_MS)
                    raw = eval_js(driver, adapter.READ_CARD_JS)
                except MapAccessBlocked as again:
                    again.checkpoint = exc.checkpoint
                    if again.kind == "captcha":
                        from zhol.agents.scout.selenium_collector import MESSAGE_STILL

                        _write_check(
                            factory,
                            batch_id,
                            status="stopped",
                            message=MESSAGE_STILL,
                            checkpoint=exc.checkpoint,
                            verification={
                                "outcome": "failed",
                                "browser": "closed",
                                "headed": False,
                                "message": MESSAGE_STILL,
                            },
                            interruption={
                                "classification": "captcha",
                                "retryable": False,
                                "reason": MESSAGE_STILL[:300],
                            },
                        )
                    else:
                        _record_restriction(factory, batch_id, again)
                    return batch_id
                except BrowserTimeout as timeout:
                    _commit_failure(factory, batch_id, external_id, f"Время ожидания карточки: {timeout}", kind="timeout")
                    continue
                except Exception as exc:
                    if is_browser_crash(exc):
                        raise
                    _commit_failure(factory, batch_id, external_id, str(exc), kind="extraction")
                    continue
            except BrowserTimeout as exc:
                _commit_failure(factory, batch_id, external_id, f"Время ожидания карточки: {exc}", kind="timeout")
                continue
            except Exception as exc:
                if is_browser_crash(exc):
                    raise
                _commit_failure(factory, batch_id, external_id, str(exc), kind="extraction")
                continue
            if not isinstance(raw, dict):
                _commit_failure(factory, batch_id, external_id, "На странице нет названия или идентификатора", kind="identity")
                continue
            _raise_if_blocked(raw)
            place = normalize_place(
                raw,
                platform=platform,
                external_id=external_id,
                url=item["url"],
                city=city_name,
                query=category,
            )
            if place is None:
                _commit_failure(factory, batch_id, external_id, "Нет идентификатора филиала или названия", kind="identity")
                continue
            place = _apply_city(place, city_name)
            shot = _capture(driver, artifact, external_id)
            if shot:
                place = replace(place, screenshot_path=shot)
            _commit_place(factory, batch_id, place)
            seen.add(external_id)
        _finish_discovered(
            factory,
            batch_id,
            found=found,
            category=category,
            platform=platform,
            control=control,
            hold_completion=hold_completion,
        )
        return batch_id
    except _MapCheckSettled as settled:
        if settled.still:
            from zhol.agents.scout.selenium_collector import MESSAGE_STILL

            _write_check(
                factory,
                batch_id,
                status="stopped",
                message=MESSAGE_STILL,
                checkpoint=settled.checkpoint,
                verification={
                    "outcome": "failed",
                    "browser": "closed",
                    "headed": False,
                    "message": MESSAGE_STILL,
                },
                interruption={"classification": "captcha", "retryable": False, "reason": MESSAGE_STILL[:300]},
            )
        return batch_id
    except MapAccessBlocked as exc:
        if exc.kind == "captcha":
            on_captcha(exc)
            return batch_id
        _record_restriction(factory, batch_id, exc)
        return batch_id
    except Exception as exc:
        state = _progress(factory, batch_id)
        state["error"] = str(exc)[:300]
        if is_browser_crash(exc):
            state["interruption"] = {"classification": "browser_session", "retryable": True, "reason": str(exc)[:300]}
        _save(factory, batch_id, state)
        _mark(factory, batch_id, "interrupted", finish=True)
        raise
    finally:
        _close_driver(driver)


def _start_local_chrome(platform: str) -> Any:
    """Start the existing Chrome launcher after the origin gate. A grant-less run stays on loopback."""
    _require_origin(platform)
    root = Path(__file__).resolve().parents[3]
    import sys

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import os

    import parser_service

    headless = os.environ.get("ZHOL_CHROME_HEADLESS", "").strip() == "1"
    return parser_service._start_chrome(headless)
