from __future__ import annotations

from zhol.scoring.adapter import COLUMNS, ENUMS, UNSEEN_KEYS

INT_KEYS = frozenset(
    {
        "ratings_count",
        "reviews_count",
        "photos_count",
        "reviews_shown",
        "replies_count",
        "branches",
        "news_count",
    }
)
FLOAT_KEYS = frozenset({"rating"})
BOOL_KEYS = frozenset(
    {
        "is_ad",
        "has_phone",
        "has_whatsapp",
        "has_instagram",
        "online_booking",
        "owner_maintains",
        "owner_claimed",
    }
)
DATE_KEYS = frozenset({"last_review_date"})
LIST_KEYS = frozenset({"phones"})
HREF_KEYS = frozenset({"link", "website"})
PLATFORMS = ("2gis", "yandex", "google")
PURPOSES = ("prospect", "order_fresh", "report", "subscription_start")
RECHECK_FIELDS = ("rating", "ratings_count", "photos_count", "prices", "online_booking")


def field_keys(platform: str) -> tuple[str, ...]:
    ordered = [key for source, key, _column in COLUMNS if source == platform]
    for key in UNSEEN_KEYS[platform]:
        if key not in ordered:
            ordered.append(key)
    return tuple(ordered)


def enum_values(platform: str, key: str) -> frozenset[str] | None:
    mapping = ENUMS.get((platform, key))
    if mapping is None:
        return None
    return frozenset(mapping.values())
