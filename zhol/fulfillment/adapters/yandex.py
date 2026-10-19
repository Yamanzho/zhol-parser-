"""Yandex Business cabinet. Owner-maintained, news, and the prices counter are observed, not written.

Description is writable from an approved client fact, or from a checked generated draft when the listing description is empty.
This adapter still does not submit the form.
"""

from __future__ import annotations

from zhol.fulfillment.adapters.base import CabinetAdapter


class YandexBusinessAdapter(CabinetAdapter):
    platform = "yandex"
    submit_buttons = {"create": "Создать", "update": "Сохранить"}
    forbidden = frozenset(
        {
            "rating",
            "owner_maintains",
            "news_count",
            "prices",
            "online_booking",
            "owner_claimed",
        }
    )
