"""Fixture catalog for a city and category. Live discovery is not implemented."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from zhol.export.mailing import ClassTable, load_categories

PLATFORMS = ("2gis", "yandex", "google")
# Live catalog search is not a permitted source. Saved pages and a manual browser export are.
LIVE_DISCOVERY = {
    "2gis": "unsupported: catalog extraction is not permitted",
    "yandex": "unsupported: live requests are disabled",
    "google": "unsupported: live requests are disabled",
}


class BatchError(Exception):
    pass


@dataclass(frozen=True)
class CatalogCompany:
    id_2gis: str
    name: str
    city: str
    category: str


def load_catalog(path: str | Path) -> list[CatalogCompany]:
    file = Path(path)
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    rows = data.get("companies") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise BatchError(f"{file} needs a companies list")
    found: list[CatalogCompany] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BatchError(f"{file} has a company that is not a mapping")
        id_2gis = str(row.get("id_2gis") or "").strip()
        name = " ".join(str(row.get("name") or "").split())
        city = " ".join(str(row.get("city") or "").split())
        category = " ".join(str(row.get("category") or "").split())
        if not id_2gis or not name or not city or not category:
            raise BatchError(f"{file} company {id_2gis or name or '?'} needs id_2gis, name, city, and category")
        if id_2gis in seen:
            raise BatchError(f"{file} repeats {id_2gis}")
        seen.add(id_2gis)
        found.append(CatalogCompany(id_2gis, name, city, category))
    return found


def _key(value: str) -> str:
    return " ".join(value.casefold().split())


def select_companies(
    rows: list[CatalogCompany],
    *,
    city: str,
    category: str,
    table: ClassTable | None = None,
) -> list[CatalogCompany]:
    labels = table or load_categories()
    key = _key(category)
    if key in labels.ambiguous:
        raise BatchError(f"category {category} is ambiguous")
    if key not in labels.exact:
        raise BatchError(f"unsupported category {category}")
    city_key = _key(city)
    if not city_key:
        raise BatchError("city is required")
    return [row for row in rows if _key(row.city) == city_key and _key(row.category) == key]
