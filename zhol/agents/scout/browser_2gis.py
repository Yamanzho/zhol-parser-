"""Policy gate for the existing 2GIS browser collector.

This module does not import a browser driver and does not open a page.
Engine readiness and source permission are separate.

A verbal permission is not a grant. Opening 2gis.kz still requires
ZHOL_2GIS_BROWSER_GRANT. Two written scopes are separate and are not
interchangeable:

- written-browser-retention-local: local non-commercial collection and storage
- written-browser-retention-commercial: collection, storage, and commercial use

A loopback address or a directory of local HTML does not need that grant
and is never treated as permission to open the live site.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.live_2gis import _strip_city
from zhol.agents.scout.localities import names_for, twogis_slug
from zhol.export.mailing import _key, load_categories

SOURCE_MODE = "browser_2gis"
GRANT_ENV = "ZHOL_2GIS_BROWSER_GRANT"
GRANT_LOCAL = "written-browser-retention-local"
GRANT_COMMERCIAL = "written-browser-retention-commercial"
GRANT_VALUE = GRANT_COMMERCIAL
GRANT_SCOPES = {GRANT_LOCAL: "local", GRANT_COMMERCIAL: "commercial"}
ENGINE_ENV = "ZHOL_BROWSER_ENGINE"
LOCAL_BASE_ENV = "ZHOL_BROWSER_BASE_URL"
LOCAL_HTML_ENV = "ZHOL_BROWSER_HTML_ROOT"
LIVE_HOST = "https://2gis.kz"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})
BROWSER_CITIES = tuple(names_for("2gis"))

BLOCKER = (
    "Сбор через браузер выключен: соглашение 2ГИС (law.2gis.ru/rules) запрещает скрипты и боты "
    "для сбора данных с сайта. Устное разрешение не подтверждено. "
    "Нужно отдельное письменное разрешение: локальный некоммерческий сбор и хранение, "
    "либо коммерческий сбор, хранение и коммерческое использование. Одно не заменяет другое. "
    "Проверка «не робот» не обходится. "
    "Живой запуск Chrome закрыт, пока нет соответствующего письменного разрешения."
)
ENGINE_BLOCKER = (
    "Движок браузера на этом компьютере не готов: Chrome для сборщика не найден. "
    "Это не разрешение и не запрет источника. Сайт 2ГИС без письменного разрешения не открывается."
)
COMMERCIAL_NOTE = (
    "Письменное коммерческое разрешение подтверждено: автоматический сбор, хранение и коммерческое использование. "
    "Существующий сборщик откроет публичные карточки 2ГИС в Chrome. "
    "Ключ Catalog API не используется. Проверка «не робот» не решается и не обходится: "
    "сбор ждет, пока человек пройдет её в видимом Chrome."
)
LOCAL_NOTE_READY = (
    "Письменное разрешение подтверждено только для локального некоммерческого сбора и хранения в PostgreSQL. "
    "Коммерческое разрешение не подтверждено. "
    "Существующий сборщик откроет публичные карточки 2ГИС в Chrome. "
    "Ключ Catalog API не используется. Проверка «не робот» не решается и не обходится: "
    "сбор ждет, пока человек пройдет её в видимом Chrome."
)
READY_NOTE = COMMERCIAL_NOTE
LOCAL_NOTE = (
    "Локальный источник включён. Существующий сборщик откроет только localhost или локальный HTML. "
    "Сайт 2ГИС не открывается. Ключ Catalog API не используется. "
    "Проверка «не робот» не решается и не обходится: сбор ждет человека в видимом Chrome."
)


class BrowserCollectionRefused(BatchError):
    pass


def grant_scope() -> str | None:
    """Return local or commercial. Any other text, including a verbal note, is no grant."""
    return GRANT_SCOPES.get(os.environ.get(GRANT_ENV, "").strip())


def live_2gis_authorized() -> bool:
    return grant_scope() is not None


def browser_engine_available() -> bool:
    """The collector can start Chrome. This does not authorize a website."""
    flag = os.environ.get(ENGINE_ENV, "").strip().lower()
    if flag in {"0", "false", "off"}:
        return False
    if flag in {"1", "true", "on"}:
        return True
    if os.environ.get("ZHOL_CHROME_CONTAINER", "").strip() == "1":
        return True
    binary = os.environ.get("ZHOL_CHROME_BINARY", "").strip()
    return bool(binary) and Path(binary).is_file()


def local_base_url() -> str | None:
    """Loopback test origin. A public host, including 2gis.kz, is ignored."""
    raw = os.environ.get(LOCAL_BASE_ENV, "").strip().rstrip("/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in LOCAL_HOSTS:
        return None
    if "2gis." in host:
        return None
    return raw


def local_html_root() -> Path | None:
    raw = os.environ.get(LOCAL_HTML_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return None
    if not path.is_dir():
        return None
    return path


def active_source() -> str:
    if local_base_url():
        return "localhost"
    if local_html_root() is not None:
        return "local_html"
    return "live_2gis"


def source_authorized(source: str | None = None) -> bool:
    kind = active_source() if source is None else source
    if kind == "live_2gis":
        return live_2gis_authorized()
    if kind in {"localhost", "local_html"}:
        return True
    return False


def browser_collection_authorized() -> bool:
    """The active source may be collected. Engine readiness is a separate check."""
    return source_authorized()


def browser_mode() -> dict:
    source = active_source()
    origin = local_base_url()
    engine = browser_engine_available()
    scope = grant_scope() if source == "live_2gis" else None
    if source == "live_2gis" and scope is None:
        available = False
        note = BLOCKER
    elif not engine:
        available = False
        note = ENGINE_BLOCKER
    elif source == "live_2gis":
        available = True
        note = LOCAL_NOTE_READY if scope == "local" else COMMERCIAL_NOTE
    else:
        available = True
        target = origin or "локальный HTML"
        note = f"{LOCAL_NOTE} Адрес сбора: {target}."
    label = (
        "Публичные карточки 2ГИС, существующий сборщик"
        if source == "live_2gis"
        else "Локальный HTML или localhost, существующий сборщик"
    )
    return {
        "id": SOURCE_MODE,
        "kind": "browser",
        "label": label,
        "available": available,
        "cities": list(BROWSER_CITIES),
        "note": note,
        "source_kind": source,
        "engine": engine,
        "origin": origin,
        "authorization_scope": scope,
    }


def refuse_browser_collection() -> None:
    if active_source() != "live_2gis" and not browser_engine_available():
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
    """Validate one authorized batch. This does not open Chrome or 2gis.kz."""
    if not source_authorized():
        refuse_browser_collection()
    if active_source() != "live_2gis" and not browser_engine_available():
        raise BrowserCollectionRefused(ENGINE_BLOCKER)
    if platforms != ["2gis"]:
        raise BatchError("Для существующего сборщика 2ГИС доступен только источник 2ГИС.")
    if limit < 1 or limit > 500:
        raise BatchError("Лимит должен быть от 1 до 500.")
    city_name = _browser_city(city)
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
            raise BatchError("Укажите категорию.")
        if len(cleaned) > 80:
            raise BatchError("Слишком длинное название категории.")
        key = _key(cleaned)
        if key in table.ambiguous:
            raise BatchError(f"Категория «{cleaned}» слишком общая. Выберите точное название из списка.")
        if key not in table.exact:
            raise BatchError(f"Категория «{cleaned}» не поддерживается. Напишите её в дополнительных запросах.")
        _sector, category, subcategory = table.exact[key]
        add(subcategory or category)
    for raw in queries:
        text = " ".join(str(raw).split())
        if not text:
            continue
        if len(text) > 120:
            raise BatchError("Слишком длинный дополнительный запрос.")
        cleaned = _strip_city(text, city_name)
        if not cleaned:
            raise BatchError("Дополнительный запрос не должен состоять только из названия города.")
        add(cleaned)
    if not labels:
        raise BatchError("Выберите хотя бы одну категорию или укажите дополнительный запрос.")
    return [
        {
            "source_mode": SOURCE_MODE,
            "city": city_name,
            "category": label,
            "platforms": ["2gis"],
            "limit": limit,
            "catalog": SOURCE_MODE,
        }
        for label in labels
    ]


def _browser_city(city: str) -> str:
    try:
        name, _slug = twogis_slug(city)
    except ValueError as exc:
        raise BatchError(str(exc)) from exc
    return name
