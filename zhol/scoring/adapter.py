from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from zhol.timeutil import ALMATY

PRICES_2GIS = {"прайс компании": "company_price", "фото прайса": "photo_only", "пусто": "empty"}
PRICES_YANDEX = {"есть": "yes", "есть, давно не обновлялись": "yes_outdated", "нет": "no"}
STATUS_YANDEX = {"найдена": "found", "найдена, адрес другой": "other_address", "не найдена": "not_found"}
STATUS_GOOGLE = {"найдена": "found", "найдена, адрес или филиал другой": "other_address", "не найдена": "not_found"}

# (platform, field key, etalon CSV column)
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("2gis", "rating", "2гис_рейтинг"),
    ("2gis", "ratings_count", "2гис_оценок"),
    ("2gis", "reviews_count", "2гис_отзывов"),
    ("2gis", "photos_count", "2гис_фото"),
    ("2gis", "reviews_shown", "2гис_показано_отзывов"),
    ("2gis", "replies_count", "2гис_ответов"),
    ("2gis", "last_review_date", "2гис_последний_отзыв"),
    ("2gis", "is_ad", "2гис_реклама"),
    ("2gis", "branches", "2гис_филиалов"),
    ("2gis", "has_phone", "2гис_телефон"),
    ("2gis", "has_whatsapp", "2гис_whatsapp"),
    ("2gis", "has_instagram", "2гис_instagram"),
    ("2gis", "link", "2гис_ссылка"),
    ("2gis", "prices", "2гис_цены"),
    ("2gis", "online_booking", "2гис_онлайн_запись"),
    ("2gis", "name", "название"),
    ("2gis", "address", "адрес_2гис"),
    ("2gis", "phones", "контактные_телефоны"),
    ("2gis", "whatsapp", "номер_whatsapp"),
    ("2gis", "instagram", "аккаунт_instagram"),
    ("yandex", "rating", "яндекс_рейтинг"),
    ("yandex", "ratings_count", "яндекс_оценок"),
    ("yandex", "reviews_count", "яндекс_отзывов"),
    ("yandex", "photos_count", "яндекс_фото"),
    ("yandex", "prices", "яндекс_цены"),
    ("yandex", "news_count", "яндекс_новости"),
    ("yandex", "owner_maintains", "яндекс_владелец_следит"),
    ("google", "rating", "google_рейтинг"),
    ("google", "reviews_count", "google_отзывов"),
    ("google", "website", "google_сайт"),
    ("google", "owner_claimed", "google_права_заявлены"),
)
# Keys of 04 sections 1.1-1.3 without a CSV column: stored as null.
UNSEEN_KEYS = {
    "2gis": ("category", "city", "district", "email", "description", "hours"),
    "yandex": (
        "name",
        "address",
        "category",
        "phones",
        "city",
        "whatsapp",
        "instagram",
        "website",
        "email",
        "description",
        "hours",
    ),
    "google": (
        "name",
        "address",
        "category",
        "phones",
        "photos_count",
        "city",
        "whatsapp",
        "instagram",
        "email",
        "description",
        "hours",
    ),
}
STATUS_COLUMNS = {"yandex": ("яндекс_статус", STATUS_YANDEX), "google": ("google_статус", STATUS_GOOGLE)}
ENUMS = {("2gis", "prices"): PRICES_2GIS, ("yandex", "prices"): PRICES_YANDEX}


def _inverse(mapping: Mapping[str, str]) -> dict[str, str]:
    return {v: k for k, v in mapping.items()}


def _get(snapshot: Any, name: str) -> Any:
    if snapshot is None:
        return None
    return snapshot.get(name) if isinstance(snapshot, Mapping) else getattr(snapshot, name)


def local_date(moment: datetime) -> date:
    return moment.astimezone(ALMATY).date()


def derive(snapshot_2gis: Any) -> dict[str, Any]:
    fields = _get(snapshot_2gis, "fields") or {}
    shown, replies = fields.get("reviews_shown"), fields.get("replies_count")
    if shown is None or replies is None:
        share = None
    else:
        share = 0 if shown == 0 else replies / shown
    last, collected = fields.get("last_review_date"), _get(snapshot_2gis, "collected_at")
    fresh = None if last is None or collected is None else (local_date(collected) - date.fromisoformat(last)).days
    return {"reply_share": share, "fresh_review_days": fresh}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def discrepancies_text(discrepancies: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for item in discrepancies or ():
        if item.get("legacy_text"):
            parts.append(str(item["legacy_text"]))
        elif item.get("field"):
            values = item.get("values") or {}
            parts.append(f"{item['field']}: " + "; ".join(f"{k} {v}" for k, v in values.items()))
    return "; ".join(parts)


def to_score_row(
    snapshots: Mapping[str, Any],
    derived: Mapping[str, Any],
    discrepancies: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Flat row with the etalon CSV columns that score() reads. null becomes "" exactly as an empty CSV cell."""
    row: dict[str, str] = {}
    for platform, key, column in COLUMNS:
        value = (_get(snapshots.get(platform), "fields") or {}).get(key)
        enum = ENUMS.get((platform, key))
        if enum is not None and value is not None:
            value = _inverse(enum)[value]
        row[column] = _cell(value)
    for platform, (column, mapping) in STATUS_COLUMNS.items():
        status = _get(snapshots.get(platform), "match_status")
        row[column] = _inverse(mapping)[status] if status else ""
    collected = _get(snapshots.get("2gis"), "collected_at")
    row["дата_просмотра"] = local_date(collected).isoformat() if collected else ""
    row["2гис_доля_ответов"] = _cell(derived.get("reply_share"))
    row["расхождения"] = discrepancies_text(discrepancies)
    return row
