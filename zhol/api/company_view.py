"""Readable company cards for the admin panel.

Stored snapshots stay as they are. A missing platform is not filled from another
platform, and a saved audit is not described as a live check.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from zhol.agents.scout.normalize import normalize_phone
from zhol.audit.bek import PROMPT as BEK_PROMPT
from zhol.browser.discovery.normalize import verified_email, verified_instagram
from zhol.db.models import Audit, CardSnapshot, Company
from zhol.export.mailing import BLOCKED, READY, classify, load_categories
from zhol.redact import redact
from zhol.timeutil import ALMATY

PLATFORMS = ("2gis", "yandex", "google")
PLATFORM_TITLE = {"2gis": "2ГИС", "yandex": "Яндекс Карты", "google": "Google Maps"}
CONTACT_HINT = (
    "«Запрет не установлен» значит, что запрета на связь в базе нет. "
    "Это не согласие на рекламу и не разрешение на рассылку."
)
CONSENT_UNKNOWN = "Не записано"
CONSENT_HINT = (
    "Согласие на рассылку в базе не записано. "
    "Значение «Запрет не установлен» в согласие не превращается."
)
# Header address is a display choice. The company row and the snapshots are not updated.
# Prefer the newest verified found address when every such snapshot names the same place.
# If none is verified, show the newest collected address and label it as unverified.
# Two different addresses are not collapsed into one. Formula, blocked, and ambiguous rows stay out.
ADDRESS_RULE = "newest_verified_else_collected_unambiguous"
SCOPE_NOTE = (
    "Адрес в шапке берется из самого нового проверенного снимка площадки, если такой адрес один. "
    "Если проверенного адреса нет, показывается адрес из собранного снимка и помечается как не проверенный. "
    "Если адреса расходятся, в шапку не выбирается ни один. Запись компании не меняется."
)
HISTORY_NOTE = "Здесь хранятся данные карточек компании, сохранённые во время предыдущих проверок."
NOT_LIVE = "Это сохраненные данные проверки, а не живой просмотр карточки."
PRICE_LABEL = {
    "company_price": "прайс компании",
    "photo_only": "только фото прайса",
    "empty": "пусто",
    "yes": "есть",
    "yes_outdated": "есть, давно не обновлялись",
    "no": "нет",
}
SOURCE_LABEL = {
    "bek_audit": "Книга аудита Бека",
    "manual_import": "Ручной эталон",
    "scout": "Сбор карточек",
}
KEY_FIELDS = (
    ("rating", "Рейтинг"),
    ("reviews_count", "Число отзывов"),
    ("replies_count", "Ответы на отзывы"),
    ("photos_count", "Число фото"),
    ("news_count", "Новости"),
    ("prices", "Цены"),
    ("category", "Рубрика"),
    ("address", "Адрес"),
    ("phones", "Телефон"),
)
CONTACT_FIELDS = (
    ("phones", "Телефон"),
    ("whatsapp", "WhatsApp"),
    ("instagram", "Instagram"),
    ("website", "Сайт"),
    ("email", "Почта"),
    ("link", "Ссылка"),
)


def contact_label(dnc: bool | None) -> str:
    """False means the ban is absent. It is not marketing consent."""
    if dnc is True:
        return "Не связываться"
    if dnc is False:
        return "Запрет не установлен"
    return "Не определено"


def consent_label(dnc: bool | None) -> str:
    """Outreach consent is not stored on the company. A false ban is not consent."""
    del dnc
    return CONSENT_UNKNOWN


def public_http(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return None


def list_item(
    company: Company,
    snapshots: list[CardSnapshot],
    platform_ids: list[Any] | None = None,
) -> dict[str, Any]:
    card = _newest(snapshots, "2gis", READY)
    phone = _plain(card, "phones") or _plain(card, "whatsapp") or _text(company.whatsapp)
    address = header_address(snapshots)
    return {
        "id": company.id,
        "name": company.name,
        "city": company.city or "Нет данных",
        "niche": company.niche,
        "category": _text(company.niche) or "Нет данных",
        "address": address["address"],
        "address_note": address["address_note"],
        "address_verified": address["address_verified"],
        "phone": phone or "Нет данных",
        "dnc": company.dnc,
        "contact_label": contact_label(company.dnc),
        "consent_label": consent_label(company.dnc),
        "platforms_view": [_indicator(name, snapshots) for name in PLATFORMS],
        "source": company.source,
        **identity_view(company, snapshots, platform_ids or []),
    }


def identity_view(company: Company, snapshots: list[CardSnapshot], platform_ids: list[Any]) -> dict[str, Any]:
    """Source platform and match state. A missing 2GIS id is an empty value, not a failure."""
    present = {row.platform for row in snapshots}
    missing = [name for name in PLATFORMS if name not in present]
    review = any(_needs_review(row) for row in snapshots)
    identities = _identities(company, platform_ids)
    if review:
        status = "требует проверки"
        note = "Есть неоднозначное совпадение. Нужна проверка человеком."
    elif missing:
        status = "не все площадки проверены"
        note = "Площадки без снимка еще не проверялись."
    else:
        status = "площадки проверены"
        note = ""
    source = _source_platform(company, snapshots, identities)
    return {
        "id_2gis": company.id_2gis,
        "id_2gis_label": company.id_2gis or "Номер 2ГИС не указан",
        "source_platform": source,
        "source_platform_label": PLATFORM_TITLE.get(source, "Не указана"),
        "platform_identities": identities,
        "matching_status": status,
        "missing_platforms": [
            {"platform": name, "label": PLATFORM_TITLE[name], "text": "не проверялось"} for name in missing
        ],
        "review_needed": review,
        "review_note": note,
    }


def company_cards(
    company: Company,
    snapshots: list[CardSnapshot],
    audit: Audit | None,
) -> dict[str, Any]:
    """snapshots are newest first and are not modified."""
    return {
        "contact_hint": CONTACT_HINT,
        "consent_hint": CONSENT_HINT,
        "history_note": HISTORY_NOTE,
        "header": _header(company, snapshots, audit),
        "cards": [_card(name, _newest(snapshots, name)) for name in PLATFORMS],
        "history": [_group(name, snapshots) for name in PLATFORMS],
        "historical_audit": _historical(audit),
    }


def header_address(snapshots: list[CardSnapshot]) -> dict[str, Any]:
    """Choose a display address. Snapshots and the company row stay unchanged."""
    verified = [row for row in snapshots if _address_row(row, "verified")]
    if verified:
        return _one_address(verified, verified=True)
    collected = [row for row in snapshots if _address_row(row, "collected")]
    if collected:
        return _one_address(collected, verified=False)
    return _address_gap(_withheld_note(snapshots))


def _one_address(rows: list[CardSnapshot], *, verified: bool) -> dict[str, Any]:
    texts: list[str] = []
    for row in rows:
        text = _plain(row, "address")
        if text and text.casefold() not in {item.casefold() for item in texts}:
            texts.append(text)
    if len(texts) > 1:
        names = ", ".join(PLATFORM_TITLE[name] for name in PLATFORMS if any(row.platform == name for row in rows))
        if verified:
            note = (
                f"В проверенных снимках несколько разных адресов ({names}). "
                "В шапку не выбран ни один, чтобы не смешать филиалы. Запись компании не изменялась."
            )
        else:
            note = (
                f"В собранных снимках несколько разных адресов ({names}). "
                "Они не проверены, и в шапку не выбран ни один. Запись компании не изменялась."
            )
        return _address_gap(note)
    chosen = _newest(rows)
    if chosen is None:
        return _address_gap("Проверенного адреса нет, и собранного адреса тоже нет.")
    text = _plain(chosen, "address") or "Нет данных"
    when = _format_when(chosen.collected_at)
    title = PLATFORM_TITLE.get(chosen.platform, chosen.platform)
    if verified:
        note = f"Адрес из проверенного снимка {title} от {when}. Запись компании не изменялась."
        freshness = "newest_verified"
    else:
        note = (
            f"Адрес из собранного снимка {title} от {when}. "
            "Снимок не проверен, это не проверенный адрес. Запись компании не изменялась."
        )
        freshness = "collected_unverified"
    return {
        "address": text,
        "address_rule": ADDRESS_RULE,
        "address_note": note,
        "address_verified": verified,
        "address_provenance": {
            "snapshot_id": chosen.id,
            "platform": chosen.platform,
            "status": chosen.status,
            "match_status": chosen.match_status,
            "collected_at": when,
            "source_label": _source(chosen.collected_by),
            "freshness": freshness,
            "verified": verified,
        },
    }


def _address_gap(note: str) -> dict[str, Any]:
    return {
        "address": "Нет данных",
        "address_rule": ADDRESS_RULE,
        "address_note": note,
        "address_verified": None,
        "address_provenance": None,
    }


def _address_row(row: CardSnapshot, status: str) -> bool:
    if row.platform not in PLATFORMS or row.status != status or row.match_status != "found":
        return False
    if _blocked(row) or _formula(_sources(row), "address"):
        return False
    return _plain(row, "address") is not None


def _withheld_note(snapshots: list[CardSnapshot]) -> str:
    held = [row for row in snapshots if row.platform in PLATFORMS and _plain(row, "address")]
    if not held:
        return "Проверенного адреса нет, и собранного адреса тоже нет."
    if any(row.match_status != "found" for row in held):
        return "Адрес в снимке неоднозначный, поэтому в шапку он не подставляется."
    return "Адрес в снимке не подходит для шапки: он формульный или без явного текста."


def _header(company: Company, snapshots: list[CardSnapshot], audit: Audit | None) -> dict[str, Any]:
    verified = [row for row in snapshots if row.status == "verified" and not _blocked(row)]
    category_row = _newest([row for row in verified if _plain(row, "category")], "2gis") or _newest(
        [row for row in verified if _plain(row, "category")]
    )
    category_label = _plain(category_row, "category")
    sector, category, subcategory = _classify(category_label)
    address = header_address(snapshots)
    phone_row = _newest([row for row in verified if _plain(row, "phones")])
    site_row = _newest([row for row in verified if _plain(row, "website") or _plain(row, "link")])
    email_row = _newest([row for row in verified if _plain(row, "email")])
    observed = _observed_on(audit)
    return {
        "name": company.name,
        "sector": sector or "Нет данных",
        "category": category or _text(company.niche) or "Нет данных",
        "subcategory": subcategory or "Нет данных",
        "city": company.city or "Нет данных",
        **address,
        "district": _plain(_newest(verified, None), "district") or "Нет данных",
        "phone": _plain(phone_row, "phones") or "Нет данных",
        "whatsapp": _text(company.whatsapp) or "Нет данных",
        "instagram": _text(company.instagram) or "Нет данных",
        "website": (_plain(site_row, "website") or _plain(site_row, "link") or "Нет данных"),
        "email": _plain(email_row, "email") or "Нет данных",
        "phone_links": _phone_links(_plain(phone_row, "phones") or ""),
        "whatsapp_href": _whatsapp_href(_text(company.whatsapp)),
        "instagram_href": _instagram_href(_text(company.instagram)),
        "website_href": public_http(_plain(site_row, "website") or _plain(site_row, "link") or ""),
        "email_href": _email_href(_plain(email_row, "email") or ""),
        "contact_label": contact_label(company.dnc),
        "consent_label": consent_label(company.dnc),
        "consent_note": CONSENT_HINT,
        "id_2gis": company.id_2gis,
        "id_2gis_label": company.id_2gis or "Номер 2ГИС не указан",
        "audit_score": "Нет данных" if audit is None else str(audit.score_total),
        "audit_observed_on": observed or "Нет данных",
        "audit_note": "Аудита в базе нет."
        if audit is None
        else "Сохраненный балл аудита. Это не живая проверка карточки.",
        "scope_note": SCOPE_NOTE,
        "live_verification": False,
    }


def _card(platform: str, row: CardSnapshot | None) -> dict[str, Any]:
    title = PLATFORM_TITLE[platform]
    if row is None:
        facts = [{"label": label, "text": "Не проверялось"} for label in _FACT_LABELS]
        return {
            "platform": platform,
            "title": title,
            "notice": "Эта площадка в базе не проверялась.",
            "live_verification": False,
            "facts": facts,
            "contacts": [{"label": "Контакты", "text": "Не проверялось", "state": "unchecked"}],
        }
    fields = row.fields if isinstance(row.fields, dict) else {}
    sources = row.field_sources if isinstance(row.field_sources, dict) else {}
    href = public_http(row.url)
    if href:
        link_text = href
    elif _ambiguous(row):
        link_text = "Совпадение не определено"
    elif row.match_status == "not_found":
        link_text = "Не найдена"
    elif row.match_status == "other_address":
        link_text = "Другой адрес"
    else:
        link_text = "Нет данных"
    shot = public_http(row.screenshot_path)
    if shot:
        shot_text = "Открыть скриншот"
    elif row.screenshot_path:
        shot_text = "Скриншот сохранен, ссылка не публикуется."
    else:
        shot_text = "Скриншота нет."
    facts = [
        {"label": "Наличие карточки", "text": _presence(row)},
        {"label": "Адрес", "text": _stored(fields, sources, "address")},
        {"label": "Ссылка на карточку", "text": link_text, **({"href": href} if href else {})},
        {"label": "Рейтинг", "text": _stored(fields, sources, "rating")},
        {"label": "Число отзывов", "text": _stored(fields, sources, "reviews_count")},
        {"label": "Ответы на отзывы", "text": _replies(fields, sources)},
        {"label": "Число фото", "text": _stored(fields, sources, "photos_count")},
        {"label": "Новости", "text": _stored(fields, sources, "news_count")},
        {"label": "Рубрика", "text": _stored(fields, sources, "category")},
        {"label": "Описание", "text": _stored(fields, sources, "description")},
        {"label": "Часы работы", "text": _stored(fields, sources, "hours")},
        {"label": "Услуги и цены", "text": _services(fields, sources)},
        {"label": "Контакты", "text": _contacts(fields, sources)},
        {"label": "Дата проверки", "text": _format_when(row.collected_at)},
        {"label": "Источник данных", "text": _source(row.collected_by)},
        {"label": "Статус проверки", "text": _verification(row.status)},
        {"label": "Скриншот", "text": shot_text, **({"href": shot} if shot else {})},
    ]
    for fact in facts:
        fact["text"] = redact(str(fact["text"]))[:500]
    contacts = _contact_items(fields, sources)
    for item in contacts:
        item["text"] = redact(str(item["text"]))[:500]
        for link in item.get("links") or []:
            link["text"] = redact(str(link["text"]))[:200]
    return {
        "platform": platform,
        "title": title,
        "notice": NOT_LIVE,
        "live_verification": False,
        "facts": facts,
        "contacts": contacts,
    }


_FACT_LABELS = (
    "Наличие карточки",
    "Адрес",
    "Ссылка на карточку",
    "Рейтинг",
    "Число отзывов",
    "Ответы на отзывы",
    "Число фото",
    "Новости",
    "Рубрика",
    "Описание",
    "Часы работы",
    "Услуги и цены",
    "Контакты",
    "Дата проверки",
    "Источник данных",
    "Статус проверки",
    "Скриншот",
)


def _group(platform: str, snapshots: list[CardSnapshot]) -> dict[str, Any]:
    rows = [row for row in snapshots if row.platform == platform]
    items = []
    for index, row in enumerate(rows):
        older = rows[index + 1] if index + 1 < len(rows) else None
        card = _card(platform, row)
        items.append(
            {
                "id": row.id,
                "when": _format_when(row.collected_at),
                "source_label": _source(row.collected_by),
                "verification_label": _verification(row.status),
                "changes": _changes(row, older),
                "facts": card["facts"],
                "contacts": card["contacts"],
                "live_verification": False,
            }
        )
    return {
        "platform": platform,
        "title": PLATFORM_TITLE[platform],
        "items": items,
        "empty": "Проверок этой площадки в базе нет." if not items else None,
    }


def _changes(row: CardSnapshot, older: CardSnapshot | None) -> list[str]:
    if older is None:
        return ["Первая сохраненная проверка"]
    notes = []
    left = _presence(older)
    right = _presence(row)
    if left != right:
        notes.append(f"Наличие карточки: {left} → {right}")
    for key, label in KEY_FIELDS:
        before = _stored(_fields(older), _sources(older), key)
        after = _stored(_fields(row), _sources(row), key)
        if before != after:
            notes.append(f"{label}: {before} → {after}")
    return [redact(note)[:500] for note in notes] or ["Ключевые поля без изменений"]


def _historical(audit: Audit | None) -> dict[str, Any]:
    if audit is None:
        return {
            "shown": False,
            "live_verification": False,
            "note": "Исторических наблюдений аудита нет.",
            "texts": [],
        }
    observed = audit.observations
    texts: list[str] = []
    day: str | None = None
    source = "Сохраненный аудит"
    if isinstance(observed, dict):
        raw = observed.get("texts")
        if isinstance(raw, list):
            texts = [redact(_line(item))[:500] for item in raw if _line(item)]
        elif _line(raw):
            texts = [redact(_line(raw))[:500]]
        if isinstance(observed.get("collected_on"), str):
            day = observed["collected_on"]
        if observed.get("source") == "bek_audit":
            source = "Книга аудита Бека"
    elif isinstance(observed, list):
        texts = [redact(_line(item))[:500] for item in observed if _line(item)]
    if audit.prompt_version == BEK_PROMPT:
        source = "Книга аудита Бека"
    return {
        "shown": True,
        "live_verification": False,
        "source_label": source,
        "observed_on": _format_day(day) if day else _format_day(audit.created_at),
        "score": str(audit.score_total),
        "texts": texts[:12],
        "note": "Исторические данные аудита. Они не заменяют проверенный снимок карточки и не являются живой проверкой.",
    }


def _indicator(platform: str, snapshots: list[CardSnapshot]) -> dict[str, str]:
    row = _newest(snapshots, platform)
    if row is None:
        text = "не проверялось"
    elif _blocked(row):
        text = "доступ ограничен"
    elif _ambiguous(row):
        text = "совпадение не определено"
    elif row.match_status == "not_found":
        text = "не найдена"
    elif row.match_status == "other_address":
        text = "другой адрес"
    elif row.match_status == "found" and row.status == "verified":
        text = "есть"
    elif row.match_status == "found":
        text = "есть, не подтверждено"
    else:
        text = "требует проверки"
    return {"platform": platform, "label": PLATFORM_TITLE[platform], "text": text}


def _classify(label: str | None) -> tuple[str | None, str | None, str | None]:
    if not label:
        return None, None, None
    try:
        table = load_categories()
    except (OSError, ValueError):
        return None, None, None
    return classify([label], table)


def _newest(
    snapshots: list[CardSnapshot],
    platform: str | None = None,
    statuses: frozenset[str] | None = None,
) -> CardSnapshot | None:
    chosen: CardSnapshot | None = None
    for row in snapshots:
        if platform is not None and row.platform != platform:
            continue
        if statuses is not None and row.status not in statuses:
            continue
        if chosen is None or (row.collected_at, row.id) > (chosen.collected_at, chosen.id):
            chosen = row
    return chosen


def _blocked(row: CardSnapshot) -> bool:
    reason = (row.fail_reason or "").strip().lower()
    if reason in BLOCKED:
        return True
    access = _sources(row).get("_access")
    if not isinstance(access, dict):
        return False
    return access.get("access") in BLOCKED or access.get("outcome") in BLOCKED


def _presence(row: CardSnapshot) -> str:
    if _blocked(row):
        return "Доступ ограничен"
    if _ambiguous(row):
        return "Совпадение не определено"
    if row.match_status == "not_found":
        return "Не найдена"
    if row.match_status == "other_address":
        return "Другой адрес"
    if row.match_status == "found":
        return "Карточка найдена"
    return "Данные требуют проверки"


def _needs_review(row: CardSnapshot) -> bool:
    hold = _sources(row).get("_collection") or {}
    if isinstance(hold, dict) and hold.get("hold") in {"ambiguous_match", "match_needs_review", "platform_id_conflict"}:
        return True
    return _ambiguous(row)


def _identities(company: Company, platform_ids: list[Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if company.id_2gis:
        found.append({"platform": "2gis", "label": PLATFORM_TITLE["2gis"], "external_id": company.id_2gis})
    for row in platform_ids:
        external = getattr(row, "external_id", None)
        platform = getattr(row, "platform", None)
        if not external or platform not in PLATFORM_TITLE:
            continue
        if any(item["platform"] == platform and item["external_id"] == external for item in found):
            continue
        found.append({"platform": platform, "label": PLATFORM_TITLE[platform], "external_id": str(external)})
    return found


def _source_platform(company: Company, snapshots: list[CardSnapshot], identities: list[dict[str, str]]) -> str:
    oldest: CardSnapshot | None = None
    for row in snapshots:
        if row.match_status != "found":
            continue
        if oldest is None or (row.collected_at, row.id) < (oldest.collected_at, oldest.id):
            oldest = row
    if oldest is not None:
        return oldest.platform
    if company.id_2gis:
        return "2gis"
    if identities:
        return identities[0]["platform"]
    return ""


def _ambiguous(row: CardSnapshot) -> bool:
    hold = (_sources(row).get("_collection") or {})
    if isinstance(hold, dict) and hold.get("hold") == "ambiguous_match":
        return True
    for source in _sources(row).values():
        if isinstance(source, dict) and source.get("reason") == "ambiguous":
            return True
    return False


def _verification(status: str) -> str:
    if status == "verified":
        return "Проверено"
    if status == "collected":
        return "Собрано, это не актуальная проверка"
    if status == "rejected":
        return "Отклонено"
    return "Данные требуют проверки"


def _source(value: str) -> str:
    if value in SOURCE_LABEL:
        return SOURCE_LABEL[value]
    if value and "/" not in value and "\\" not in value:
        return value
    return "Сохраненная запись"


def _stored(fields: dict[str, Any], sources: dict[str, Any], key: str) -> str:
    if _formula(sources, key):
        return "Данные требуют проверки"
    if key not in fields:
        return "Не проверялось"
    if key == "phones":
        shown = _phone_display(sources)
        if shown:
            return shown
    return _format(key, fields.get(key))


def _phone_display(sources: dict[str, Any]) -> str | None:
    source = sources.get("phones")
    if not isinstance(source, dict):
        return None
    value = source.get("display")
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(parts) or None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _formula(sources: dict[str, Any], key: str) -> bool:
    value = sources.get(key)
    return isinstance(value, dict) and value.get("formula") is True


def _format(key: str, value: object) -> str:
    if value is None:
        return "Нет данных"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.10f}".rstrip("0").rstrip(".")
    if key == "prices":
        if isinstance(value, str) and value in PRICE_LABEL:
            return PRICE_LABEL[value]
        return "Данные требуют проверки"
    if isinstance(value, str):
        text = value.strip()
        return text or "Нет данных"
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip() and str(item).strip() != "None"]
        return ", ".join(parts) or "Нет данных"
    if isinstance(value, dict):
        name = value.get("name") if isinstance(value.get("name"), str) else ""
        return name.strip() or "Нет данных"
    return "Данные требуют проверки"


def _replies(fields: dict[str, Any], sources: dict[str, Any]) -> str:
    replies = _stored(fields, sources, "replies_count")
    shown = _stored(fields, sources, "reviews_shown")
    if replies == "Не проверялось" and shown == "Не проверялось":
        return "Не проверялось"
    if "Данные требуют проверки" in (replies, shown):
        return "Данные требуют проверки"
    if replies not in ("Нет данных", "Не проверялось") and shown not in ("Нет данных", "Не проверялось"):
        return f"{replies} из {shown}"
    if replies != "Не проверялось":
        return replies
    return shown


def _services(fields: dict[str, Any], sources: dict[str, Any]) -> str:
    prices = _stored(fields, sources, "prices")
    services = _stored(fields, sources, "services")
    if prices == "Не проверялось" and services == "Не проверялось":
        return "Не проверялось"
    parts = []
    if prices != "Не проверялось":
        parts.append(f"Цены: {prices}")
    if services != "Не проверялось":
        parts.append(f"Услуги: {services}")
    return "; ".join(parts)


def _fact_state(text: str) -> str:
    if text == "Не проверялось":
        return "unchecked"
    if text == "Данные требуют проверки":
        return "unverified"
    if text == "Нет данных":
        return "empty"
    return "value"


def _phone_links(text: str) -> list[dict[str, str]]:
    links = []
    for part in text.split(","):
        shown = part.strip()
        number = normalize_phone(shown) if shown else None
        if shown and number:
            links.append({"text": shown, "href": f"tel:+{number}"})
    return links


def _whatsapp_href(text: str) -> str | None:
    raw = text.strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")) and ("wa.me" in lowered or "whatsapp" in lowered):
        return raw.split()[0]
    number = normalize_phone(raw)
    if number is None:
        return None
    return f"https://wa.me/{number}"


def _instagram_href(text: str) -> str | None:
    handle = verified_instagram(text if "instagram.com" in text.lower() else None, text)
    if not handle:
        return None
    return f"https://instagram.com/{handle}"


def _email_href(text: str) -> str | None:
    mail = verified_email(text)
    if not mail:
        return None
    return f"mailto:{mail}"


def _contact_items(fields: dict[str, Any], sources: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen = False
    for key, label in CONTACT_FIELDS:
        text = _stored(fields, sources, key)
        if text != "Не проверялось":
            seen = True
        item: dict[str, Any] = {"label": label, "text": text, "state": _fact_state(text)}
        if item["state"] == "value":
            links = _links_for(key, text)
            if links:
                item["links"] = links
                if key == "website":
                    item["text"] = "Открыть сайт"
                elif key == "link":
                    item["text"] = "Открыть ссылку"
        items.append(item)
    if not seen:
        return [{"label": "Контакты", "text": "Не проверялось", "state": "unchecked"}]
    return items


def _links_for(key: str, text: str) -> list[dict[str, str]]:
    if key == "phones":
        return _phone_links(text)
    if key == "whatsapp":
        href = _whatsapp_href(text)
        return [{"text": text, "href": href}] if href else []
    if key == "instagram":
        href = _instagram_href(text)
        shown = text[1:] if text.startswith("@") else text
        return [{"text": shown, "href": href}] if href else []
    if key == "email":
        href = _email_href(text)
        return [{"text": text, "href": href}] if href else []
    if key in {"website", "link"}:
        href = public_http(text)
        label = "Открыть сайт" if key == "website" else "Открыть ссылку"
        return [{"text": label, "href": href}] if href else []
    return []


def _contacts(fields: dict[str, Any], sources: dict[str, Any]) -> str:
    parts = []
    unseen = True
    for key, label in CONTACT_FIELDS:
        text = _stored(fields, sources, key)
        if text != "Не проверялось":
            unseen = False
            parts.append(f"{label}: {text}")
    if unseen:
        return "Не проверялось"
    if parts and all(item.endswith(": Нет данных") or item.endswith(": Данные требуют проверки") for item in parts):
        if any(item.endswith("Данные требуют проверки") for item in parts):
            return "Данные требуют проверки"
        return "Нет данных"
    return "; ".join(parts)


def _plain(row: CardSnapshot | None, key: str) -> str | None:
    if row is None or _blocked(row) or _formula(_sources(row), key):
        return None
    text = _format(key, _fields(row).get(key) if key in _fields(row) else None)
    if text in ("Нет данных", "Не проверялось", "Данные требуют проверки"):
        return None
    return text


def _fields(row: CardSnapshot) -> dict[str, Any]:
    return row.fields if isinstance(row.fields, dict) else {}


def _sources(row: CardSnapshot) -> dict[str, Any]:
    return row.field_sources if isinstance(row.field_sources, dict) else {}


def _line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[:500]


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _format_when(value: datetime | None) -> str:
    if value is None:
        return "Нет данных"
    current = value if value.tzinfo is not None else value.replace(tzinfo=ALMATY)
    return current.astimezone(ALMATY).strftime("%d.%m.%Y, %H:%M")


def _format_day(value: object) -> str:
    if isinstance(value, datetime):
        return _format_when(value).split(",")[0]
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        year, month, day = value[:10].split("-")
        return f"{day}.{month}.{year}"
    return "Нет данных"


def _observed_on(audit: Audit | None) -> str | None:
    if audit is None:
        return None
    observed = audit.observations if isinstance(audit.observations, dict) else {}
    day = observed.get("collected_on") if isinstance(observed, dict) else None
    if isinstance(day, str):
        return _format_day(day)
    if audit.created_at is not None:
        return _format_day(audit.created_at)
    return None
