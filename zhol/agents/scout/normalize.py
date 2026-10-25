from __future__ import annotations

import re

# Fillers from prompts/scout-match.md, plus the Latin forms of the Cyrillic words.
_FILLERS = frozenset({"салон", "студия", "beauty", "studio", "salon", "studiya"})
_CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ә": "a",
    "ғ": "g",
    "қ": "k",
    "ң": "n",
    "ө": "o",
    "ұ": "u",
    "ү": "u",
    "һ": "h",
    "і": "i",
}
_STREET = (
    (re.compile(r"\bулица\b"), "ул"),
    (re.compile(r"\bул\b"), "ул"),
    (re.compile(r"\bпроспект\b"), "просп"),
    (re.compile(r"\bпросп\b"), "просп"),
    (re.compile(r"\bпереулок\b"), "пер"),
    (re.compile(r"\bпер\b"), "пер"),
    (re.compile(r"\bмикрорайон\b"), "мкр"),
    (re.compile(r"\bмкр\b"), "мкр"),
)


def _letters(value: str) -> str:
    text = value.lower().replace("ё", "е")
    text = text.replace("пр-т", "просп")
    return re.sub(r"[«»\"'“”„]", " ", text)


def _transliterate(token: str) -> str:
    return "".join(_CYRILLIC.get(char, char) for char in token)


def name_tokens(value: str | None) -> frozenset[str]:
    if not value or not value.strip():
        return frozenset()
    text = re.sub(r"[^a-zа-яәғқңөұүһі0-9]+", " ", _letters(value))
    tokens = []
    for token in text.split():
        folded = _transliterate(token)
        if folded and folded not in _FILLERS:
            tokens.append(folded)
    return frozenset(tokens)


def names_compatible(left: str | None, right: str | None) -> bool:
    tokens_left, tokens_right = name_tokens(left), name_tokens(right)
    return bool(tokens_left) and tokens_left == tokens_right


def name_key(value: str | None) -> str | None:
    """Stable lookup key. Equal keys are exactly the names `names_compatible` accepts."""
    tokens = name_tokens(value)
    if not tokens:
        return None
    return " ".join(sorted(tokens))


def normalize_address(value: str) -> str:
    text = _letters(value).replace(".", " ")
    for pattern, replacement in _STREET:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


_CITIES = frozenset(
    {
        "алматы",
        "астана",
        "шымкент",
        "караганда",
        "актобе",
        "актау",
        "атырау",
        "тараз",
        "павлодар",
        "семей",
        "усть каменогорск",
        "костанай",
        "кызылорда",
        "уральск",
        "петропавловск",
        "туркестан",
    }
)


def _without_floor(value: str) -> str:
    text = re.sub(r"(\d+)\s*этаж[а-я]*\s*\d+", r"\1", value, flags=re.IGNORECASE)
    text = re.sub(r"\s+\d+\s*этаж[а-я]*", " ", text, flags=re.IGNORECASE)
    return re.sub(r"этаж[а-я]*\s*\d+", " ", text, flags=re.IGNORECASE)


def address_parts(value: str | None) -> tuple[str, str] | None:
    if not value or not value.strip():
        return None
    tokens = normalize_address(_without_floor(value)).split()
    house_at = None
    for index, token in enumerate(tokens):
        if any(char.isdigit() for char in token):
            house_at = index
    if house_at is None:
        return None
    street = " ".join(token for index, token in enumerate(tokens) if index != house_at)
    if not street:
        return None
    return street, tokens[house_at]


def addresses_same(left: str | None, right: str | None) -> bool:
    parts_left, parts_right = address_parts(left), address_parts(right)
    return parts_left is not None and parts_left == parts_right


def addresses_compatible(left: str | None, right: str | None) -> bool:
    """Same street and house. Extra words such as a mall name do not make a second branch."""
    if addresses_same(left, right):
        return True
    parts_left, parts_right = address_parts(left), address_parts(right)
    if parts_left is None or parts_right is None or parts_left[1] != parts_right[1]:
        return False
    left_tokens = {token for token in parts_left[0].split() if token not in _CITIES}
    right_tokens = {token for token in parts_right[0].split() if token not in _CITIES}
    if not left_tokens or not right_tokens:
        return False
    return left_tokens <= right_tokens or right_tokens <= left_tokens


def addresses_conflict(left: str | None, right: str | None) -> bool:
    parts_left, parts_right = address_parts(left), address_parts(right)
    return parts_left is not None and parts_right is not None and parts_left != parts_right and not addresses_compatible(left, right)


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) < 11:
        return None
    return digits


def phone_parts(value: str | None) -> tuple[str | None, str | None]:
    """Canonical digits plus the original display string. Blank input stays empty."""
    shown = " ".join(str(value or "").split())
    if not shown:
        return None, None
    return normalize_phone(shown), shown


def phone_set(values: list[str] | None) -> set[str]:
    found = set()
    for value in values or []:
        number = normalize_phone(value)
        if number:
            found.add(number)
    return found


def phones_overlap(left: list[str] | None, right: list[str] | None) -> bool:
    return bool(phone_set(left) & phone_set(right))


def normalize_platform_id(platform: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if platform == "2gis":
        match = re.search(r"(\d{8,})", text)
        return match.group(1) if match else text
    if platform == "yandex":
        match = re.search(r"/org/[^/]+/(\d+)", text)
        if match:
            return match.group(1)
        return text
    return text
