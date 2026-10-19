"""Normalize a search hit. A value that is not on the page stays empty."""

from __future__ import annotations

import re

from zhol.agents.scout.normalize import addresses_compatible, names_compatible, normalize_phone
from zhol.export.mailing import ClassTable, classify, load_categories

_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_EMAIL = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)
_SKIP_HOSTS = (
    "instagram.",
    "wa.me",
    "whatsapp.",
    "yandex.",
    "google.",
    "g.page",
    "goo.gl",
    "2gis.",
    "facebook.",
    "fb.com",
    "t.me",
    "telegram.",
    "vk.com",
    "youtube.",
)
_RESERVED = frozenset({"p", "reel", "reels", "explore", "stories", "accounts"})


def clean_name(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace("«", " ").replace("»", " ").replace('"', " ")
    text = " ".join(text.split())
    return text or None


def clean_address(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.split())
    return text or None


def verified_phone(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    return normalize_phone(str(value))


def verified_whatsapp(href: str | None, text: str | None) -> str | None:
    source = href or ""
    if "wa.me/" not in source.lower() and "whatsapp" not in f"{text or ''} {source}".lower():
        return None
    number = verified_phone(source) or verified_phone(text)
    return number


def verified_instagram(href: str | None, text: str | None) -> str | None:
    raw = (href or text or "").strip()
    if not raw:
        return None
    if "instagram.com/" in raw.lower():
        raw = raw.split("?")[0].rstrip("/").split("/")[-1]
    handle = raw[1:] if raw.startswith("@") else raw
    if not _HANDLE.match(handle) or handle.lower() in _RESERVED:
        return None
    return handle


def verified_website(href: str | None, text: str | None) -> str | None:
    raw = (href or text or "").strip()
    if not raw.lower().startswith("http"):
        return None
    host = raw.split("/")[2].lower() if "://" in raw else ""
    if any(part in host for part in _SKIP_HOSTS):
        return None
    return raw.rstrip("/")


def verified_email(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not _EMAIL.match(text):
        return None
    return text


def normalize_category(label: str | None, table: ClassTable | None = None) -> tuple[str | None, str | None, str | None]:
    if not label or not str(label).strip():
        return None, None, None
    return classify([str(label)], table or load_categories())


def same_branch(
    left_name: str | None,
    left_address: str | None,
    left_firm: str | None,
    right_name: str | None,
    right_address: str | None,
    right_firm: str | None,
) -> bool:
    if left_firm and right_firm:
        return left_firm == right_firm
    if not left_name or not right_name or not left_address or not right_address:
        return False
    return names_compatible(left_name, right_name) and addresses_compatible(left_address, right_address)
