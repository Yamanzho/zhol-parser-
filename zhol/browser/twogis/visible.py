from __future__ import annotations

import re
from typing import Any

_STREET = re.compile(
    r"(улиц|\bул\.?\b|проспект|просп|\bпр-т\b|переул|\bпер\.?\b|микрорайон|\bмкр\b)",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?:\+7|8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_RATING_LINE = re.compile(r"[0-5](?:\.\d)?")
_MONTHS = {
    "января": "01",
    "февраля": "02",
    "марта": "03",
    "апреля": "04",
    "мая": "05",
    "июня": "06",
    "июля": "07",
    "августа": "08",
    "сентября": "09",
    "октября": "10",
    "ноября": "11",
    "декабря": "12",
}
_DATE = re.compile(
    r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})",
    re.IGNORECASE,
)
_PRICE_AMOUNT = re.compile(r"\d[\d\s\u00a0]*\s*(?:₸|тенге\b)", re.IGNORECASE)
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_URL = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>]*)?", re.IGNORECASE)
_PLACE = re.compile(
    r"(?P<district>[A-Za-zА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ\- ]+район)\s*,\s*"
    r"(?P<city>[A-Za-zА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ\- ]+)\s*,\s*\d{6}",
    re.IGNORECASE,
)
_HOUR_LINE = re.compile(
    r"^(?:ежедневно|круглосуточно|пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\b.+\d{1,2}:\d{2}",
    re.IGNORECASE,
)
_SKIP_HOSTS = ("2gis.", "instagram.", "wa.me", "whatsapp.", "yandex.", "google.", "g.page", "goo.gl")
_CHROME = frozenset(
    {
        "поиск",
        "маршруты",
        "друзья",
        "гид",
        "жильё",
        "жилье",
        "сохранить",
        "отправить",
        "маршрут",
        "контакты",
        "инфо",
        "отзывы",
        "цены",
        "фото",
        "реклама",
        "открыто",
        "закрыто",
        "записаться",
        "записаться онлайн",
        "instagram",
        "whatsapp",
        "показать вход",
        "показать телефон",
        "нашли ошибку в данных?",
    }
)


def _fold(value: str) -> str:
    return " ".join(value.split()).casefold()


def _visible_line(line: str) -> str:
    return " ".join(_INVISIBLE.sub(" ", line).split())


def _company_text(text: str) -> str:
    """Drop the similar-companies block so an advert does not become this firm's phone."""
    folded = text.casefold()
    cut = folded.find("похожие организации")
    if cut == -1:
        return text
    return text[:cut]


def _chrome(line: str) -> bool:
    return _fold(line) in _CHROME


def _metric(line: str) -> bool:
    token = line.strip().replace(",", ".")
    if _RATING_LINE.fullmatch(token):
        return True
    return re.search(r"\d[\d\s\u00a0]*\s+(?:оцен|отзыв|фото|филиал)", line, flags=re.IGNORECASE) is not None


def _price_only(line: str) -> bool:
    match = _PRICE_AMOUNT.search(line)
    if match is None:
        return False
    rest = (line[: match.start()] + line[match.end() :]).strip(" -–—:.")
    return rest == ""


def _add_service(names: list[str], label: str, blocked: str) -> None:
    text = " ".join(label.split()).strip(" -–—:")
    if not text or _fold(text) == blocked or text in names or _chrome(text) or _metric(text):
        return
    names.append(text)


def service_names(prices_text: str | None, company_name: str | None) -> tuple[list[str], str]:
    """Names that sit next to a price. The company name is never a service."""
    if not prices_text or not _PRICE_AMOUNT.search(prices_text):
        return [], "not_shown"
    blocked = _fold(company_name) if company_name else ""
    names: list[str] = []
    lines = [line.strip() for line in prices_text.splitlines() if line.strip()]
    index = 0
    while index < len(lines):
        line = lines[index]
        amount = _PRICE_AMOUNT.search(line)
        if amount and amount.start() > 0:
            _add_service(names, line[: amount.start()], blocked)
            index += 1
            continue
        if _price_only(line):
            index += 1
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if nxt and _price_only(nxt):
            _add_service(names, line, blocked)
            index += 2
            continue
        index += 1
    if not names:
        return [], "not_shown"
    return names, "shown"


def _rubric_line(text: str, name: str | None) -> tuple[str | None, str]:
    if not name or not name.strip():
        return None, "not_shown"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    wanted = _fold(name)
    index = next((i for i, line in enumerate(lines) if _fold(line) == wanted), None)
    if index is None:
        return None, "not_shown"
    for line in lines[index + 1 : index + 4]:
        if _chrome(line):
            continue
        if _metric(line) or _PHONE.search(line) or _STREET.search(line) or _PLACE.search(line):
            return None, "not_shown"
        return line, "candidate"
    return None, "not_shown"


def _split_labels(line: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*[·•,/]\s*", line) if part.strip()]


def _accept_category(line: str | None, status: str) -> tuple[str | None, str]:
    if line is None:
        return None, status
    from zhol.export.mailing import classify, load_categories

    table = load_categories()
    labels = _split_labels(line)
    folded = [_fold(label) for label in labels]
    if any(key in table.ambiguous for key in folded):
        return None, "ambiguous"
    sector, _category, _subcategory = classify(labels, table)
    if sector is not None or len(labels) == 1:
        return labels[0], "shown"
    if any(key not in table.exact for key in folded):
        return None, "unknown_label"
    return None, "ambiguous"


def _hours(text: str) -> tuple[str | None, str]:
    found = []
    for line in _company_text(text).splitlines():
        stripped = _visible_line(line)
        if _HOUR_LINE.match(stripped):
            found.append(stripped)
    unique = list(dict.fromkeys(found))
    daily = [line for line in unique if line.casefold().startswith(("ежедневно", "круглосуточно"))]
    if len(daily) > 1:
        return None, "ambiguous"
    if not unique:
        return None, "not_shown"
    return "; ".join(unique), "shown"


def _place(text: str) -> tuple[dict[str, str], dict[str, str]]:
    found = []
    for match in _PLACE.finditer(text):
        found.append((" ".join(match.group("district").split()), " ".join(match.group("city").split())))
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        district, city = unique[0]
        return {"district": district, "city": city}, {"district": "shown", "city": "shown"}
    if len(unique) > 1:
        return {}, {"district": "ambiguous", "city": "ambiguous"}
    return {}, {"district": "not_shown", "city": "not_shown"}


def _email(text: str) -> tuple[str | None, str]:
    found = list(dict.fromkeys(match.group(0) for match in _EMAIL.finditer(_company_text(text))))
    if len(found) == 1:
        return found[0], "shown"
    if len(found) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _description(info_text: str | None, name: str | None) -> tuple[str | None, str]:
    if info_text is None:
        return None, "not_shown"
    kept = []
    for line in info_text.splitlines():
        stripped = line.strip()
        if len(stripped) < 40 or _chrome(stripped):
            continue
        folded = stripped.casefold()
        if "реклама" in folded or "скидка" in folded:
            continue
        if name and _fold(stripped) == _fold(name):
            continue
        if _STREET.search(stripped) or _PHONE.search(stripped) or _HOUR_LINE.match(stripped) or _PLACE.search(stripped):
            continue
        kept.append(stripped)
    unique = list(dict.fromkeys(kept))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _text_link(text: str) -> tuple[str | None, str]:
    found: list[str] = []
    own = _company_text(text)
    for match in _URL.finditer(own):
        if match.start() > 0 and own[match.start() - 1] == "@":
            continue
        raw = match.group(0).rstrip(".,)")
        lowered = raw.lower()
        host = lowered.split("//")[-1].split("/")[0]
        if any(part in host or part in lowered for part in _SKIP_HOSTS):
            continue
        if raw not in found:
            found.append(raw)
    if len(found) == 1:
        value = found[0]
        if not value.lower().startswith("http"):
            value = "https://" + value
        return value, "shown"
    if len(found) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _one_number(pattern: str | list[str], text: str) -> tuple[str | None, str]:
    found: list[str] = []
    patterns = pattern if isinstance(pattern, list) else [pattern]
    for item in patterns:
        for match in re.finditer(item, text, flags=re.IGNORECASE):
            digits = re.sub(r"[\s\u00a0]", "", match.group("value"))
            if digits.isdigit():
                found.append(str(int(digits)))
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _rating(text: str) -> tuple[str | None, str]:
    decimals: list[str] = []
    integers: list[str] = []
    for line in text.splitlines():
        token = _visible_line(line).replace(",", ".")
        if not _RATING_LINE.fullmatch(token):
            continue
        if "." in token:
            decimals.append(token)
        else:
            integers.append(token)
    decimals = list(dict.fromkeys(decimals))
    integers = list(dict.fromkeys(integers))
    if len(decimals) == 1:
        return decimals[0], "shown"
    if len(decimals) > 1:
        return None, "ambiguous"
    if len(integers) == 1:
        return integers[0], "shown"
    if len(integers) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _address(text: str) -> tuple[str | None, str]:
    lines = [_visible_line(line) for line in text.splitlines() if _STREET.search(_visible_line(line))]
    unique = list(dict.fromkeys(lines))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _phones(text: str) -> tuple[str | None, str]:
    found = [re.sub(r"\s+", " ", match.group(0)).strip() for match in _PHONE.finditer(_company_text(text))]
    unique = list(dict.fromkeys(found))
    if not unique:
        return None, "not_shown"
    return ", ".join(unique), "shown"


def _label(text: str, labels: list[str]) -> bool:
    folded = text.lower()
    return any(label.lower() in folded for label in labels)


def _dates(text: str) -> list[str]:
    found = []
    for day, month, year in _DATE.findall(text):
        found.append(f"{int(year):04d}-{_MONTHS[month.lower()]}-{int(day):02d}")
    return list(dict.fromkeys(found))


def _prices(text: str | None, phrases: dict[str, list[str]]) -> tuple[str | None, str]:
    if text is None:
        return None, "not_shown"
    folded = text.lower()
    if any(phrase.lower() in folded for phrase in phrases.get("empty") or []):
        return "empty", "shown"
    if _PRICE_AMOUNT.search(text):
        return "company_price", "shown"
    if any(phrase.lower() in folded for phrase in phrases.get("photo_only") or []):
        return "photo_only", "shown"
    return None, "not_shown"


def _external_links(hrefs: list[str]) -> dict[str, str]:
    sites: list[str] = []
    whatsapp = ""
    instagram = ""
    for href in hrefs:
        lower = href.lower()
        if "wa.me/" in lower or "whatsapp.com" in lower:
            whatsapp = whatsapp or href
            continue
        if "instagram.com/" in lower:
            handle = href.rstrip("/").split("/")[-1]
            if handle and handle not in ("instagram.com", "www.instagram.com"):
                instagram = instagram or handle
            continue
        if lower.startswith("http") and "2gis." not in lower:
            sites.append(href)
    found: dict[str, str] = {}
    if len(sites) == 1:
        found["link"] = sites[0]
    if whatsapp:
        found["whatsapp"] = whatsapp
    if instagram:
        found["instagram"] = instagram
    return found


def parse_visible(
    text: str,
    *,
    name: str | None,
    hrefs: list[str],
    patterns: dict[str, str],
    labels: dict[str, list[str]],
    prices_text: str | None,
    price_phrases: dict[str, list[str]],
    reviews_text: str | None,
    info_text: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    text = _company_text(text)
    fields: dict[str, str] = {}
    notes: dict[str, str] = {}
    if name and name.strip():
        fields["name"] = name.strip()
        notes["name"] = "shown"
    else:
        notes["name"] = "not_shown"
    address, why = _address(text)
    notes["address"] = why
    if address:
        fields["address"] = address
    rating, why = _rating(text)
    notes["rating"] = why
    if rating:
        fields["rating"] = rating
    for key in ("ratings_count", "reviews_count", "photos_count", "branches"):
        pattern = patterns.get(key)
        if not pattern:
            notes[key] = "not_shown"
            continue
        value, why = _one_number(pattern, text)
        notes[key] = why
        if value is not None:
            fields[key] = value
    phones, why = _phones(text)
    notes["phones"] = why
    notes["has_phone"] = why
    if phones:
        fields["phones"] = phones
        fields["has_phone"] = "1"
    for key in ("is_ad", "has_whatsapp", "has_instagram", "online_booking"):
        if _label(text, list(labels.get(key) or [])):
            fields[key] = "1"
            notes[key] = "shown"
        else:
            notes[key] = "not_shown"
    links = _external_links(hrefs)
    for key, value in links.items():
        fields[key] = value
        notes[key] = "shown"
    for key in ("link", "whatsapp", "instagram"):
        notes.setdefault(key, "not_shown")
    price, why = _prices(prices_text, price_phrases)
    notes["prices"] = why
    if price:
        fields["prices"] = price
    if reviews_text is None:
        notes["last_review_date"] = "not_shown"
        notes["reviews_shown"] = "not_shown"
        notes["replies_count"] = "not_shown"
    else:
        dates = _dates(reviews_text)
        if len(dates) == 1:
            fields["last_review_date"] = dates[0]
            notes["last_review_date"] = "shown"
        elif len(dates) > 1:
            notes["last_review_date"] = "ambiguous"
        else:
            notes["last_review_date"] = "not_shown"
        notes["reviews_shown"] = "not_shown"
        notes["replies_count"] = "not_shown"
    rubric, rubric_status = _rubric_line(text, name)
    category, category_status = _accept_category(rubric, rubric_status)
    notes["category"] = category_status
    if category:
        fields["category"] = category
    place_fields, place_notes = _place(text)
    fields.update(place_fields)
    notes.update(place_notes)
    email, why = _email(text)
    notes["email"] = why
    if email:
        fields["email"] = email
    hours, why = _hours(text)
    notes["hours"] = why
    if hours:
        fields["hours"] = hours
    description, why = _description(info_text, name)
    notes["description"] = why
    if description:
        fields["description"] = description
    names, why = service_names(prices_text, name)
    notes["services"] = why
    if names:
        notes["service_names"] = "; ".join(names)
    if "link" not in fields:
        link, why = _text_link(text)
        notes["link"] = why
        if link:
            fields["link"] = link
    return fields, notes


def live_spec(cfg: dict[str, Any]) -> dict[str, Any]:
    spec = (cfg.get("platforms") or {}).get("2gis", {}).get("live")
    if not isinstance(spec, dict):
        raise ValueError("config/platforms.yaml is missing platforms.2gis.live")
    return spec
