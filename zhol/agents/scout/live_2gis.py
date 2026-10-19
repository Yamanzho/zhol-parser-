"""Official 2GIS Catalog API collector.

This module does not open 2gis.kz and does not solve a robot check.
A call leaves the machine only through catalog.api.2gis.com when a key is configured
and the caller did not pass a test transport.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.normalize import normalize_phone, normalize_platform_id, phone_parts
from zhol.agents.scout.persist import insert_snapshot, remember_platform_id, remember_search_query
from zhol.browser.discovery.normalize import verified_email, verified_instagram, verified_website, verified_whatsapp
from zhol.browser.pace import collection_rate_seconds, collection_turn
from zhol.browser.discovery.types import RunControl
from zhol.browser.live import wait_seconds
from zhol.browser.twogis.capture import FIRM_URL
from zhol.db.models import CardSnapshot, CollectionBatch, CollectionBatchItem, Company
from zhol.export.mailing import _key, load_categories
from zhol.timeutil import now

ENV_KEY = "DGIS_CATALOG_KEY"
HOST = "catalog.api.2gis.com"
REGION_PATH = "/2.0/region/search"
ITEMS_PATH = "/3.0/items"
ALLOWED_PATHS = frozenset({REGION_PATH, ITEMS_PATH})
LIVE_CITIES = ("Алматы", "Астана")
SOURCE_MODE = "live_2gis"
COMPANY_SOURCE = "live_2gis"
CATALOG_MARKER = "live_2gis"
PAGE_SIZE = 10
MAX_HTTP_ATTEMPTS = 3
KEY_MISSING = (
    "Сбор из каталога 2ГИС не запущен: нет ключа DGIS_CATALOG_KEY. "
    "Ключ выдаёт platform.2gis.ru. Демо-ключ ограничен, коммерческий сбор требует договора. "
    "Сайт 2ГИС не открывается."
)
KEY_DENIED = "Каталог 2ГИС отклонил ключ. Проверьте DGIS_CATALOG_KEY и права ключа. Обход сайта не используется."
TEMPORARY = "Каталог 2ГИС временно не ответил. Повтор ограничен. Уже записанные компании сохранены."

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


class CatalogError(BatchError):
    pass


class CatalogDenied(CatalogError):
    pass


class CatalogTemporary(CatalogError):
    pass


class CatalogTransport(Protocol):
    def get(self, path: str, params: dict[str, str]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LiveFirm:
    firm_id: str
    name: str
    address: str | None
    city: str
    category: str | None
    phones: tuple[str, ...]
    phone_display: tuple[str, ...]
    whatsapp: str | None
    instagram: str | None
    website: str | None
    email: str | None
    url: str | None
    rating: float | None
    reviews_count: int | None
    branches: int | None
    contacts_seen: bool
    reviews_seen: bool


def key_configured() -> bool:
    return bool(os.environ.get(ENV_KEY, "").strip())


def live_mode() -> dict[str, Any]:
    ready = key_configured()
    return {
        "id": SOURCE_MODE,
        "label": "Официальный Catalog API, 2ГИС",
        "available": ready,
        "cities": list(LIVE_CITIES),
        "blocker": None if ready else KEY_MISSING,
        "note": (
            "Официальный Catalog API. Сайт 2ГИС не открывается."
            if ready
            else KEY_MISSING
        ),
    }


def build_live_plans(
    *,
    city: str,
    categories: list[str],
    queries: list[str],
    platforms: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    if not key_configured():
        raise CatalogDenied(KEY_MISSING)
    if limit < 1 or limit > 500:
        raise CatalogError("Лимит должен быть от 1 до 500.")
    _platforms(platforms)
    city_name = _city(city)
    table = load_categories()
    labels: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        key = _key(label)
        if key in seen:
            return
        seen.add(key)
        labels.append(label)

    for raw in categories:
        cleaned = _strip_city(" ".join(str(raw).split()), city_name)
        if not cleaned:
            raise CatalogError("Укажите категорию.")
        if len(cleaned) > 80:
            raise CatalogError("Слишком длинное название категории.")
        key = _key(cleaned)
        if key in table.ambiguous:
            raise CatalogError(f"Категория «{cleaned}» слишком общая. Выберите точное название из списка.")
        if key not in table.exact:
            raise CatalogError(f"Категория «{cleaned}» не поддерживается. Напишите её в дополнительных запросах.")
        _sector, category, subcategory = table.exact[key]
        add(subcategory or category)
    for raw in queries:
        text = " ".join(str(raw).split())
        if not text:
            continue
        if len(text) > 120:
            raise CatalogError("Слишком длинный дополнительный запрос.")
        cleaned = _strip_city(text, city_name)
        if not cleaned:
            raise CatalogError("Дополнительный запрос не должен состоять только из названия города.")
        add(cleaned)
    if not labels:
        raise CatalogError("Выберите хотя бы одну категорию или укажите дополнительный запрос.")
    return [
        {
            "source_mode": SOURCE_MODE,
            "city": city_name,
            "category": label,
            "platforms": ["2gis"],
            "limit": limit,
            "catalog": CATALOG_MARKER,
        }
        for label in labels
    ]


def firm_from_item(item: dict[str, Any], *, city: str, category: str, slug: str | None) -> LiveFirm | None:
    if item.get("type") not in (None, "branch"):
        return None
    firm_id = normalize_platform_id("2gis", str(item.get("id") or ""))
    if not firm_id or not firm_id.isdigit() or len(firm_id) < 8:
        return None
    name = " ".join(str(item.get("name") or "").split())
    if not name:
        return None
    address = _address(item)
    contacts_seen = "contact_groups" in item
    phones, phone_display, whatsapp, instagram, website, email = (
        _contacts(item) if contacts_seen else ((), (), None, None, None, None)
    )
    reviews = item.get("reviews") if isinstance(item.get("reviews"), dict) else None
    rating, reviews_count = _reviews(reviews)
    branches = _branches(item.get("org"))
    rubric = _rubric(item.get("rubrics"))
    return LiveFirm(
        firm_id=firm_id,
        name=name,
        address=address,
        city=city,
        category=rubric,
        phones=phones,
        phone_display=phone_display,
        whatsapp=whatsapp,
        instagram=instagram,
        website=website,
        email=email,
        url=_firm_url(slug, firm_id),
        rating=rating,
        reviews_count=reviews_count,
        branches=branches,
        contacts_seen=contacts_seen,
        reviews_seen=reviews is not None,
    )


def run_live_batch(
    factory: sessionmaker[Session],
    *,
    city: str,
    category: str,
    limit: int,
    out: Path,
    resume: int | None = None,
    control: RunControl | None = None,
    on_open: Callable[[int], None] | None = None,
    transport: CatalogTransport | None = None,
    sleep: Sleeper | None = None,
    clock: Clock | None = None,
) -> int:
    """Collect one query. A missing transport uses the Catalog API and requires a key."""
    if limit < 1:
        raise CatalogError("Лимит должен быть от 1 до 500.")
    client = transport or HttpCatalogClient.from_env()
    control = control or RunControl()
    pause = sleep or time.sleep
    tick = clock or (lambda: datetime.now(timezone.utc))
    batch_id = resume or _open(factory, city=city, category=category, limit=limit, out=out)
    if on_open is not None:
        on_open(batch_id)
    state = _progress(factory, batch_id)
    if _status(factory, batch_id) in {"completed", "stopped"}:
        return batch_id
    _mark(factory, batch_id, "running", finish=False)
    try:
        region = state.get("region") if isinstance(state.get("region"), dict) else None
        if region is None:
            region = _region(client, city, pause, tick)
            state["region"] = {"id": region["id"], "slug": region.get("slug")}
            _save(factory, batch_id, state)
        slug = region.get("slug") if isinstance(region.get("slug"), str) else None
        region_id = str(region["id"])
        accepted = len(state.get("seen_ids") or [])
        page = int(state.get("next_page") or 1)
        last_at: datetime | None = tick()
        while accepted < limit:
            if control._stopped:
                _mark(factory, batch_id, "stopped", finish=True)
                return batch_id
            if control._paused:
                _mark(factory, batch_id, "paused", finish=False)
                return batch_id
            if page > limit + 5:
                state["error"] = "Сбор остановлен: каталог не дал новых компаний в отведенном числе страниц."
                _save(factory, batch_id, state)
                break
            page_size = min(PAGE_SIZE, limit - accepted)
            delay = wait_seconds(last_at, rate_limit_seconds=collection_rate_seconds(), at=tick())
            if delay > 0:
                pause(delay)
            if control._stopped:
                _mark(factory, batch_id, "stopped", finish=True)
                return batch_id
            if control._paused:
                _mark(factory, batch_id, "paused", finish=False)
                return batch_id
            with collection_turn():
                payload = client.get(
                    ITEMS_PATH,
                    {
                        "q": category,
                        "region_id": region_id,
                        "type": "branch",
                        "page": str(page),
                        "page_size": str(page_size),
                        "locale": "ru_KZ",
                        "fields": "items.contact_groups,items.reviews,items.rubrics,items.org",
                    },
                )
            if not isinstance(payload, dict):
                raise CatalogTemporary(TEMPORARY)
            last_at = tick()
            items, total = _items(payload)
            if not items:
                break
            finished_page = True
            for offset, raw in enumerate(items):
                if accepted >= limit or control._stopped or control._paused:
                    finished_page = False
                    break
                firm = firm_from_item(raw, city=city, category=category, slug=slug)
                if firm is None:
                    state["unreadable"] = int(state.get("unreadable") or 0) + 1
                    _failed_item(factory, batch_id, f"unreadable-{page}-{offset}", "В ответе каталога нет устойчивого номера филиала или названия.")
                    continue
                seen = [str(item) for item in state.get("seen_ids") or []]
                if firm.firm_id in seen:
                    state["duplicates"] = int(state.get("duplicates") or 0) + 1
                    continue
                _store_firm(factory, batch_id, firm, category)
                seen.append(firm.firm_id)
                state["seen_ids"] = seen
                state["hits"] = len(seen)
                accepted += 1
                _save(factory, batch_id, state)
            if finished_page:
                page += 1
                state["next_page"] = page
            state["active_query"] = category
            _save(factory, batch_id, state)
            if len(items) < page_size:
                break
            if isinstance(total, int) and accepted >= total:
                break
        if control._stopped:
            _mark(factory, batch_id, "stopped", finish=True)
        elif control._paused:
            _mark(factory, batch_id, "paused", finish=False)
        else:
            _mark(factory, batch_id, "completed", finish=True)
        return batch_id
    except CatalogError as exc:
        state = _progress(factory, batch_id)
        state["error"] = str(exc)
        _save(factory, batch_id, state)
        _mark(factory, batch_id, "interrupted", finish=True)
        raise


class HttpCatalogClient:
    """Talks only to catalog.api.2gis.com. Tests pass a transport instead of constructing this."""

    def __init__(self, key: str, opener: Callable[..., Any] | None = None, sleep: Sleeper | None = None):
        self._key = key.strip()
        if not self._key:
            raise CatalogDenied(KEY_MISSING)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep

    @classmethod
    def from_env(cls) -> HttpCatalogClient:
        return cls(os.environ.get(ENV_KEY, ""))

    def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if path not in ALLOWED_PATHS:
            raise CatalogDenied("Запрошен неизвестный адрес каталога 2ГИС.")
        last: CatalogTemporary | None = None
        for attempt in range(MAX_HTTP_ATTEMPTS):
            try:
                return self._once(path, params)
            except CatalogTemporary as exc:
                last = exc
                if attempt + 1 >= MAX_HTTP_ATTEMPTS:
                    raise
                self._sleep(collection_rate_seconds())
        raise last or CatalogTemporary(TEMPORARY)

    def _once(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = _request_url(path, params, self._key)
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Zhol catalog client"})
        try:
            response = self._opener(request, timeout=20)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc.code, self._read_error(exc)) from None
        except urllib.error.URLError as exc:
            raise CatalogTemporary(TEMPORARY) from exc
        status = int(getattr(response, "status", 200))
        final = str(getattr(response, "geturl", lambda: url)())
        host = urllib.parse.urlparse(final).hostname
        if host != HOST:
            raise CatalogDenied("Ответ пришёл не от catalog.api.2gis.com. Обход ограничений не выполняется.")
        raw = response.read()
        return _decode(status, raw, self._key)


def _request_url(path: str, params: dict[str, str], key: str) -> str:
    if path not in ALLOWED_PATHS:
        raise CatalogDenied("Запрошен неизвестный адрес каталога 2ГИС.")
    query = {key_name: value for key_name, value in params.items() if key_name != "key"}
    query["key"] = key
    return f"https://{HOST}{path}?{urllib.parse.urlencode(query)}"


def _decode(status: int, raw: bytes, key: str) -> dict[str, Any]:
    try:
        import json

        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CatalogTemporary(TEMPORARY) from exc
    if not isinstance(body, dict):
        raise CatalogTemporary(TEMPORARY)
    code = status
    meta = body.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("code"), int):
        code = int(meta["code"])
    if code in {401, 403}:
        raise CatalogDenied(KEY_DENIED)
    if code == 404:
        return {"meta": {"code": 404}, "result": {"total": 0, "items": []}}
    if code == 429 or code >= 500:
        raise CatalogTemporary(TEMPORARY)
    if code >= 400:
        raise CatalogDenied(_safe(str(_meta_message(body) or "Каталог 2ГИС отклонил запрос."), key))
    return body


def _http_error(status: int, detail: str) -> CatalogError:
    if status in {401, 403}:
        return CatalogDenied(KEY_DENIED)
    if status == 429 or status >= 500:
        return CatalogTemporary(TEMPORARY)
    return CatalogDenied(detail or "Каталог 2ГИС отклонил запрос.")


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:180]
    except Exception:
        return ""


def _meta_message(body: dict[str, Any]) -> str | None:
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return None
    error = meta.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return str(message) if message else None
    return None


def _safe(text: str, key: str) -> str:
    cleaned = text.replace(key, "[redacted]") if key else text
    cleaned = re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[redacted]", cleaned)
    return cleaned[:300]


def _region(client: CatalogTransport, city: str, sleep: Sleeper, clock: Clock) -> dict[str, str]:
    payload = _paced(client, REGION_PATH, {"q": city}, sleep, clock, None)
    items = ((payload.get("result") or {}).get("items") or []) if isinstance(payload, dict) else []
    wanted = _fold(city)
    found = [
        item
        for item in items
        if isinstance(item, dict) and item.get("type") in (None, "region") and _fold(str(item.get("name") or "")) == wanted
    ]
    if len(found) != 1 or not str(found[0].get("id") or "").isdigit():
        raise CatalogDenied(
            f"Город «{city}» не найден в Regions API 2ГИС. Сбор остановлен, компании не подставлены из файла."
        )
    slug = found[0].get("uri_code") or found[0].get("code")
    return {"id": str(found[0]["id"]), "slug": str(slug) if isinstance(slug, str) else ""}


def _paced(
    client: CatalogTransport,
    path: str,
    params: dict[str, str],
    sleep: Sleeper,
    clock: Clock,
    last_at: datetime | None,
) -> dict[str, Any]:
    delay = wait_seconds(last_at, rate_limit_seconds=collection_rate_seconds(), at=clock())
    if delay > 0:
        sleep(delay)
    with collection_turn():
        body = client.get(path, params)
    if not isinstance(body, dict):
        raise CatalogTemporary(TEMPORARY)
    return body


def _items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    raw = result.get("items") or []
    total = result.get("total")
    items = [item for item in raw if isinstance(item, dict)]
    return items, total if isinstance(total, int) else None


def _contacts(item: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], str | None, str | None, str | None, str | None]:
    phones: list[str] = []
    phone_display: list[str] = []
    whatsapp = None
    instagram = None
    website = None
    email = None
    for group in item.get("contact_groups") or []:
        if not isinstance(group, dict):
            continue
        for contact in group.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            kind = str(contact.get("type") or "").lower()
            text = str(contact.get("text") or contact.get("value") or "")
            url = str(contact.get("url") or "")
            value = str(contact.get("value") or "")
            if kind == "phone":
                number, _shown_value = phone_parts(value or text)
                _number_text, shown = phone_parts(text or value)
                if number and number not in phones:
                    phones.append(number)
                    if shown:
                        phone_display.append(shown)
            elif kind == "whatsapp" and whatsapp is None:
                whatsapp = verified_whatsapp(url, text or value) or normalize_phone(value or text)
            elif kind == "instagram" and instagram is None:
                instagram = verified_instagram(url or value, text)
            elif kind == "website" and website is None:
                website = verified_website(url or value, text)
            elif kind == "email" and email is None:
                email = verified_email(value or text)
            elif whatsapp is None and ("wa.me" in url.lower() or "whatsapp" in kind):
                whatsapp = verified_whatsapp(url, text or value)
            elif instagram is None and "instagram.com" in f"{url} {value}".lower():
                instagram = verified_instagram(url or value, text)
    return tuple(phones), tuple(phone_display), whatsapp, instagram, website, email


def _reviews(reviews: dict[str, Any] | None) -> tuple[float | None, int | None]:
    if reviews is None:
        return None, None
    rating = reviews.get("general_rating", reviews.get("rating"))
    count = reviews.get("general_review_count", reviews.get("review_count"))
    parsed_rating = float(rating) if isinstance(rating, (int, float)) and not isinstance(rating, bool) else None
    parsed_count = int(count) if isinstance(count, int) and not isinstance(count, bool) else None
    return parsed_rating, parsed_count


def _branches(org: object) -> int | None:
    if not isinstance(org, dict) or not isinstance(org.get("branch_count"), int) or isinstance(org.get("branch_count"), bool):
        return None
    count = int(org["branch_count"])
    return count if count >= 0 else None


def _rubric(raw: object) -> str | None:
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        return None
    name = " ".join(str(raw[0].get("name") or "").split())
    return name or None


def _address(item: dict[str, Any]) -> str | None:
    for key in ("address_name", "full_address_name"):
        text = " ".join(str(item.get(key) or "").split())
        if text:
            return text
    return None


def _firm_url(slug: str | None, firm_id: str) -> str | None:
    if not slug or not re.fullmatch(r"[a-z0-9_-]+", slug):
        return None
    url = f"https://2gis.kz/{slug}/firm/{firm_id}"
    return url if FIRM_URL.match(url) else None


def _platforms(values: list[str]) -> None:
    if values != ["2gis"]:
        raise CatalogError("Для каталога 2ГИС доступен только источник 2ГИС. Яндекс и Google не подключаются.")


def _city(city: str) -> str:
    wanted = _fold(city)
    for name in LIVE_CITIES:
        if _fold(name) == wanted:
            return name
    raise CatalogError(f"Город «{city.strip() or 'без названия'}» не входит в список каталога 2ГИС.")


def _strip_city(text: str, city: str) -> str:
    words = text.split()
    city_words = city.split()
    size = len(city_words)
    city_keys = [_fold(part) for part in city_words]
    changed = True
    while changed and len(words) >= size:
        changed = False
        if [_fold(part) for part in words[:size]] == city_keys:
            words = words[size:]
            changed = True
        elif [_fold(part) for part in words[-size:]] == city_keys:
            words = words[:-size]
            changed = True
    return " ".join(words)


def _fold(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def _open(factory: sessionmaker[Session], *, city: str, category: str, limit: int, out: Path) -> int:
    with factory() as session, session.begin():
        batch = CollectionBatch(
            city=city,
            category=category,
            platforms=["2gis"],
            item_limit=limit,
            source_mode=SOURCE_MODE,
            status="running",
            out_path=str(out),
            catalog_path=CATALOG_MARKER,
            progress={"phase": "collect", "active_query": category, "next_page": 1, "seen_ids": [], "hits": 0, "duplicates": 0},
        )
        session.add(batch)
        session.flush()
        return batch.id


def _store_firm(factory: sessionmaker[Session], batch_id: int, firm: LiveFirm, query: str) -> None:
    try:
        with factory() as session, session.begin():
            _store_in_session(session, batch_id, firm, query)
    except IntegrityError:
        with factory() as session, session.begin():
            company = session.scalar(select(Company).where(Company.id_2gis == firm.firm_id))
            if company is None:
                raise
            _link_existing(session, batch_id, company, firm, query)


def _store_in_session(session: Session, batch_id: int, firm: LiveFirm, query: str) -> None:
    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        return
    existing_item = session.scalar(
        select(CollectionBatchItem).where(
            CollectionBatchItem.batch_id == batch_id,
            CollectionBatchItem.id_2gis == firm.firm_id,
        )
    )
    if existing_item is not None:
        return
    company = session.scalar(select(Company).where(Company.id_2gis == firm.firm_id))
    if company is None:
        company = Company(
            id_2gis=firm.firm_id,
            name=firm.name,
            city=firm.city or batch.city,
            niche=query,
            whatsapp=firm.whatsapp,
            instagram=firm.instagram,
            source=COMPANY_SOURCE,
        )
        session.add(company)
        session.flush()
        _snapshot(session, batch, company, firm)
        reason = None
    else:
        reason = "already_present"
    session.add(
        CollectionBatchItem(
            batch_id=batch.id,
            id_2gis=firm.firm_id,
            company_id=company.id,
            status="completed",
            detail={
                "name": firm.name,
                "city": firm.city,
                "category": query,
                "address": firm.address,
                "reason": reason,
            },
        )
    )


def _link_existing(session: Session, batch_id: int, company: Company, firm: LiveFirm, query: str) -> None:
    existing_item = session.scalar(
        select(CollectionBatchItem).where(
            CollectionBatchItem.batch_id == batch_id,
            CollectionBatchItem.id_2gis == firm.firm_id,
        )
    )
    if existing_item is not None:
        return
    session.add(
        CollectionBatchItem(
            batch_id=batch_id,
            id_2gis=firm.firm_id,
            company_id=company.id,
            status="completed",
            detail={"name": firm.name, "city": firm.city, "category": query, "reason": "already_present"},
        )
    )


def _snapshot(session: Session, batch: CollectionBatch, company: Company, firm: LiveFirm) -> CardSnapshot:
    seen_at = now().isoformat()
    fields = {key: None for key in field_keys("2gis")}
    fields.update(
        {
            "name": firm.name,
            "address": firm.address,
            "city": firm.city,
            "category": firm.category,
            "phones": list(firm.phones) if firm.phones else None,
            "whatsapp": firm.whatsapp,
            "instagram": firm.instagram,
            "website": firm.website,
            "email": firm.email,
            "link": firm.url,
            "rating": firm.rating,
            "reviews_count": firm.reviews_count,
            "branches": firm.branches,
            "has_phone": bool(firm.phones) if firm.contacts_seen else None,
            "has_whatsapp": firm.whatsapp is not None if firm.contacts_seen else None,
            "has_instagram": firm.instagram is not None if firm.contacts_seen else None,
        }
    )
    sources = {
        key: {
            "url": f"https://{HOST}{ITEMS_PATH}",
            "seen_at": seen_at,
            "read_by": "code",
            "source": "catalog.api.2gis.com",
        }
        for key, value in fields.items()
        if value is not None
    }
    if firm.category:
        sources["category"]["checked"] = True
    else:
        sources["category"] = {
            "url": f"https://{HOST}{ITEMS_PATH}",
            "seen_at": seen_at,
            "read_by": "code",
            "source": "catalog.api.2gis.com",
            "reason": "not_shown",
            "checked": True,
        }
    if firm.phone_display and "phones" in sources:
        sources["phones"]["display"] = list(firm.phone_display)
    if firm.contacts_seen and not firm.phones:
        sources["phones"] = {
            "url": f"https://{HOST}{ITEMS_PATH}",
            "seen_at": seen_at,
            "read_by": "code",
            "source": "catalog.api.2gis.com",
            "reason": "empty",
        }
        if "has_phone" in sources:
            sources["has_phone"]["reason"] = "empty"
    remember_platform_id(session, company.id, "2gis", firm.firm_id, firm.url)
    snapshot = insert_snapshot(
        session,
        company_id=company.id,
        platform="2gis",
        purpose="prospect",
        match_status="found",
        url=firm.url,
        fields=fields,
        field_sources=sources,
        screenshot_path=None,
        collection_key=f"live_2gis:{batch.id}:{firm.firm_id}",
        page_file=None,
        kind="catalog_api",
        raw_path=None,
    )
    remember_search_query(snapshot, batch.category)
    return snapshot


def _failed_item(factory: sessionmaker[Session], batch_id: int, item_id: str, reason: str) -> None:
    with factory() as session, session.begin():
        existing = session.scalar(
            select(CollectionBatchItem).where(
                CollectionBatchItem.batch_id == batch_id,
                CollectionBatchItem.id_2gis == item_id,
            )
        )
        if existing is not None:
            return
        session.add(
            CollectionBatchItem(
                batch_id=batch_id,
                id_2gis=item_id,
                status="failed",
                detail={"reason": reason},
            )
        )


def _progress(factory: sessionmaker[Session], batch_id: int) -> dict[str, Any]:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        return dict(batch.progress or {}) if batch is not None else {}


def _save(factory: sessionmaker[Session], batch_id: int, progress: dict[str, Any]) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            return
        previous = batch.progress or {}
        kept = {key: value for key, value in previous.items() if str(key).startswith("admin_")}
        batch.progress = {**progress, **kept}


def _status(factory: sessionmaker[Session], batch_id: int) -> str:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        return "" if batch is None else batch.status


def _mark(factory: sessionmaker[Session], batch_id: int, status: str, *, finish: bool) -> None:
    with factory() as session, session.begin():
        batch = session.get(CollectionBatch, batch_id)
        if batch is None:
            return
        batch.status = status
        batch.finished_at = now() if finish else None
