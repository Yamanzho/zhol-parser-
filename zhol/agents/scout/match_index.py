"""Indexed name and phone keys for company lookup.

The keys are derived from the company name and from the newest snapshot of one
platform. Rebuilding them does not merge, split, or delete companies.

Supported writes are the SQLAlchemy ORM unit of work: insert, update, and
delete of `Company` and `CardSnapshot`. Those events keep the keys in the same
transaction. Direct SQL, `bulk_insert_mappings`, and `executemany` do not fire
the events. After any such write, run `rebuild_match_keys` (CLI:
`zhol rebuild-match-keys`) before another import. The command is idempotent
and does not change companies, snapshots, or platform ids.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from zhol.agents.scout.normalize import name_key, phone_set

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from sqlalchemy import event

    from zhol.db.models import CardSnapshot, Company

    event.listen(Company, "after_insert", _on_company, propagate=True)
    event.listen(Company, "after_update", _on_company, propagate=True)
    event.listen(CardSnapshot, "after_insert", _on_snapshot, propagate=True)
    event.listen(CardSnapshot, "after_update", _on_snapshot, propagate=True)
    event.listen(CardSnapshot, "after_delete", _on_snapshot, propagate=True)
    _INSTALLED = True


def candidate_ids(
    session: Session,
    *,
    city: str | None,
    platform: str | None,
    name: str | None,
    phones: Iterable[str],
) -> list[int]:
    if not city:
        return []
    from sqlalchemy import or_

    from zhol.db.models import CompanyMatchKey

    clauses = []
    key = name_key(name)
    if key:
        clauses.append(
            (CompanyMatchKey.kind == "name") & (CompanyMatchKey.platform == "") & (CompanyMatchKey.value == key)
        )
    numbers = sorted(phone_set(list(phones)))
    if numbers and platform:
        clauses.append(
            (CompanyMatchKey.kind == "phone")
            & (CompanyMatchKey.platform == platform)
            & (CompanyMatchKey.value.in_(numbers))
        )
    if not clauses:
        return []
    found = session.scalars(
        select(CompanyMatchKey.company_id)
        .where(CompanyMatchKey.city == city, or_(*clauses))
        .distinct()
    ).all()
    return list(found)


def rebuild_match_keys(connection) -> int:
    """Replace every key from current companies and newest snapshots. Company rows stay."""
    if connection.dialect.name != "postgresql":
        return 0
    from zhol.db.models import CompanyMatchKey

    connection.execute(delete(CompanyMatchKey))
    companies = connection.execute(text("SELECT id, city, name FROM companies ORDER BY id")).all()
    snapshots = connection.execute(
        text(
            """
            SELECT DISTINCT ON (company_id, platform)
                   company_id, platform, fields
            FROM card_snapshots
            WHERE company_id IS NOT NULL
            ORDER BY company_id, platform, collected_at DESC, id DESC
            """
        )
    ).all()
    phones: dict[int, list[tuple[str, object]]] = {}
    for company_id, platform, fields in snapshots:
        phones.setdefault(int(company_id), []).append((str(platform), fields))
    rows: list[dict[str, object]] = []
    for company_id, city, name in companies:
        key = name_key(str(name) if name is not None else None)
        if key:
            rows.append(
                {"company_id": int(company_id), "city": city, "platform": "", "kind": "name", "value": key}
            )
        for platform, fields in phones.get(int(company_id), []):
            for number in sorted(phone_set(_phone_list(fields))):
                rows.append(
                    {
                        "company_id": int(company_id),
                        "city": city,
                        "platform": platform,
                        "kind": "phone",
                        "value": number,
                    }
                )
    if rows:
        for start in range(0, len(rows), 1000):
            connection.execute(CompanyMatchKey.__table__.insert(), rows[start : start + 1000])
    return len(rows)


def _on_company(_mapper, connection, target) -> None:
    if not _keys_ready(connection) or target.id is None:
        return
    _sync_name(connection, int(target.id), str(target.city), target.name)
    connection.execute(
        text("UPDATE company_match_keys SET city = :city WHERE company_id = :id AND kind = 'phone'"),
        {"city": target.city, "id": int(target.id)},
    )


def _on_snapshot(_mapper, connection, target) -> None:
    if not _keys_ready(connection) or target.company_id is None:
        return
    row = connection.execute(
        text("SELECT city, name FROM companies WHERE id = :id"),
        {"id": int(target.company_id)},
    ).one_or_none()
    if row is None:
        return
    _sync_name(connection, int(target.company_id), str(row.city), row.name)
    _sync_phones(connection, int(target.company_id), str(row.city), str(target.platform))


def _sync_name(connection, company_id: int, city: str, name: str | None) -> None:
    connection.execute(
        text("DELETE FROM company_match_keys WHERE company_id = :id AND kind = 'name'"),
        {"id": company_id},
    )
    key = name_key(name)
    if not key:
        return
    _insert(connection, [{"company_id": company_id, "city": city, "platform": "", "kind": "name", "value": key}])


def _sync_phones(connection, company_id: int, city: str, platform: str) -> None:
    connection.execute(
        text(
            "DELETE FROM company_match_keys WHERE company_id = :id AND kind = 'phone' AND platform = :platform"
        ),
        {"id": company_id, "platform": platform},
    )
    fields = connection.execute(
        text(
            """
            SELECT fields
            FROM card_snapshots
            WHERE company_id = :id AND platform = :platform
            ORDER BY collected_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"id": company_id, "platform": platform},
    ).scalar_one_or_none()
    numbers = sorted(phone_set(_phone_list(fields)))
    if not numbers:
        return
    _insert(
        connection,
        [
            {"company_id": company_id, "city": city, "platform": platform, "kind": "phone", "value": number}
            for number in numbers
        ],
    )


def _insert(connection, rows: list[dict[str, object]]) -> None:
    from zhol.db.models import CompanyMatchKey

    connection.execute(CompanyMatchKey.__table__.insert(), rows)


def _phone_list(fields: object) -> list[str]:
    if not isinstance(fields, dict):
        return []
    raw = fields.get("phones")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item]


def _keys_ready(connection) -> bool:
    """Check the catalog each time. A cached answer stays wrong after a migration."""
    if connection.dialect.name != "postgresql":
        return False
    return bool(
        connection.execute(text("SELECT to_regclass('public.company_match_keys') IS NOT NULL")).scalar()
    )
