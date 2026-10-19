"""Kazakhstan localities the collectors can name without inventing a platform id.

A 2GIS value is the public path slug already used as ``/{slug}/search/...``.
It is not a Catalog API region id. A Yandex value is only the region id and
slug this collector already opens. Google has no place id: the city name is
added to the search text and is not stored as the business category.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class YandexPlace:
    region_id: str
    slug: str


@dataclass(frozen=True)
class Locality:
    id: str
    name: str
    region: str
    twogis_slug: str | None
    yandex: YandexPlace | None
    google: bool
    aliases: tuple[str, ...] = ()

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "region": self.region,
            "sources": {
                "2gis": self.twogis_slug is not None,
                "google": self.google,
                "yandex": self.yandex is not None,
            },
        }


# Yandex region 162 / slug almaty is the path already built in zhol/browser/yandex/selenium.py.
_ALMATY_YANDEX = YandexPlace("162", "almaty")

LOCALITIES: tuple[Locality, ...] = (
    Locality("almaty", "Алматы", "город Алматы", "almaty", _ALMATY_YANDEX, True, ("алма-ата", "алма ата")),
    Locality("astana", "Астана", "город Астана", "astana", None, True, ("нур-султан", "нурсултан", "нур султан")),
    Locality("shymkent", "Шымкент", "город Шымкент", "shymkent", None, True, ("чимкент",)),
    Locality("karaganda", "Караганда", "Карагандинская область", "karaganda", None, True, ("караганды",)),
    Locality("aktobe", "Актобе", "Актюбинская область", "aktobe", None, True, ("актюбинск",)),
    Locality("taraz", "Тараз", "Жамбылская область", "taraz", None, True, ("джамбул",)),
    Locality("pavlodar", "Павлодар", "Павлодарская область", "pavlodar", None, True),
    Locality(
        "oskemen",
        "Усть-Каменогорск",
        "Восточно-Казахстанская область",
        "oskemen",
        None,
        True,
        ("оскемен", "усть каменогорск"),
    ),
    Locality("semey", "Семей", "Абайская область", "semey", None, True, ("семипалатинск",)),
    Locality("atyrau", "Атырау", "Атырауская область", "atyrau", None, True),
    Locality("aktau", "Актау", "Мангистауская область", "aktau", None, True),
    Locality("kostanay", "Костанай", "Костанайская область", "kostanay", None, True, ("кустанай",)),
    Locality("kyzylorda", "Кызылорда", "Кызылординская область", "kyzylorda", None, True, ("кзыл-орда", "кызыл-орда")),
    Locality("oral", "Уральск", "Западно-Казахстанская область", "oral", None, True, ("орал",)),
    Locality("petropavlovsk", "Петропавловск", "Северо-Казахстанская область", "petropavlovsk", None, True, ("петропавл",)),
    Locality("kokshetau", "Кокшетау", "Акмолинская область", "kokshetau", None, True, ("кокчетав",)),
    Locality("taldykorgan", "Талдыкорган", "Жетысуская область", "taldykorgan", None, True, ("талды-курган",)),
    Locality("turkestan", "Туркестан", "Туркестанская область", "turkestan", None, True),
    Locality("zhezkazgan", "Жезказган", "Улытауская область", "zhezkazgan", None, True, ("жезказган",)),
    Locality("temirtau", "Темиртау", "Карагандинская область", "temirtau", None, True),
    Locality("ekibastuz", "Экибастуз", "Павлодарская область", "ekibastuz", None, True),
    Locality("rudny", "Рудный", "Костанайская область", "rudny", None, True),
    Locality("balkhash", "Балхаш", "Карагандинская область", "balkhash", None, True),
    # No verified 2GIS path slug and no verified Yandex region id. Google search text only.
    Locality("konaev", "Конаев", "Алматинская область", None, None, True, ("капчагай", "капшагай")),
    Locality("satpaev", "Сатпаев", "Улытауская область", None, None, True),
    Locality("kentau", "Кентау", "Туркестанская область", None, None, True),
    Locality("zhanaozen", "Жанаозен", "Мангистауская область", None, None, True, ("новый узень",)),
    Locality("kaskelen", "Каскелен", "Алматинская область", None, None, True),
    Locality("talgar", "Талгар", "Алматинская область", None, None, True),
    Locality("esik", "Есик", "Алматинская область", None, None, True, ("исык",)),
)

def _fold_name(value: str) -> str:
    text = value.casefold().replace("ё", "е").replace("-", " ")
    return " ".join(text.split())


_BY_ID = {item.id: item for item in LOCALITIES}
_BY_FOLD: dict[str, Locality] = {}
for _item in LOCALITIES:
    _BY_FOLD.setdefault(_fold_name(_item.name), _item)
    _BY_FOLD.setdefault(_fold_name(_item.id), _item)
    for _alias in _item.aliases:
        _BY_FOLD.setdefault(_fold_name(_alias), _item)


def all_localities() -> tuple[Locality, ...]:
    return LOCALITIES


def match_locality(city: str) -> Locality | None:
    return _BY_FOLD.get(_fold_name(city))


def require_locality(city: str) -> Locality:
    found = match_locality(city)
    if found is None:
        shown = " ".join(city.split()) or "без названия"
        raise ValueError(
            f"Город «{shown}» нет в справочнике Казахстана. Сбор не переключается на Алматы."
        )
    return found


def names_for(platform: str) -> list[str]:
    if platform not in {"2gis", "google", "yandex"}:
        raise ValueError("unknown platform")
    names: list[str] = []
    for item in LOCALITIES:
        if platform == "2gis" and item.twogis_slug:
            names.append(item.name)
        elif platform == "google" and item.google:
            names.append(item.name)
        elif platform == "yandex" and item.yandex is not None:
            names.append(item.name)
    if "Алматы" in names:
        names.remove("Алматы")
        names.insert(0, "Алматы")
    return names


def twogis_slug(city: str) -> tuple[str, str]:
    found = require_locality(city)
    if not found.twogis_slug:
        raise ValueError(
            f"Город «{found.name}» не поддержан в 2ГИС: нет проверенного адреса поиска. "
            "Сбор не переключается на Алматы."
        )
    return found.name, found.twogis_slug


def yandex_place(city: str) -> tuple[str, YandexPlace]:
    found = require_locality(city)
    if found.yandex is None:
        raise ValueError(
            f"Город «{found.name}» не поддержан в Яндекс Картах: нет проверенного номера региона. "
            "Сбор не переключается на Алматы."
        )
    return found.name, found.yandex


def supports(city: str, platform: str) -> bool:
    found = match_locality(city)
    if found is None or platform not in {"2gis", "google", "yandex"}:
        return False
    if platform == "2gis":
        return found.twogis_slug is not None
    if platform == "yandex":
        return found.yandex is not None
    return found.google


def region_of(city: str | None) -> str | None:
    if not city:
        return None
    found = match_locality(city)
    return found.region if found is not None else None


def google_search_text(query: str, city: str) -> str:
    """Location hint for the Google search URL. The stored query stays unchanged."""
    text = " ".join(query.split())
    name = require_locality(city).name
    if _fold_name(name) in _fold_name(text):
        return text
    return f"{text} {name}"
