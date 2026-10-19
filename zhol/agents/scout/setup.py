"""Visual collection setup. It reads the saved catalog and does not open a map site."""

from __future__ import annotations

from pathlib import Path

import yaml

from zhol.agents.scout.browser_2gis import browser_mode
from zhol.agents.scout.catalog import PLATFORMS, BatchError, load_catalog
from zhol.agents.scout.live_2gis import live_mode
from zhol.agents.scout.localities import all_localities
from zhol.browser.collector import panel_browser_modes
from zhol.export.mailing import DEFAULT_CATEGORIES, ClassTable, _key, load_categories

CATALOG_NAME = "catalog.yaml"
DEFAULT_LIMIT = 20
LIMIT_MIN = 1
LIMIT_MAX = 500
AVAILABLE = frozenset({"2gis"})
SOURCE_LABEL = {"2gis": "2ГИС", "yandex": "Яндекс Карты", "google": "Google Maps"}


class SetupError(ValueError):
    pass


def collection_setup(catalog_file: Path | None) -> dict:
    entries = _entries()
    table = load_categories()
    live = live_mode()
    browser = browser_mode()
    sources = _sources(browser, live)
    base = {
        "sources": sources,
        "limit": {"min": LIMIT_MIN, "max": LIMIT_MAX, "default": DEFAULT_LIMIT},
        "skip_existing": {
            "supported": True,
            "configurable": False,
            "note": "Компания с тем же номером в 2ГИС не создается второй раз.",
        },
        "delay": {"supported": False},
        "stop": {"supported": True},
        "resume": {"supported": True},
        "live_collection": False,
        "live_2gis": {"available": live["available"], "cities": live["cities"], "blocker": live["blocker"]},
        "browser_2gis": {
            "available": browser["available"],
            "cities": browser["cities"],
            "blocker": None if browser["available"] else browser["note"],
            "source_kind": browser["source_kind"],
            "engine": browser["engine"],
            "origin": browser["origin"],
            "authorization_scope": browser.get("authorization_scope"),
        },
        "collection_modes": _collection_modes(live, fixture_ready=False),
        "browser_sources": panel_browser_modes(),
        "browser_note": browser["note"],
        "note": _setup_note(browser),
        **_catalog_facts(),
    }
    if catalog_file is None or not catalog_file.is_file():
        return {
            **base,
            "ready": live["available"],
            "error": "Сохраненный каталог не найден. Запуск сбора из формы недоступен.",
            "default_city": "",
            "cities": [],
            "categories": _category_rows(entries, table, {}, {}),
        }
    try:
        rows = load_catalog(catalog_file)
    except BatchError:
        return {
            **base,
            "ready": live["available"],
            "error": "Сохраненный каталог не читается. Запуск сбора из формы недоступен.",
            "default_city": "",
            "cities": [],
            "categories": [],
        }
    cities, spellings, counts = _index(rows)
    if not cities:
        return {
            **base,
            "ready": live["available"],
            "error": "В сохраненном каталоге нет городов.",
            "default_city": "",
            "cities": [],
            "categories": _category_rows(entries, table, spellings, counts),
        }
    city_names = _city_names(cities)
    return {
        **base,
        "ready": True,
        "default_city": "Алматы" if "Алматы" in city_names else city_names[0],
        "cities": city_names,
        "categories": _category_rows(entries, table, spellings, counts, city_names),
        "collection_modes": _collection_modes(live, fixture_ready=True),
    }


def _catalog_facts() -> dict:
    from zhol.agents.scout.collection_plan import active_categories, category_groups

    localities = [item.public() for item in all_localities()]
    try:
        rows = active_categories()
        catalog = {
            "ready": True,
            "count": len(rows),
            "error": None,
            "groups": category_groups(rows),
        }
    except Exception as exc:
        rows = []
        catalog = {"ready": False, "count": 0, "error": str(exc), "groups": []}
    return {
        "localities": localities,
        "locality_count": len(localities),
        "default_cities": ["Алматы"] if any(item["name"] == "Алматы" for item in localities) else [],
        "category_catalog": catalog,
    }


def _setup_note(browser: dict) -> str:
    if browser.get("available") and browser.get("source_kind") != "live_2gis":
        return "Тестовый каталог не открывает сайт. Локальный браузерный источник включён. Сайт 2ГИС не открывается."
    if browser.get("available") and browser.get("authorization_scope") == "local":
        return (
            "Тестовый каталог не открывает сайт. Локальный некоммерческий сбор 2ГИС включён. "
            "Коммерческое разрешение не подтверждено."
        )
    if browser.get("available") and browser.get("authorization_scope") == "commercial":
        return "Тестовый каталог не открывает сайт. Коммерческий сбор 2ГИС включён."
    return "Тестовый каталог не открывает сайт. Режим браузера показан и выключен, пока письменное разрешение не подтверждено."


def _collection_modes(live: dict, *, fixture_ready: bool) -> list[dict]:
    return [
        *panel_browser_modes(),
        {
            "id": live["id"],
            "label": live["label"],
            "available": live["available"],
            "note": live["note"],
        },
        {
            "id": "fixture",
            "label": "Тестовый каталог",
            "available": fixture_ready,
            "note": "Берет компании из сохраненного списка. Сайт 2ГИС не открывается.",
        },
    ]


def build_plans(
    *,
    city: str,
    categories: list[str],
    queries: list[str],
    platforms: list[str],
    limit: int,
    catalog_file: Path,
    catalog_name: str = CATALOG_NAME,
) -> list[dict]:
    if not catalog_file.is_file():
        raise SetupError("Сохраненный каталог не найден. Запуск сбора из формы недоступен.")
    if limit < LIMIT_MIN or limit > LIMIT_MAX:
        raise SetupError(f"Лимит должен быть от {LIMIT_MIN} до {LIMIT_MAX}.")
    chosen = _platforms(platforms)
    try:
        rows = load_catalog(catalog_file)
    except BatchError as exc:
        raise SetupError("Сохраненный каталог не читается.") from exc
    cities, spellings, counts = _index(rows)
    city_name = _match_city(city, cities)
    table = load_categories()
    labels: list[str] = []
    seen: set[str] = set()
    for raw in categories:
        label = _resolve(raw, city_name, table, spellings, counts, kind="category")
        key = _key(label)
        if key not in seen:
            seen.add(key)
            labels.append(label)
    for raw in queries:
        text = " ".join(str(raw).split())
        if not text:
            continue
        label = _resolve(text, city_name, table, spellings, counts, kind="query")
        key = _key(label)
        if key not in seen:
            seen.add(key)
            labels.append(label)
    if not labels:
        raise SetupError("Выберите хотя бы одну категорию или укажите дополнительный запрос.")
    return [
        {
            "source_mode": "fixture",
            "city": city_name,
            "category": label,
            "platforms": chosen,
            "limit": limit,
            "catalog": catalog_name,
        }
        for label in labels
    ]


def _sources(browser: dict, live: dict) -> list[dict]:
    from zhol.agents.scout.browser_google import browser_mode as google_mode
    from zhol.agents.scout.browser_yandex import browser_mode as yandex_mode

    twogis_live, twogis_note = _twogis_live(browser, live)
    yandex = yandex_mode()
    google = google_mode()
    rows = {
        "2gis": {
            "note": "Тестовый каталог: доступен.",
            "live_available": twogis_live,
            "live_note": twogis_note,
            "collection_state": "available" if twogis_live else ("restricted" if browser.get("authorization_scope") else "unauthorized"),
        },
        "yandex": {
            "note": "Тестовый каталог: недоступен.",
            "live_available": bool(yandex.get("live_available")),
            "live_note": yandex["note"],
            "collection_state": yandex.get("collection_state") or "unauthorized",
            "limit_note": yandex.get("limit_note") or "",
        },
        "google": {
            "note": "Тестовый каталог: недоступен.",
            "live_available": bool(google.get("live_available")),
            "live_note": google["note"],
            "collection_state": google.get("collection_state") or "unauthorized",
        },
    }
    return [
        {
            "id": name,
            "label": SOURCE_LABEL[name],
            "available": name in AVAILABLE,
            **rows[name],
        }
        for name in PLATFORMS
    ]


def _twogis_live(browser: dict, live: dict) -> tuple[bool, str]:
    browser_live = browser.get("source_kind") == "live_2gis" and bool(browser.get("available"))
    catalog_live = bool(live.get("available"))
    parts: list[str] = []
    if browser.get("source_kind") == "live_2gis":
        parts.append(str(browser.get("note") or ""))
    else:
        parts.append("Сайт 2ГИС не открывается: сборщик направлен на локальный HTML или localhost.")
    if catalog_live:
        parts.append(str(live.get("note") or ""))
    elif not browser_live:
        parts.append(str(live.get("blocker") or live.get("note") or ""))
    return browser_live or catalog_live, " ".join(part for part in parts if part)


def _platforms(values: list[str]) -> list[str]:
    if not values:
        raise SetupError("Выберите хотя бы один доступный источник. Сейчас это только 2ГИС.")
    found: list[str] = []
    blocked: list[str] = []
    for value in values:
        name = str(value).strip().lower()
        if name not in PLATFORMS:
            raise SetupError("Неизвестный источник сбора.")
        if name not in AVAILABLE:
            blocked.append(SOURCE_LABEL[name])
            continue
        if name not in found:
            found.append(name)
    if blocked:
        raise SetupError("Источник недоступен: " + ", ".join(blocked) + ". Живой сбор выключен.")
    if not found:
        raise SetupError("Выберите хотя бы один доступный источник. Сейчас это только 2ГИС.")
    return found


def _entries() -> list[dict]:
    data = yaml.safe_load(DEFAULT_CATEGORIES.read_text(encoding="utf-8"))
    rows = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _index(rows: list) -> tuple[dict[str, str], dict[str, str], dict[tuple[str, str], int]]:
    cities: dict[str, str] = {}
    spellings: dict[str, str] = {}
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        city_key = _key(row.city)
        category_key = _key(row.category)
        cities.setdefault(city_key, row.city)
        spellings.setdefault(category_key, row.category)
        counts[(city_key, category_key)] = counts.get((city_key, category_key), 0) + 1
    return cities, spellings, counts


def _city_names(cities: dict[str, str]) -> list[str]:
    names = list(cities.values())
    if "Алматы" in names:
        names.remove("Алматы")
        return ["Алматы", *names]
    return names


def _category_rows(
    entries: list[dict],
    table: ClassTable,
    spellings: dict[str, str],
    counts: dict[tuple[str, str], int],
    city_names: list[str] | None = None,
) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()

    def add(label: object, sector: object) -> None:
        text = " ".join(str(label or "").split())
        key = _key(text)
        if not text or not key or key in seen or key in table.ambiguous or key not in table.exact:
            return
        seen.add(key)
        display = spellings.get(key, text)
        by_city = {name: counts.get((_key(name), key), 0) for name in city_names or []}
        found.append({"label": display, "sector": " ".join(str(sector or "").split()), "counts": by_city})

    for entry in entries:
        add(entry.get("category"), entry.get("sector"))
        add(entry.get("subcategory"), entry.get("sector"))
    for key, spelling in spellings.items():
        if key in seen or key not in table.exact:
            continue
        sector = table.exact[key][0]
        add(spelling, sector)
    return found


def _match_city(city: str, cities: dict[str, str]) -> str:
    key = _key(city)
    if not key or key not in cities:
        raise SetupError(f"Город «{city.strip() or 'без названия'}» не входит в сохраненный каталог.")
    return cities[key]


def _strip_city(text: str, city: str) -> str:
    words = text.split()
    city_words = city.split()
    if not city_words:
        return " ".join(words)
    city_keys = [_key(part) for part in city_words]
    size = len(city_words)
    changed = True
    while changed and len(words) >= size:
        changed = False
        if [_key(part) for part in words[:size]] == city_keys:
            words = words[size:]
            changed = True
            continue
        if [_key(part) for part in words[-size:]] == city_keys:
            words = words[:-size]
            changed = True
    return " ".join(words)


def _resolve(
    raw: str,
    city: str,
    table: ClassTable,
    spellings: dict[str, str],
    counts: dict[tuple[str, str], int],
    *,
    kind: str,
) -> str:
    cleaned = _strip_city(" ".join(raw.split()), city)
    if not cleaned:
        if kind == "query":
            raise SetupError("Дополнительный запрос не должен состоять только из названия города.")
        raise SetupError("Укажите категорию.")
    key = _key(cleaned)
    if key in table.ambiguous:
        raise SetupError(f"Категория «{cleaned}» слишком общая. Выберите точное название из списка.")
    if key not in table.exact:
        if kind == "query":
            raise SetupError(
                f"Запрос «{cleaned}» не поддерживается. Живой поиск по площадкам выключен. Выберите категорию из списка."
            )
        raise SetupError(f"Категория «{cleaned}» не поддерживается.")
    catalog_label = _catalog_label(key, table.exact[key], spellings)
    if catalog_label is None or counts.get((_key(city), _key(catalog_label)), 0) < 1:
        shown = catalog_label or cleaned
        raise SetupError(f"В сохраненном каталоге нет компаний категории «{shown}» для города {city}.")
    return catalog_label


def _catalog_label(key: str, triple: tuple[str, str, str], spellings: dict[str, str]) -> str | None:
    if key in spellings:
        return spellings[key]
    _sector, category, subcategory = triple
    if _key(category) in spellings:
        return spellings[_key(category)]
    if subcategory and _key(subcategory) in spellings:
        return spellings[_key(subcategory)]
    return None
