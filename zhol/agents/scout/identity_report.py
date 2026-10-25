"""Read-only report of conflicting identities already stored on one company.

The report does not merge, split, delete, or rewrite platform ids.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhol.agents.scout.identity import Profile, Ref, relate
from zhol.agents.scout.match_index import _phone_list
from zhol.agents.scout.normalize import phone_set


def conflicts(session: Session) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (s.company_id, s.platform)
                   s.company_id, c.name, s.platform, s.fields
            FROM card_snapshots AS s
            JOIN companies AS c ON c.id = s.company_id
            WHERE s.company_id IS NOT NULL
            ORDER BY s.company_id, s.platform, s.collected_at DESC, s.id DESC
            """
        )
    ).all()
    refs = session.execute(
        text(
            """
            SELECT company_id, platform, external_id
            FROM company_platform_ids
            WHERE external_id IS NOT NULL
            UNION
            SELECT id, '2gis', id_2gis
            FROM companies
            WHERE id_2gis IS NOT NULL
            """
        )
    ).all()
    by_company: dict[int, dict[str, object]] = {}
    for company_id, name, platform, fields in rows:
        item = by_company.setdefault(int(company_id), {"name": name, "platforms": {}})
        phones = tuple(sorted(phone_set(_phone_list(fields))))
        address = fields.get("address") if isinstance(fields, dict) else None
        if not isinstance(address, str):
            address = None
        item["platforms"][str(platform)] = {"address": address, "phones": phones, "refs": [], "snapshot": True}
    for company_id, platform, external_id in refs:
        item = by_company.get(int(company_id))
        if item is None:
            continue
        platforms = item["platforms"]
        assert isinstance(platforms, dict)
        slot = platforms.get(str(platform))
        if slot is None:
            continue
        slot["refs"].append(Ref(str(platform), str(external_id)))
    found: list[dict[str, object]] = []
    for company_id, item in by_company.items():
        platforms = item["platforms"]
        assert isinstance(platforms, dict)
        names = sorted(platforms)
        for index, left_name in enumerate(names):
            for right_name in names[index + 1 :]:
                left = platforms[left_name]
                right = platforms[right_name]
                relation = relate(
                    Profile(str(item["name"]), left["address"], left["phones"], tuple(left["refs"])),
                    Profile(str(item["name"]), right["address"], right["phones"], tuple(right["refs"])),
                )
                if relation.kind == "same" and relation.phone_confirmed:
                    continue
                found.append(
                    {
                        "company_id": company_id,
                        "name": item["name"],
                        "left": left_name,
                        "right": right_name,
                        "kind": relation.kind,
                    }
                )
    return found


def render(session: Session) -> str:
    rows = conflicts(session)
    lines = ["Проверка связей компаний. Записи не изменялись."]
    if not rows:
        lines.append("Сомнительных связей внутри одной компании нет.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"Компания {row['company_id']} «{row['name']}»: {row['left']} и {row['right']} не подтверждены ({row['kind']})."
        )
    lines.append(f"Итого сомнительных связей: {len(rows)}.")
    return "\n".join(lines)
