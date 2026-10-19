"""Google Business Profile. Services, WhatsApp, and Instagram are not written from this dictionary.

Description is writable from an approved client fact, or from a checked generated draft when the listing description is empty.
This adapter still does not submit the form.
"""

from __future__ import annotations

from zhol.fulfillment.adapters.base import CabinetAdapter


class GoogleBusinessAdapter(CabinetAdapter):
    platform = "google"
    submit_buttons = {"create": "Create", "update": "Сохранить"}
    forbidden = frozenset(
        {
            "rating",
            "owner_claimed",
            "whatsapp",
            "instagram",
            "services",
            "prices",
            "link",
            "messengers",
            "socials",
        }
    )
