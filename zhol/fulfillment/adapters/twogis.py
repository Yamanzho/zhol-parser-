"""2GIS owner cabinet. The public prices flag is observed, not written."""

from __future__ import annotations

from zhol.fulfillment.adapters.base import CabinetAdapter


class TwoGisAdapter(CabinetAdapter):
    platform = "2gis"
    submit_buttons = {"create": "Создать", "update": "Сохранить"}
    forbidden = frozenset(
        {
            "rating",
            "ratings_count",
            "reviews_count",
            "online_booking",
            "is_ad",
            "prices",
            "description",
            "owner_claimed",
            "owner_maintains",
        }
    )
