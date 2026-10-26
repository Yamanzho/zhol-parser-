"""One reading of a collection batch result for the panel and the versioned API.

The 2GIS collector stores its stop reason as the text of search.py, the map
collectors store a short code. Both are read here. Reaching the requested
limit is a normal result, and only a confirmed end says the search ran out.
"""

from __future__ import annotations

from typing import Any

# Same texts as search.STOP_*. A test keeps them equal.
SEARCH_STOP_TEXTS = {
    "Достигнут отладочный лимит уникальных карточек": "limit",
    "Подтверждён конец выдачи": "confirmed_end",
    "Повторяющаяся выдача без новых идентификаторов": "repeat",
    "Три страницы подряд без новых карточек, конец выдачи не подтверждён": "stall",
    "Достигнут предел страниц поиска, конец выдачи не подтверждён": "page_cap",
    "Сбор ссылок прерван": "stopped",
}
STOP_CODES = frozenset({"limit", "confirmed_end", "repeat", "stall", "page_cap", "stopped", "page_error", "unconfirmed"})
BLOCKING_CLASSES = frozenset({"captcha", "access_restriction"})

LABELS = {
    "queued": "В очереди",
    "running": "Идёт сбор",
    "paused": "На паузе",
    "blocked": "Нужна проверка или доступ ограничен",
    "stopped": "Остановлен оператором",
    "interrupted": "Прерван, можно продолжить",
    "failed": "Ошибка",
    "completed": "Завершён",
    "limit_reached": "Достигнут лимит",
    "partial": "Частичный результат",
}
_OPEN_END = {
    "repeat": "Выдача повторялась без новых карточек, конец выдачи не подтверждён.",
    "stall": "Три страницы подряд без новых карточек, конец выдачи не подтверждён.",
    "page_cap": "Достигнут предел страниц поиска, конец выдачи не подтверждён.",
    "page_error": "Страница выдачи не открылась, конец выдачи не подтверждён.",
    "unconfirmed": "Конец выдачи не подтверждён.",
}


QUERY_SCOPE = "Сбор по одному поисковому запросу в одном городе, а не обход всего города."
_LIVE_HOSTS = {
    "browser_2gis": ("live_2gis", "Живой сбор с 2gis.kz в браузере"),
    "browser_yandex": ("browser_yandex", "Живой сбор с Яндекс Карт в браузере"),
    "browser_google": ("browser_google", "Живой сбор с Google Maps в браузере"),
}


def collection_mode(source_mode: str | None, stored: dict[str, Any]) -> dict[str, str]:
    """Where the batch really read its cards. Missing facts are reported as missing."""
    source = stored.get("browser_source")
    if not isinstance(source, str):
        verification = stored.get("manual_verification")
        source = verification.get("source_kind") if isinstance(verification, dict) else None
    if source_mode in _LIVE_HOSTS:
        live_source, live_label = _LIVE_HOSTS[source_mode]
        if source == live_source:
            code, label = "live", live_label
        elif source == "localhost":
            code, label = "local", "Локальная проверка: страницы с этого компьютера, живой сайт не открывался"
        elif source == "local_html":
            code, label = "local", "Сохранённые HTML-страницы, живой сайт не открывался"
        else:
            code, label = "unknown", "Сбор через браузер, источник страниц не записан"
    elif source_mode == "live_2gis":
        code, label = "api", "Catalog API 2ГИС, сайт не открывался"
    elif source_mode == "manual":
        code, label = "saved", "Импорт сохранённых страниц, живой сайт не открывался"
    elif source_mode == "fixture":
        code, label = "fixture", "Тестовый каталог, живой сайт не открывался"
    else:
        code, label = "unknown", "Источник страниц не записан"
    return {"code": code, "label": label, "scope": "query", "scope_label": QUERY_SCOPE}


def stop_reason_code(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text in STOP_CODES:
        return text
    if text in SEARCH_STOP_TEXTS:
        return SEARCH_STOP_TEXTS[text]
    if text.startswith("Не удалось открыть страницу выдачи") or text.startswith("Сайт открыл страницу"):
        return "page_error"
    return "unconfirmed"


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _blocked(stored: dict[str, Any]) -> str | None:
    verification = stored.get("manual_verification")
    if isinstance(verification, dict) and verification.get("outcome") in {"failed", "not_permitted", "unavailable"}:
        return "Проверка в браузере не пройдена или недоступна."
    interruption = stored.get("interruption")
    if isinstance(interruption, dict) and interruption.get("classification") in BLOCKING_CLASSES:
        if interruption.get("classification") == "captcha":
            return "Площадка попросила проверку, сбор остановлен."
        return "Площадка ограничила доступ, сбор остановлен."
    return None


def collection_outcome(
    status: str,
    stored: dict[str, Any],
    *,
    collected: int,
    limit: int | None,
    rejected: int = 0,
) -> dict[str, Any]:
    """Result code, its label and the facts behind it. `status` stays the stored batch status."""
    reason = stop_reason_code(stored.get("stop_reason"))
    metrics = stored.get("metrics") if isinstance(stored.get("metrics"), dict) else {}
    errors = max(_count(metrics.get("failed_records")), _count(rejected))
    limit_reached = reason == "limit" or (bool(limit) and collected >= int(limit or 0))
    exhausted = reason == "confirmed_end"
    warnings: list[str] = []
    note = ""
    if errors:
        warnings.append(f"Не прочитано карточек: {errors}.")
    if stored.get("retry_pending") is True:
        warnings.append("Есть карточки, которые ждут повтора.")

    if status in {"queued", "running", "paused"}:
        code = status
    elif status == "awaiting_manual_verification":
        code = "blocked"
        note = "Площадка попросила проверку в браузере. Её проходит человек."
    elif status == "stopped":
        blocked = _blocked(stored)
        code = "blocked" if blocked else "stopped"
        note = blocked or "Сбор остановлен оператором. Собранные компании сохранены."
    elif status in {"interrupted", "failed", "dead"}:
        blocked = _blocked(stored)
        if blocked:
            code, note = "blocked", blocked
        elif status == "interrupted":
            code, note = "interrupted", "Сбор прерван. Собранные компании сохранены, сбор можно продолжить."
        else:
            code = "partial" if collected else "failed"
            note = "Сбор завершился ошибкой." + (" Собранные компании сохранены." if collected else "")
    elif status == "completed":
        if errors:
            code = "partial"
        elif exhausted:
            code = "completed"
        elif limit_reached:
            code = "limit_reached"
        elif reason is None:
            code = "completed"
        else:
            code = "partial"
        if reason in _OPEN_END and not limit_reached:
            warnings.append(_OPEN_END[reason])
        if exhausted:
            note = "Подтверждён конец выдачи." if limit_reached else "Подтверждён конец выдачи: карточек меньше лимита."
        elif limit_reached:
            note = f"Сбор остановлен на запрошенном лимите ({limit}). Выдача могла продолжаться дальше."
    else:
        code = status
    return {
        "code": code,
        "label": LABELS.get(code, code),
        "note": note,
        "limit_reached": bool(limit_reached),
        "search_exhausted": exhausted,
        "stop_reason": reason,
        "warnings": warnings,
    }
