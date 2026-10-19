"""Store the dry-run plan on the open draft package. A second run does not insert a duplicate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zhol.db.models import ChangeItem, ChangeLog, ChangePackage, HumanTask
from zhol.fulfillment.types import FieldPlan
from zhol.guards import no_long_dash
from zhol.timeutil import now

SUMMARY = "План правок, форма не отправлялась."


def change_key(order_id: int, platform: str, field: str, after: Any) -> str:
    raw = json.dumps(
        {"after": after, "field": field, "order_id": order_id, "platform": platform},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def persist_sets(
    session: Session,
    order_id: int,
    plans: Sequence[FieldPlan],
    *,
    screenshot_before: str | None,
    evidence: str | None,
) -> dict[tuple[str, str], bool]:
    """Return whether each set field already had a dry-run log before this call."""
    selected = [plan for plan in plans if plan.operation == "set"]
    if not selected:
        return {}
    if not no_long_dash(SUMMARY):
        raise ValueError("client summary must not use a long dash")
    package = _draft_package(session, order_id)
    existing = {
        item.idempotency_key: item
        for item in session.scalars(select(ChangeItem).where(ChangeItem.package_id.in_(_package_ids(order_id))))
    }
    current_n = session.scalar(select(func.max(ChangeItem.n)).where(ChangeItem.package_id == package.id)) or 0
    already: dict[tuple[str, str], bool] = {}
    for plan in selected:
        if plan.source_ref is None:
            raise ValueError(f"{plan.platform}.{plan.field} is missing a source")
        key = change_key(order_id, plan.platform, plan.field, plan.after)
        item = existing.get(key)
        if item is None:
            current_n += 1
            item = ChangeItem(
                package_id=package.id,
                n=current_n,
                platform=plan.platform,
                field=plan.field,
                before=plan.before,
                after=plan.after,
                source_ref={"ref": plan.source_ref},
                is_nap=plan.is_nap,
                nap_consent_ref=plan.nap_consent_ref,
                counts_to_limit=False,
                status="draft",
                idempotency_key=key,
            )
            session.add(item)
            session.flush()
            existing[key] = item
        logged = _logged(session, item.id)
        already[(plan.platform, plan.field)] = logged
        if logged:
            continue
        session.add(
            ChangeLog(
                change_item_id=item.id,
                action="apply",
                screenshot_before=screenshot_before or evidence,
                screenshot_after=None,
                result="dry_run",
                details={
                    "submitted": False,
                    "mode": "dry_run",
                    "before": plan.before,
                    "after": plan.after,
                    "verified": True,
                    "evidence": evidence,
                },
                at=now(),
            )
        )
    return already


def ensure_pause_task(
    session: Session,
    order_id: int,
    platform: str,
    reason: str,
    evidence: str | None,
) -> None:
    rows = session.scalars(
        select(HumanTask).where(
            HumanTask.object_type == "order",
            HumanTask.object_id == order_id,
            HumanTask.reason == "listing_paused",
            HumanTask.status == "open",
        )
    ).all()
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        if details.get("platform") == platform and details.get("pause") == reason:
            return
    session.add(
        HumanTask(
            reason="listing_paused",
            object_type="order",
            object_id=order_id,
            details={"platform": platform, "pause": reason, "submitted": False, "evidence": evidence},
            status="open",
            created_at=now(),
        )
    )


def _draft_package(session: Session, order_id: int) -> ChangePackage:
    package = session.scalar(
        select(ChangePackage).where(ChangePackage.order_id == order_id).order_by(ChangePackage.version.desc())
    )
    if package is not None and package.status == "draft":
        return package
    version = 1 if package is None else int(package.version) + 1
    if package is not None and package.status != "superseded":
        package.status = "superseded"
    created = ChangePackage(order_id=order_id, version=version, status="draft", summary_for_client=SUMMARY, created_at=now())
    session.add(created)
    session.flush()
    return created


def _package_ids(order_id: int):
    return select(ChangePackage.id).where(ChangePackage.order_id == order_id)


def prepared_already(session: Session, order_id: int, platform: str, field: str, after: Any) -> bool:
    """True when this exact prepared value was already logged. A second run does not write another row."""
    item = session.scalar(select(ChangeItem).where(ChangeItem.idempotency_key == change_key(order_id, platform, field, after)))
    if item is None:
        return False
    row = session.scalar(
        select(ChangeLog.id).where(
            ChangeLog.change_item_id == item.id,
            ChangeLog.action == "prepare",
            ChangeLog.result == "prepared",
        )
    )
    return row is not None


def persist_prepared(
    session: Session,
    order_id: int,
    plans: Sequence[FieldPlan],
    *,
    screenshot_before: str | None,
    screenshot_after: str | None,
    evidence: str | None,
    summary: str | None = None,
) -> None:
    """Log a cabinet prepare. The public card is not published and the form is not submitted."""
    if not plans:
        return
    if not no_long_dash(summary or PREPARED_SUMMARY):
        raise ValueError("client summary must not use a long dash")
    package = _draft_package(session, order_id)
    existing = {
        item.idempotency_key: item
        for item in session.scalars(select(ChangeItem).where(ChangeItem.package_id.in_(_package_ids(order_id))))
    }
    current_n = session.scalar(select(func.max(ChangeItem.n)).where(ChangeItem.package_id == package.id)) or 0
    for plan in plans:
        if plan.source_ref is None:
            raise ValueError(f"{plan.platform}.{plan.field} is missing a source")
        key = change_key(order_id, plan.platform, plan.field, plan.after)
        item = existing.get(key)
        if item is None:
            current_n += 1
            item = ChangeItem(
                package_id=package.id,
                n=current_n,
                platform=plan.platform,
                field=plan.field,
                before=plan.before,
                after=plan.after,
                source_ref={"ref": plan.source_ref},
                is_nap=plan.is_nap,
                nap_consent_ref=plan.nap_consent_ref,
                counts_to_limit=False,
                status="draft",
                idempotency_key=key,
            )
            session.add(item)
            session.flush()
            existing[key] = item
        if prepared_already(session, order_id, plan.platform, plan.field, plan.after):
            continue
        session.add(
            ChangeLog(
                change_item_id=item.id,
                action="prepare",
                screenshot_before=screenshot_before,
                screenshot_after=screenshot_after,
                result="prepared",
                details={
                    "submitted": False,
                    "published": False,
                    "mode": "prepared",
                    "approval": "required",
                    "before": plan.before,
                    "after": plan.after,
                    "evidence": evidence,
                },
                at=now(),
            )
        )


def ensure_review_task(
    session: Session,
    order_id: int,
    fields: Sequence[Mapping[str, Any]],
    evidence: str | None,
    *,
    reason: str = "yandex_listing_review",
    platform: str = "yandex",
) -> None:
    rows = session.scalars(
        select(HumanTask).where(
            HumanTask.object_type == "order",
            HumanTask.object_id == order_id,
            HumanTask.reason == reason,
            HumanTask.status == "open",
        )
    ).all()
    if rows:
        return
    session.add(
        HumanTask(
            reason=reason,
            object_type="order",
            object_id=order_id,
            details={
                "platform": platform,
                "approval": "required",
                "submitted": False,
                "published": False,
                "fields": list(fields),
                "evidence": evidence,
            },
            status="open",
            created_at=now(),
        )
    )


PREPARED_SUMMARY = "Правки Яндекса подготовлены, форма не отправлена."
PREPARED_SUMMARIES = {
    "yandex": PREPARED_SUMMARY,
    "2gis": "Правки 2ГИС подготовлены, форма не отправлена.",
    "google": "Правки Google подготовлены, форма не отправлена.",
}
CREATE_SUMMARIES = {
    "2gis": "Черновик карточки 2ГИС подготовлен, форма не отправлена.",
    "yandex": "Черновик карточки Яндекса подготовлен, форма не отправлена.",
    "google": "Черновик карточки Google подготовлен, форма не отправлена.",
}


def ensure_listing_task(
    session: Session,
    order_id: int,
    platform: str,
    reason: str,
    detail: str,
    evidence: str | None,
) -> None:
    """One open human task per order, platform, and reason. A repeat does not add another row."""
    rows = session.scalars(
        select(HumanTask).where(
            HumanTask.object_type == "order",
            HumanTask.object_id == order_id,
            HumanTask.reason == reason,
            HumanTask.status == "open",
        )
    ).all()
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        if details.get("platform") == platform:
            return
    session.add(
        HumanTask(
            reason=reason,
            object_type="order",
            object_id=order_id,
            details={"platform": platform, "detail": detail, "submitted": False, "published": False, "evidence": evidence},
            status="open",
            created_at=now(),
        )
    )


def _logged(session: Session, item_id: int) -> bool:
    row = session.scalar(
        select(ChangeLog.id).where(
            ChangeLog.change_item_id == item_id,
            ChangeLog.action == "apply",
            ChangeLog.result.in_(("dry_run", "already")),
        )
    )
    return row is not None
