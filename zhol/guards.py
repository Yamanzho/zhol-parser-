from __future__ import annotations

import re

LONG_DASHES = ("\u2013", "\u2014")

NAP_FIELDS = frozenset({"name", "address", "phones", "category"})
PLATFORMS = frozenset({"2gis", "yandex", "google"})

FORBIDDEN_PHRASES = (
    "у вас всё плохо",
    "вы теряете клиентов",
    "уходят к соседям",
    "уходит к соседям",
    "уходит к конкурентам",
    "уходят к конкурентам",
    "гарантируем",
    "рост продаж",
    "увеличим",
    "в топ",
    "лучший",
    "уровень лучших салонов города",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


_FORBIDDEN_PATTERNS = tuple(
    (phrase, re.compile(rf"(?<!\w){re.escape(_normalize(phrase))}(?!\w)")) for phrase in FORBIDDEN_PHRASES
)


def no_long_dash(text: str) -> bool:
    return not any(dash in text for dash in LONG_DASHES)


def is_nap_field(field: str) -> bool:
    platform, sep, key = field.partition(".")
    if sep and platform in PLATFORMS:
        field = key
    return field in NAP_FIELDS


def forbidden_phrases(text: str) -> list[str]:
    normalized = _normalize(text)
    return [phrase for phrase, pattern in _FORBIDDEN_PATTERNS if pattern.search(normalized)]


def saved_discovery_only(source_mode: str, platform: str) -> None:
    """City and category discovery reads saved or manual pages. It does not fetch a map catalog."""
    if source_mode in {"fixture", "manual"}:
        return
    if platform == "2gis":
        raise PermissionError("2GIS catalog extraction is not permitted")
    raise PermissionError("live requests are disabled")


def unseen_must_be_null(seen: bool, value: object) -> None:
    """AGENTS.md 5.1: a field that was not on the page is null, never 0 and never copied from another platform."""
    if not seen and value is not None:
        raise ValueError("a field that was not seen must be null")


def kzt(amount: int) -> str:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("amount must be an integer number of tenge")
    if amount < 0:
        raise ValueError("amount must not be negative")
    return f"{amount:,}".replace(",", " ") + " ₸"


def client_supplied_fact(supplied: bool, value: object) -> None:
    """An address, contact, or service is stored only when the client supplied that value."""
    if value is not None and not supplied:
        raise ValueError("address, contact, and service values must be supplied by the client")


def require_verified_owner(platform: str, access_status: str) -> None:
    """Listing changes wait until the owner authorization for that platform is verified."""
    if access_status != "verified":
        raise PermissionError(f"{platform} requires verified owner authorization before a listing change")


BLOCKED_BROWSER_ACTIONS = frozenset(
    {
        "type_password",
        "solve_captcha",
        "read_sms",
        "bypass_login",
        "rotate_ip",
        "stealth",
        "submit_listing_form",
    }
)

GENERATED_CONTENT_KINDS = frozenset({"cover", "promo_banner", "price_image", "video"})
BANNED_MEDIA_KINDS = frozenset({"photo_work", "interior", "staff", "premises", "masters"})
FABRICATED_SCENE_KINDS = BANNED_MEDIA_KINDS | frozenset(
    {
        "product",
        "products",
        "employee",
        "employees",
        "person",
        "people",
        "work",
        "completed_work",
        "client",
        "clients",
    }
)


def refuse_blocked_browser_action(action: str) -> None:
    """Owner login, checks, and form submission stay with a person."""
    if action in BLOCKED_BROWSER_ACTIONS:
        raise PermissionError(f"{action} is not allowed")


def refuse_listing_submit(submit: bool) -> None:
    """Prepare and dry-run do not send the form."""
    if submit:
        refuse_blocked_browser_action("submit_listing_form")


def refuse_live_listing_submit(mode: str) -> None:
    """Submission in this batch is an offline simulation. A live cabinet is not sent."""
    if mode != "offline":
        raise PermissionError("live listing submission is closed")


def refuse_unapproved_fact(field: str, approved: bool) -> None:
    """A listing value is written only from an approved client fact."""
    if not approved:
        raise PermissionError(f"{field} is not approved")


def listing_media_allowed(kind: str, *, status: str | None = None, illustrative: bool | None = None) -> None:
    """Real premises photos come from the client. Generated covers must be approved and labeled."""
    if kind in BANNED_MEDIA_KINDS:
        raise PermissionError("generated photos of work, interior, or staff are not published")
    if kind in GENERATED_CONTENT_KINDS and (status != "checked" or illustrative is not True):
        raise PermissionError("generated content must be approved and labeled illustrative")


def refuse_fabricated_scene(kind: str) -> None:
    """Illustrations stay abstract. Premises, staff, products, and finished work are not generated."""
    if kind in FABRICATED_SCENE_KINDS:
        raise PermissionError("generated photos of premises, staff, products, or completed work are not allowed")
