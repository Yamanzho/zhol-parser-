"""One bounded plan per source. Combinations run later, in order, on the existing collector.

The source cap is the total number of distinct cards for that source. It is not
multiplied by cities or queries. Nothing here opens a browser or a map site.
"""

from __future__ import annotations

from typing import Any

from zhol.agents.scout.localities import all_localities, match_locality, supports
from zhol.agents.scout.setup import LIMIT_MAX, LIMIT_MIN, SetupError, _category_rows, _entries
from zhol.export.mailing import load_categories

LARGE_COMBINATIONS = 12
_PLATFORMS = ("2gis", "google", "yandex")
_MODE = {"2gis": "browser_2gis", "google": "browser_google", "yandex": "browser_yandex"}


class PlanRejected(SetupError):
    def __init__(self, message: str, preview: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.preview = preview


def normalize_queries(values: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = " ".join(str(raw).split())
        if not text:
            continue
        key = text.casefold().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found


def active_categories() -> list[dict[str, Any]]:
    rows = _category_rows(_entries(), load_categories(), {}, {})
    if not rows:
        raise PlanRejected("Каталог категорий пуст. Режим «Все категории» недоступен.")
    return rows


def category_groups(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    source = active_categories() if rows is None else rows
    groups: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for row in source:
        sector = str(row.get("sector") or "Без группы")
        bucket = index.get(sector)
        if bucket is None:
            bucket = {"sector": sector, "labels": []}
            index[sector] = bucket
            groups.append(bucket)
        bucket["labels"].append(row["label"])
    return groups


def selection_needs_confirm(*, all_cities: bool, all_categories: bool, combinations: int) -> bool:
    return all_cities or all_categories or combinations > LARGE_COMBINATIONS


def build_collection_plan(
    *,
    cities: list[str],
    all_cities: bool,
    categories: list[str],
    queries: list[str],
    all_categories: bool,
    platforms: list[str],
    limit: int,
    combination_limit: int | None,
    confirm: bool,
    skip_unsupported: bool,
    ready: dict[str, bool],
    blocked_note: dict[str, str],
) -> dict[str, Any]:
    if limit < LIMIT_MIN or limit > LIMIT_MAX:
        raise PlanRejected(f"Лимит должен быть от {LIMIT_MIN} до {LIMIT_MAX}.")
    if combination_limit is not None and (combination_limit < LIMIT_MIN or combination_limit > limit):
        raise PlanRejected("Лимит на сочетание не может быть больше лимита на источник.")
    chosen = _platforms(platforms)
    catalog = active_categories()
    known = {str(row["label"]).casefold().replace("ё", "е"): str(row["label"]) for row in catalog}
    if all_categories:
        labels = [str(row["label"]) for row in catalog]
        manual = []
    else:
        labels = []
        for raw in categories:
            text = " ".join(str(raw).split())
            if not text:
                continue
            key = text.casefold().replace("ё", "е")
            if key not in known:
                raise PlanRejected(f"Категория «{text}» не входит в каталог.")
            if known[key] not in labels:
                labels.append(known[key])
        manual = normalize_queries(queries)
    phrases = labels if all_categories else normalize_queries([*labels, *manual])
    if not phrases:
        raise PlanRejected("Выберите хотя бы одну категорию или укажите поисковый запрос.")
    selected = _cities(cities, all_cities)
    skipped: list[dict[str, str]] = []
    plans: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for platform in chosen:
        if not ready.get(platform):
            errors.append(
                {
                    "platform": platform,
                    "detail": blocked_note.get(platform) or "Источник недоступен.",
                }
            )
            continue
        combinations: list[dict[str, str]] = []
        for city in selected:
            if not supports(city.name, platform):
                skipped.append(
                    {
                        "city": city.name,
                        "platform": platform,
                        "reason": "Источник не поддержан для этого города.",
                    }
                )
                continue
            for phrase in phrases:
                kind = "category" if phrase in labels else "query"
                combinations.append(
                    {
                        "city_id": city.id,
                        "city": city.name,
                        "region": city.region,
                        "query": phrase,
                        "kind": kind,
                        "status": "pending",
                    }
                )
        if not combinations:
            continue
        plans.append(
            {
                "platform": platform,
                "source_mode": _MODE[platform],
                "source_cap": limit,
                "combination_limit": combination_limit,
                "combinations": combinations,
                "cursor": 0,
            }
        )
    combination_count = sum(len(item["combinations"]) for item in plans)
    if skipped and not skip_unsupported:
        preview = _preview(selected, phrases, chosen, plans, skipped, limit, all_cities, all_categories)
        sample = ", ".join(f"{item['city']} / {item['platform']}" for item in skipped[:8])
        extra = "" if len(skipped) <= 8 else f" и ещё {len(skipped) - 8}"
        raise PlanRejected(
            f"Есть неподдержанные сочетания города и источника: {sample}{extra}. "
            "Они не поставлены в очередь. Уберите их или явно пропустите.",
            preview,
        )
    if not plans:
        detail = errors[0]["detail"] if errors else "Нет сочетаний, которые можно собрать."
        raise PlanRejected(detail)
    needs_confirm = selection_needs_confirm(
        all_cities=all_cities,
        all_categories=all_categories,
        combinations=combination_count,
    )
    preview = _preview(selected, phrases, chosen, plans, skipped if skip_unsupported else [], limit, all_cities, all_categories)
    if needs_confirm and not confirm:
        raise PlanRejected(
            "Большой сбор нужно подтвердить. "
            f"Городов: {preview['cities']}. Категорий и запросов: {preview['phrases']}. "
            f"Источников: {preview['sources']}. Сочетаний: {preview['combinations']}. "
            f"Верхняя оценка сохранённых компаний: {preview['estimated_upper_bound']}.",
            preview,
        )
    return {
        "plans": plans,
        "errors": errors,
        "skipped": skipped if skip_unsupported else [],
        "phrases": phrases,
        "categories": labels,
        "queries": manual if not all_categories else [],
        "preview": preview,
        "all_cities": all_cities,
        "all_categories": all_categories,
    }


def _cities(cities: list[str], all_cities: bool) -> list[Any]:
    if all_cities:
        return list(all_localities())
    if not cities:
        raise PlanRejected("Выберите хотя бы один город.")
    found = []
    seen: set[str] = set()
    for raw in cities:
        match = match_locality(raw)
        if match is None:
            shown = " ".join(str(raw).split()) or "без названия"
            raise PlanRejected(f"Город «{shown}» нет в справочнике. Сбор не переключается на Алматы.")
        if match.id in seen:
            continue
        seen.add(match.id)
        found.append(match)
    return found


def _platforms(values: list[str]) -> list[str]:
    if not values:
        raise PlanRejected("Выберите хотя бы один источник.")
    found: list[str] = []
    for value in values:
        name = str(value).strip().lower()
        if name not in _PLATFORMS:
            raise PlanRejected("Неизвестный источник сбора.")
        if name not in found:
            found.append(name)
    return found


def _preview(
    cities: list[Any],
    phrases: list[str],
    platforms: list[str],
    plans: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    limit: int,
    all_cities: bool,
    all_categories: bool,
) -> dict[str, Any]:
    sources = len(plans)
    return {
        "cities": len(cities),
        "catalog_cities": len(all_localities()),
        "phrases": len(phrases),
        "sources": sources,
        "requested_sources": len(platforms),
        "combinations": sum(len(item["combinations"]) for item in plans),
        "skipped": len(skipped),
        "estimated_upper_bound": limit * sources,
        "limit_per_source": limit,
        "all_cities": all_cities,
        "all_categories": all_categories,
        "all_cities_meaning": "every validated city in the local catalog",
    }
