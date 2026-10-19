"""Read a public Yandex or Google card from visible text. Unshown values stay empty."""

from __future__ import annotations

import re

from zhol.browser.twogis.visible import _PHONE, _STREET, _fold, _visible_line

_GAP = r"[ \t\u00a0]+"
_COUNTS = {
    "ratings_count": re.compile(rf"(?P<value>\d[\d \u00a0]*){_GAP}оцен", re.IGNORECASE),
    "reviews_count": re.compile(rf"(?P<value>\d[\d \u00a0]*){_GAP}отзыв", re.IGNORECASE),
    "photos_count": re.compile(rf"(?P<value>\d[\d \u00a0]*){_GAP}фото\b", re.IGNORECASE),
    "news_count": re.compile(rf"(?P<value>\d[\d \u00a0]*){_GAP}новост", re.IGNORECASE),
}
_TAB_COUNTS = {
    "ratings_count": re.compile(r"оценк[а-я]*", re.IGNORECASE),
    "reviews_count": re.compile(r"отзыв[а-я]*", re.IGNORECASE),
    "photos_count": re.compile(r"фото", re.IGNORECASE),
}
_HOUR_LINE = re.compile(
    r"^(?:ежедневно|круглосуточно|пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\b.+\d{1,2}:\d{2}",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_CITY = re.compile(
    r"\b(Алматы|Астана|Шымкент|Караганда|Актобе|Актау|Атырау|Тараз|Павлодар|Семей|"
    r"Усть-Каменогорск|Костанай|Кызылорда|Уральск|Петропавловск|Туркестан)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SKIP_HOSTS = ("instagram.", "wa.me", "whatsapp.", "yandex.", "google.", "g.page", "goo.gl", "2gis.")
_SOCIAL_HOSTS = ("facebook.", "fb.com", "t.me", "telegram.", "vk.com", "vk.ru", "ok.ru", "tiktok.", "youtube.")
_ASSET_HOSTS = ("yastatic.", "adfox.", "gstatic.", "googleusercontent.")
_RESERVED_HANDLES = frozenset({"p", "reel", "reels", "explore", "stories", "accounts", "instagram.com", "www.instagram.com"})
_CUTS = ("похожие организации", "подобные места", "people also search", "рядом с этим местом")
_CHROME = frozenset(
    {
        "маршрут",
        "маршруты",
        "как добраться",
        "обзор",
        "отзывы",
        "фото",
        "меню",
        "услуги",
        "цены",
        "о месте",
        "описание",
        "сохранить",
        "поделиться",
        "позвонить",
        "сайт",
        "контакты",
        "поиск",
        "закрыто",
        "открыто",
    }
)
_SERVICE_HEADS = frozenset({"услуги", "меню", "прайс", "товары и услуги"})
_ABOUT_HEADS = frozenset({"описание", "о компании", "о салоне"})
_BAD_NAMES = frozenset({"google maps", "карты", "яндекс карты", "yandex maps", "результаты"})


def _block(text: str) -> str:
    folded = text.casefold()
    cut = len(text)
    for marker in _CUTS:
        at = folded.find(marker)
        if at != -1:
            cut = min(cut, at)
    return text[:cut]


def _lines(text: str) -> list[str]:
    return [_visible_line(line) for line in _block(text).splitlines() if _visible_line(line)]


def _tab_count(lines: list[str], label: re.Pattern[str]) -> list[str]:
    found = []
    for index, line in enumerate(lines[:-1]):
        if not label.fullmatch(line):
            continue
        nxt = lines[index + 1].replace("\u00a0", "").replace(" ", "")
        if nxt.isdigit():
            found.append(str(int(nxt)))
    return found


def _one(pattern: re.Pattern[str], text: str) -> tuple[str | None, str]:
    found = []
    for match in pattern.finditer(text):
        digits = re.sub(r"[\s\u00a0]", "", match.group("value"))
        if digits.isdigit():
            found.append(str(int(digits)))
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _metric(line: str) -> bool:
    token = line.replace(",", ".")
    if re.fullmatch(r"[0-5]\.\d", token):
        return True
    if re.fullmatch(r"\(\d[\d \u00a0]*\)", line):
        return True
    patterns = []
    for pattern in _COUNTS.values():
        patterns.extend(pattern if isinstance(pattern, tuple) else (pattern,))
    return any(pattern.search(line) for pattern in patterns)


def _rating(lines: list[str]) -> tuple[str | None, str]:
    found = []
    for line in lines:
        token = line.replace(",", ".")
        if re.fullmatch(r"[0-5]\.\d", token):
            found.append(token)
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _paren_reviews(lines: list[str]) -> tuple[str | None, str]:
    rating_at = next((index for index, line in enumerate(lines) if re.fullmatch(r"[0-5]\.\d", line.replace(",", "."))), None)
    if rating_at is None:
        return None, "not_shown"
    window = lines[rating_at : rating_at + 2]
    found = []
    for line in window:
        for match in re.finditer(r"\((\d[\d \u00a0]*)\)", line):
            found.append(str(int(re.sub(r"[\s\u00a0]", "", match.group(1)))))
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _phones(text: str) -> tuple[str | None, str]:
    found = [" ".join(match.group(0).split()) for match in _PHONE.finditer(_block(text))]
    unique = list(dict.fromkeys(found))
    if not unique:
        return None, "not_shown"
    return ", ".join(unique), "shown"


def _labeled_phone(text: str, label: str) -> tuple[str | None, str]:
    found = []
    for line in _lines(text):
        if label not in line.casefold():
            continue
        found.extend(" ".join(match.group(0).split()) for match in _PHONE.finditer(line))
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _href_match(hrefs: list[str], needles: tuple[str, ...]) -> list[str]:
    found = []
    for href in hrefs:
        lower = href.lower()
        if any(needle in lower for needle in needles) and href not in found:
            found.append(href)
    return found


def _number_from_href(href: str) -> str | None:
    match = _PHONE.search(href.replace("%20", " "))
    if match:
        return " ".join(match.group(0).split())
    digits = re.sub(r"\D", "", href.split("?")[0].rstrip("/").split("/")[-1])
    if len(digits) in (10, 11):
        return "+" + digits if digits.startswith("7") else digits
    return None


def _whatsapp(text: str, hrefs: list[str]) -> tuple[str | None, str]:
    found: list[str] = []
    labeled, why = _labeled_phone(text, "whatsapp")
    if labeled:
        found.append(labeled)
    elif why == "ambiguous":
        return None, "ambiguous"
    for href in _href_match(hrefs, ("wa.me/", "whatsapp.")):
        found.append(_number_from_href(href) or href)
    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        key = re.sub(r"\D", "", item) or item
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _instagram(text: str, hrefs: list[str]) -> tuple[str | None, str]:
    found = []
    for href in _href_match(hrefs, ("instagram.com/",)):
        handle = href.split("?")[0].rstrip("/").split("/")[-1]
        if re.fullmatch(r"[A-Za-z0-9._]{2,30}", handle) and handle.lower() not in _RESERVED_HANDLES:
            found.append(handle)
    for line in _lines(text):
        if "instagram" not in line.casefold():
            continue
        for match in re.finditer(r"@([A-Za-z0-9._]{2,})", line):
            found.append(match.group(1))
    unique = list(dict.fromkeys(item.casefold() for item in found))
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _email(text: str) -> tuple[str | None, str]:
    found = list(dict.fromkeys(match.group(0) for match in _EMAIL.finditer(_block(text))))
    if len(found) == 1:
        return found[0], "shown"
    if len(found) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _clean_address(line: str) -> str:
    text = re.sub(r"(\d+)\s*этаж[а-я]*\s*\d+", r"\1", line, flags=re.IGNORECASE)
    text = re.sub(r"\s+\d+\s*этаж[а-я]*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"этаж[а-я]*\s*\d+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _address(lines: list[str]) -> tuple[str | None, str]:
    labeled: list[str] = []
    for index, line in enumerate(lines):
        if _fold(line) != "адрес":
            continue
        for follow in lines[index + 1 : index + 3]:
            if not _STREET.search(follow):
                continue
            cleaned = _clean_address(follow)
            if cleaned and cleaned not in labeled:
                labeled.append(cleaned)
            break
    if len(labeled) == 1:
        return labeled[0], "shown"
    if len(labeled) > 1:
        return None, "ambiguous"
    stop = next((index for index, line in enumerate(lines) if _fold(line) in {"как добраться", "маршрут", "маршруты"}), len(lines))
    found: list[str] = []
    for line in lines[:stop]:
        if not _STREET.search(line):
            continue
        cleaned = _clean_address(line)
        if cleaned and cleaned not in found:
            found.append(cleaned)
    unique = found
    if len(unique) == 1:
        return unique[0], "shown"
    if len(unique) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _city(text: str) -> tuple[str | None, str]:
    found = list(dict.fromkeys(match.group(1) for match in _CITY.finditer(_block(text))))
    folded = {item.casefold(): item for item in found}
    if len(folded) == 1:
        return next(iter(folded.values())), "shown"
    if len(folded) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _hours(lines: list[str]) -> tuple[str | None, str]:
    found = [line for line in lines if _HOUR_LINE.match(line)]
    unique = list(dict.fromkeys(found))
    daily = [line for line in unique if line.casefold().startswith(("ежедневно", "круглосуточно"))]
    if len(daily) > 1:
        return None, "ambiguous"
    if not unique:
        return None, "not_shown"
    return "; ".join(unique), "shown"


def _section(lines: list[str], heads: frozenset[str]) -> list[str]:
    found: list[str] = []
    for index, line in enumerate(lines):
        if _fold(line) not in heads:
            continue
        body = []
        for nxt in lines[index + 1 :]:
            if _fold(nxt) in _CHROME or _fold(nxt) in heads or _fold(nxt) in _ABOUT_HEADS:
                break
            body.append(nxt)
        if any(_is_price(item) for item in body):
            return body
        if body and not found:
            found = body
    return found


def _description(lines: list[str], name: str | None) -> tuple[str | None, str]:
    body = _section(lines, _ABOUT_HEADS)
    text = " ".join(body).strip()
    if not text:
        return None, "not_shown"
    if name and _fold(text) == _fold(name):
        return None, "not_shown"
    return text, "shown"


def _is_price(line: str) -> bool:
    return bool(re.search(r"\d[\d\s\u00a0]*\s*(?:₸|тенге\b|тнг\b)", line, re.IGNORECASE))


def _services(lines: list[str], name: str | None) -> tuple[list[str], str]:
    body = _section(lines, _SERVICE_HEADS)
    if not body:
        return [], "not_shown"
    names: list[str] = []
    blocked = _fold(name or "")
    for index, line in enumerate(body):
        if not _is_price(line) or index == 0:
            continue
        title = body[index - 1]
        if "." in title or len(title) > 90:
            title = body[index - 2] if index >= 2 else ""
        title = title.strip(" -")
        if not title or _is_price(title) or _fold(title) == blocked or _fold(title) in _CHROME:
            continue
        if "посмотреть все" in _fold(title) or title not in names:
            if "посмотреть все" in _fold(title):
                continue
            names.append(title)
    if not names:
        return [], "not_shown"
    return names, "shown"


_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.IGNORECASE)


def _website(text: str, hrefs: list[str]) -> tuple[str | None, str]:
    found: list[str] = []
    for href in hrefs:
        lower = href.lower()
        if not lower.startswith("http"):
            continue
        if any(host in lower for host in (*_SKIP_HOSTS, *_SOCIAL_HOSTS, *_ASSET_HOSTS)):
            continue
        if href not in found:
            found.append(href)
    if not found:
        for match in _URL.finditer(_block(text)):
            raw = match.group(0).rstrip(".,)")
            lower = raw.lower()
            if any(host in lower for host in _SKIP_HOSTS):
                continue
            if raw not in found:
                found.append(raw)
    if not found:
        for line in _lines(text):
            token = line.strip().rstrip(".")
            if not _DOMAIN.fullmatch(token) or any(host in token.lower() for host in _SKIP_HOSTS):
                continue
            if token not in found:
                found.append(token)
    if len(found) == 1:
        return found[0], "shown"
    if len(found) > 1:
        return None, "ambiguous"
    return None, "not_shown"


def _category(lines: list[str], name: str | None) -> tuple[str | None, str]:
    if not name or not name.strip() or _fold(name) in _BAD_NAMES:
        return None, "not_shown"
    wanted = _fold(name)
    index = next((i for i, line in enumerate(lines) if _fold(line) == wanted), None)
    if index is None:
        return None, "not_shown"
    from zhol.export.mailing import classify, load_categories

    table = load_categories()
    for line in lines[index + 1 : index + 6]:
        if _fold(line) in _CHROME or _metric(line) or _PHONE.search(line) or _STREET.search(line):
            continue
        labels = [part.strip() for part in re.split(r"\s*[·•,/]\s*", line) if part.strip()]
        folded = [_fold(label) for label in labels]
        if any(key in table.ambiguous for key in folded):
            return None, "ambiguous"
        sector, _category_name, _sub = classify(labels, table)
        if sector is not None or len(labels) == 1:
            return labels[0], "shown"
        if any(key not in table.exact for key in folded):
            return None, "unknown_label"
        return None, "ambiguous"
    return None, "not_shown"


def place_id(platform: str, url: str | None) -> str | None:
    if not url:
        return None
    if platform == "yandex":
        match = re.search(r"/org/[^/?#]+/(\d+)", url)
        return match.group(1) if match else None
    feature = re.search(r"1s(ChIJ[A-Za-z0-9_\-]+)", url)
    if feature:
        return feature.group(1)
    hex_id = re.search(r"(0x[0-9a-f]+:0x[0-9a-f]+)", url, re.IGNORECASE)
    if hex_id:
        return hex_id.group(1)
    query = re.search(r"[?&]place_id=(ChIJ[A-Za-z0-9_\-]+)", url)
    return query.group(1) if query else None


def parse_card(
    text: str,
    *,
    name: str | None,
    hrefs: list[str],
    platform: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return only values that are printed once. platform selects which keys are kept."""
    lines = _lines(text)
    fields: dict[str, str] = {}
    notes: dict[str, str] = {}
    if name and name.strip() and _fold(name) not in _BAD_NAMES:
        fields["name"] = name.strip()
        notes["name"] = "shown"
    else:
        notes["name"] = "not_shown"
    address, why = _address(lines)
    notes["address"] = why
    if address:
        fields["address"] = address
    city, why = _city(text)
    notes["city"] = why
    if city:
        fields["city"] = city
    rating, why = _rating(lines)
    notes["rating"] = why
    if rating:
        fields["rating"] = rating
    for key, pattern in _COUNTS.items():
        if platform == "google" and key in ("ratings_count", "news_count"):
            continue
        value, why = _one(pattern, _block(text))
        extra = _tab_count(lines, _TAB_COUNTS[key]) if key in _TAB_COUNTS else []
        if value is None and why != "ambiguous" and extra:
            unique = list(dict.fromkeys(extra))
            value, why = (unique[0], "shown") if len(unique) == 1 else (None, "ambiguous")
        elif value is not None and extra and any(item != value for item in extra):
            value, why = None, "ambiguous"
        notes[key] = why
        if value is not None:
            fields[key] = value
    if "reviews_count" not in fields and notes.get("reviews_count") != "ambiguous":
        paren, why = _paren_reviews(lines)
        if paren is not None:
            fields["reviews_count"] = paren
            notes["reviews_count"] = why
        elif why == "ambiguous":
            notes["reviews_count"] = why
    if "reviews_count" not in fields and notes.get("reviews_count") == "not_shown":
        empty = [line for line in lines if re.fullmatch(r"(?:ещё|еще)\s+нет отзывов|нет отзывов", line, re.IGNORECASE)]
        if len(empty) == 1:
            fields["reviews_count"] = "0"
            notes["reviews_count"] = "shown"
    phones, why = _phones(text)
    notes["phones"] = why
    if phones:
        fields["phones"] = phones
    whatsapp, why = _whatsapp(text, hrefs)
    notes["whatsapp"] = why
    if whatsapp:
        fields["whatsapp"] = whatsapp
    instagram, why = _instagram(text, hrefs)
    notes["instagram"] = why
    if instagram:
        fields["instagram"] = instagram
    email, why = _email(text)
    notes["email"] = why
    if email:
        fields["email"] = email
    website, why = _website(text, hrefs)
    notes["website"] = why
    if website:
        if platform == "google":
            fields["website"] = website
        elif platform == "yandex":
            fields["website"] = website
    hours, why = _hours(lines)
    notes["hours"] = why
    if hours:
        fields["hours"] = hours
    description, why = _description(lines, name)
    notes["description"] = why
    if description:
        fields["description"] = description
    category, why = _category(lines, fields.get("name"))
    notes["category"] = why
    if category:
        fields["category"] = category
    names, why = _services(lines, fields.get("name"))
    notes["services"] = why
    if names:
        notes["service_names"] = "; ".join(names)
    notes.setdefault("reviews_count", "not_shown")
    notes.setdefault("photos_count", "not_shown")
    return fields, notes


def candidates_from_links(platform: str, links: list[tuple[str, str]], *, limit: int = 5) -> list[dict]:
    """Build search candidates from anchor href and the text inside that anchor."""
    found = []
    seen: set[str] = set()
    for href, text in links:
        external_id = place_id(platform, href)
        key = external_id or href
        if not key or key in seen:
            continue
        lines = [_visible_line(line) for line in text.splitlines() if _visible_line(line)]
        name = next(
            (
                line
                for line in lines
                if not re.fullmatch(r"\d+", line)
                and _fold(line) not in _CHROME
                and _fold(line) not in _BAD_NAMES
                and not _metric(line)
            ),
            None,
        )
        address = next((line for line in lines if _STREET.search(line)), None)
        if address:
            address = re.sub(r"(\d)\s*этаж\b.*", r"\1", address, flags=re.IGNORECASE)
            address = " ".join(address.split())
        phones = [" ".join(match.group(0).split()) for match in _PHONE.finditer(text)]
        if name is None and external_id is None:
            continue
        seen.add(key)
        fields = {}
        if name:
            fields["name"] = name
        if address:
            fields["address"] = address
        if phones:
            fields["phones"] = ", ".join(dict.fromkeys(phones))
        found.append(
            {
                "candidate_id": external_id or str(len(found) + 1),
                "external_id": external_id,
                "url": href,
                "fields": fields,
            }
        )
        if len(found) >= limit:
            break
    return found
