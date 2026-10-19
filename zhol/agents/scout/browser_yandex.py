"""Policy gate for the Yandex Maps browser collector.

This module does not open a page. ZHOL_2GIS_BROWSER_GRANT is not read.
An empty value, any other text, and the Google grant leave the live site closed.
The two scopes are not interchangeable:

- written-browser-retention-local: local non-commercial collection and PostgreSQL storage
- written-browser-retention-commercial: collection, storage, and commercial use

A loopback fixture does not use this grant and is not reported as live collection.
A robot check is not solved and is not bypassed.
"""

from __future__ import annotations

from zhol.agents.scout.browser_2gis import browser_engine_available, local_base_url, local_html_root
from zhol.agents.scout.localities import names_for
from zhol.agents.scout.browser_maps import (
    GRANT_COMMERCIAL,
    GRANT_LOCAL,
    BrowserCollectionRefused,
    allow_url,
    collection_state,
    grant_scope as read_grant,
    map_plans,
)

SOURCE_MODE = "browser_yandex"
PLATFORM = "yandex"
GRANT_ENV = "ZHOL_YANDEX_BROWSER_GRANT"
LIVE_ORIGIN = "https://yandex.kz"
BLOCKER = (
    "Живой сбор Яндекс Карт выключен. "
    "Условия использования Яндекс Карт, пункт 4.6 "
    "(yandex.ru/legal/maps_termsofuse, редакция от 23.09.2026): "
    "информация сервиса предназначена только для личного некоммерческого использования. "
    "Копирование данных и коммерческое хранение без письменного разрешения правообладателя запрещены. "
    "Письменное разрешение другой площадки этот источник не открывает. "
    "Чтобы включить источник, задайте ZHOL_YANDEX_BROWSER_GRANT ровно одним значением: "
    f"{GRANT_LOCAL} (локальный некоммерческий сбор и хранение в PostgreSQL) "
    f"или {GRANT_COMMERCIAL} (сбор, хранение и коммерческое использование). "
    "Пустое значение и любой другой текст источник не открывают. Одно разрешение не заменяет другое. "
    "Проверка «не робот» не обходится. Сайт Яндекс Карт не открывается."
)
ENGINE_BLOCKER = (
    "Движок браузера на этом компьютере не готов: Chrome для сборщика не найден. "
    "Это не разрешение и не запрет источника. Сайт Яндекс Карт без письменного разрешения не открывается."
)
LOOPBACK_BLOCKER = (
    "Задан локальный адрес сбора. Живой сайт Яндекс Карт не открывается. "
    "Локальный HTML не считается живым сбором."
)
LOCAL_NOTE = (
    "Письменное разрешение Яндекс Карт подтверждено только для локального некоммерческого сбора "
    "и хранения в PostgreSQL. Коммерческое использование не подтверждено. "
    "Сборщик откроет публичные карточки в Chrome. "
    "Проверка «не робот» не решается и не обходится."
)
COMMERCIAL_NOTE = (
    "Письменное коммерческое разрешение Яндекс Карт подтверждено: сбор, хранение и коммерческое использование. "
    "Сборщик откроет публичные карточки в Chrome. "
    "Проверка «не робот» не решается и не обходится."
)
LIMIT_NOTE = (
    "Страница с ответом HTTP 429 или со словом limited останавливает сбор. "
    "Такая страница не записывается как успешная карточка."
)


def grant_scope() -> str | None:
    return read_grant(GRANT_ENV)


def live_authorized() -> bool:
    return grant_scope() is not None


def accept_url(url: str) -> None:
    allow_url(url, BLOCKER, live_origin=LIVE_ORIGIN if live_authorized() else None)


def collection_ready() -> bool:
    """Live Yandex may be queued. A loopback address does not count as this site."""
    return live_authorized() and browser_engine_available() and local_base_url() is None and local_html_root() is None


def browser_mode() -> dict:
    scope = grant_scope()
    engine = browser_engine_available()
    loopback = local_base_url() is not None or local_html_root() is not None
    if scope is None:
        available = False
        note = BLOCKER
        source_kind = "live_refused"
    elif loopback:
        available = False
        note = LOOPBACK_BLOCKER
        source_kind = "localhost"
    elif not engine:
        available = False
        note = ENGINE_BLOCKER
        source_kind = "live_yandex"
    else:
        available = True
        note = LOCAL_NOTE if scope == "local" else COMMERCIAL_NOTE
        source_kind = "live_yandex"
    return {
        "id": SOURCE_MODE,
        "kind": "browser",
        "label": "Публичные карточки Яндекс Карт",
        "available": available,
        "cities": names_for("yandex"),
        "note": note,
        "source_kind": source_kind,
        "engine": engine,
        "origin": LIVE_ORIGIN if available else None,
        "authorization_scope": scope,
        "live_available": available,
        "collection_state": collection_state(available=available, scope=scope),
        "limit_note": LIMIT_NOTE,
    }


def refuse_browser_collection() -> None:
    if grant_scope() is None:
        raise BrowserCollectionRefused(BLOCKER)
    if local_base_url() is not None or local_html_root() is not None:
        raise BrowserCollectionRefused(LOOPBACK_BLOCKER)
    if not browser_engine_available():
        raise BrowserCollectionRefused(ENGINE_BLOCKER)
    raise BrowserCollectionRefused(BLOCKER)


def build_browser_plans(
    *,
    city: str,
    categories: list[str],
    queries: list[str],
    platforms: list[str],
    limit: int,
) -> list[dict]:
    if not collection_ready():
        refuse_browser_collection()
    return map_plans(
        source_mode=SOURCE_MODE,
        platform=PLATFORM,
        city=city,
        categories=categories,
        queries=queries,
        platforms=platforms,
        limit=limit,
    )
