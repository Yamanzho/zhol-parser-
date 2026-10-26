"""Read-only views for the local admin panel.

Queries stay bounded. Stored evidence is labeled as stored evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from zhol.agents.scout.batch import progress_counts
from zhol.agents.scout.collection_outcome import collection_mode, collection_outcome, stop_reason_code
from zhol.agents.scout.run_timing import public_timings
from zhol.api.company_view import CONTACT_HINT, company_cards, identity_view, list_item, public_http
from zhol.audit.bek import PROMPT as BEK_PROMPT
from zhol.audit.bek import read_company_plan
from zhol.content.prepare import content_status, list_content
from zhol.db.models import (
    Audit,
    CardSnapshot,
    ChangeItem,
    ChangeLog,
    ChangePackage,
    CollectionBatch,
    CollectionBatchItem,
    Company,
    CompanyPlatformId,
    ContentItem,
    HumanTask,
    Intake,
    ListingApproval,
    ListingImprovementPlan,
    ListingSubmission,
    Order,
    Report,
)
from zhol.orders.intake import check_readiness, order_status
from zhol.redact import strip_secrets

CARD_KEYS = (
    "name",
    "address",
    "district",
    "category",
    "city",
    "rating",
    "ratings_count",
    "reviews_count",
    "reviews_shown",
    "replies_count",
    "photos_count",
    "news_count",
    "prices",
    "services",
    "website",
    "link",
    "phones",
    "whatsapp",
    "instagram",
    "email",
    "owner_claimed",
    "owner_maintains",
    "has_phone",
    "has_whatsapp",
    "has_instagram",
    "hours",
    "description",
    "last_review_date",
    "online_booking",
    "is_ad",
    "branches",
)
LISTING_PHASE = {
    "draft": "draft",
    "prepared": "draft",
    "awaiting_approval": "draft",
    "approved": "draft",
    "submitting": "submitted",
    "submitted": "submitted",
    "moderation_pending": "moderation",
    "published": "verified",
}
PHASE_LABEL = {
    "draft": "Draft. Not submitted.",
    "submitted": "Submitted in stored evidence. Not a live publication.",
    "moderation": "Moderation status from stored evidence. Not a live check.",
    "verified": "Matched stored evidence. Not a live publication.",
    "rejected": "Rejected in stored evidence.",
    "uncertain": "Stored result is uncertain.",
    "human_required": "Needs a person.",
}
ITEM_CAP = 200


def envelope(**payload: Any) -> dict[str, Any]:
    body = {
        "record_source": "database",
        "live_publication": False,
        "live_mass_discovery": False,
        "bek_connected": False,
    }
    body.update(payload)
    return body


def page_body(total: int, limit: int, offset: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    return envelope(total=total, limit=limit, offset=offset, items=items)


def summary(session: Session) -> dict[str, Any]:
    listing_rows = session.execute(
        select(ListingSubmission.status, func.count()).group_by(ListingSubmission.status)
    ).all()
    phases: dict[str, int] = {}
    for status, count in listing_rows:
        phase = listing_phase(str(status))
        phases[phase] = phases.get(phase, 0) + int(count)
    modes = {
        str(mode): int(count)
        for mode, count in session.execute(
            select(CollectionBatch.source_mode, func.count()).group_by(CollectionBatch.source_mode)
        ).all()
    }
    return envelope(
        counts={
            "companies": _count(session, Company),
            "orders": _count(session, Order),
            "scout_batches": _count(session, CollectionBatch),
            "audits": _count(session, Audit),
            "plans": _count(session, ListingImprovementPlan),
            "content_items": _count(session, ContentItem),
            "listing_submissions": _count(session, ListingSubmission),
            "open_human_tasks": int(
                session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.status == "open")) or 0
            ),
            "reports": _count(session, Report),
        },
        scout_source_modes=modes,
        listing_phases=phases,
        note="Counts are rows in this database. They are not production statistics.",
    )


def list_companies(session: Session, *, q: str, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(Company)
    term = q.strip()
    if term:
        stmt = stmt.where(
            or_(
                Company.name.ilike(_like(term), escape="\\"),
                Company.city.ilike(_like(term), escape="\\"),
                Company.id_2gis.ilike(_like(term), escape="\\"),
                Company.niche.ilike(_like(term), escape="\\"),
                Company.id.in_(
                    select(CompanyPlatformId.company_id).where(
                        CompanyPlatformId.external_id.ilike(_like(term), escape="\\")
                    )
                ),
            )
        )
    total, rows = _paginate(session, stmt, Company.id.desc(), limit, offset)
    grouped = _snapshots_for(session, [row.id for row in rows])
    platforms = _platform_ids_for(session, [row.id for row in rows])
    body = page_body(
        total,
        limit,
        offset,
        [list_item(row, grouped.get(row.id, []), platforms.get(row.id, [])) for row in rows],
    )
    body["contact_hint"] = CONTACT_HINT
    body["unresolved_matches"] = _unresolved_matches(session)
    return body


def company_detail(session: Session, company_id: int) -> dict[str, Any] | None:
    company = session.get(Company, company_id)
    if company is None:
        return None
    platforms = session.scalars(
        select(CompanyPlatformId).where(CompanyPlatformId.company_id == company_id).order_by(CompanyPlatformId.platform)
    ).all()
    total = int(
        session.scalar(select(func.count()).select_from(CardSnapshot).where(CardSnapshot.company_id == company_id)) or 0
    )
    snapshots = session.scalars(
        select(CardSnapshot)
        .where(CardSnapshot.company_id == company_id)
        .order_by(CardSnapshot.collected_at.desc(), CardSnapshot.id.desc())
        .limit(100)
    ).all()
    audit = session.scalar(select(Audit).where(Audit.company_id == company_id).order_by(Audit.id.desc()))
    from zhol.api.company_workspace import company_workspace

    cards = company_cards(company, list(snapshots), audit)
    cards["header"].update(identity_view(company, list(snapshots), list(platforms)))
    return envelope(
        company={
            "id": company.id,
            "id_2gis": company.id_2gis,
            "id_2gis_label": company.id_2gis or "Номер 2ГИС не указан",
            "name": company.name,
            "city": company.city,
            "niche": company.niche,
            "whatsapp": company.whatsapp,
            "instagram": company.instagram,
            "dnc": company.dnc,
            "contact_label": list_item(company, list(snapshots), list(platforms))["contact_label"],
            "source": company.source,
            "created_at": _iso(company.created_at),
        },
        platforms=[
            {
                "platform": row.platform,
                "external_id": row.external_id,
                "public_url": public_http(row.public_url),
                "access_status": row.access_status,
            }
            for row in platforms
        ],
        snapshots=[_snapshot(row) for row in snapshots],
        history_truncated=total > len(snapshots),
        **cards,
        workspace=company_workspace(session, company_id, audit),
    )


def list_audits(session: Session, *, company_id: int | None, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(Audit, Company.name).join(Company, Company.id == Audit.company_id, isouter=True)
    if company_id is not None:
        stmt = stmt.where(Audit.company_id == company_id)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.execute(stmt.order_by(Audit.id.desc()).limit(limit).offset(offset)).all()
    items = []
    for audit, company_name in rows:
        items.append(
            {
                "id": audit.id,
                "company_id": audit.company_id,
                "company_name": company_name,
                "status": audit.status,
                "verification_status": audit.status,
                "score_total": audit.score_total,
                "source": "bek_workbook" if audit.prompt_version == BEK_PROMPT else "stored_audit",
                "prompt_version": audit.prompt_version,
                "model": audit.model,
                "created_at": _iso(audit.created_at),
            }
        )
    return page_body(total, limit, offset, items)


def audit_detail(session: Session, audit_id: int) -> dict[str, Any] | None:
    audit = session.get(Audit, audit_id)
    if audit is None:
        return None
    company = session.get(Company, audit.company_id) if audit.company_id is not None else None
    plan = read_company_plan(session, audit.company_id) if audit.company_id is not None else None
    return envelope(
        audit={
            "id": audit.id,
            "company_id": audit.company_id,
            "company_name": None if company is None else company.name,
            "status": audit.status,
            "verification_status": audit.status,
            "score_total": audit.score_total,
            "score_groups": strip_secrets(audit.score_groups),
            "source": "bek_workbook" if audit.prompt_version == BEK_PROMPT else "stored_audit",
            "prompt_version": audit.prompt_version,
            "model": audit.model,
            "snapshot_ids": list(audit.snapshot_ids or [])[:50],
            "observations": _clip(audit.observations),
            "gaps": _clip(audit.gaps),
            "discrepancies": _clip(audit.discrepancies),
            "created_at": _iso(audit.created_at),
        },
        plan=None if plan is None else _public_plan(plan),
    )


def list_batches(session: Session, *, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(CollectionBatch)
    total, rows = _paginate(session, stmt, CollectionBatch.id.desc(), limit, offset)
    return page_body(total, limit, offset, [_batch(session, row) for row in rows])


def batch_detail(session: Session, batch_id: int, *, limit: int, offset: int) -> dict[str, Any] | None:
    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        return None
    stmt = select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)
    total, rows = _paginate(session, stmt, CollectionBatchItem.id, limit, offset)
    body = _batch(session, batch)
    freshness = _freshness(session, [row.company_id for row in rows if row.company_id is not None])
    body["items"] = [
        {
            "id": row.id,
            "id_2gis": row.id_2gis,
            "company_id": row.company_id,
            "status": row.status,
            "attempts": row.attempts,
            "card_state": _item_state(row),
            "verification_status": freshness.get(row.company_id or 0, {}).get("verification_status"),
            "collected_at": freshness.get(row.company_id or 0, {}).get("collected_at"),
            "detail": _public_batch_detail(row.detail),
        }
        for row in rows
    ]
    body["item_total"] = total
    body["limit"] = limit
    body["offset"] = offset
    return body


def list_orders(session: Session, *, stage: str | None, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(Order)
    if stage:
        stmt = stmt.where(Order.stage == stage)
    total, rows = _paginate(session, stmt, Order.id.desc(), limit, offset)
    items = []
    for order in rows:
        ready = check_readiness(session, order.id)
        company = session.get(Company, order.company_id) if order.company_id is not None else None
        items.append(
            {
                "order_id": order.id,
                "stage": order.stage,
                "kind": order.kind,
                "branches": order.branches,
                "amount_kzt": order.amount_kzt,
                "company_id": order.company_id,
                "company_name": None if company is None else company.name,
                "readiness": None if ready is None else ready.status,
            }
        )
    return page_body(total, limit, offset, items)


def order_detail(session: Session, order_id: int) -> dict[str, Any] | None:
    order = session.get(Order, order_id)
    if order is None:
        return None
    company = session.get(Company, order.company_id) if order.company_id is not None else None
    view = order_status(session, order_id)
    ready = check_readiness(session, order_id)
    drafts = content_status(session, order_id)
    return envelope(
        order_id=order.id,
        stage=order.stage,
        kind=order.kind,
        branches=order.branches,
        amount_kzt=order.amount_kzt,
        company_id=order.company_id,
        company_name=None if company is None else company.name,
        status=None if view is None else view.as_dict(),
        readiness=None if ready is None else ready.as_dict(),
        content=None
        if drafts is None
        else {
            "order_id": drafts.order_id,
            "skipped_reviews": drafts.skipped_reviews,
            "items": list(drafts.rows),
            "publication": "not_published",
        },
    )


def content_page(session: Session, *, order_id: int | None, status: str | None, limit: int, offset: int) -> dict[str, Any]:
    if order_id is not None and session.get(Order, order_id) is None:
        return {"missing": "order"}
    total, items = list_content(session, order_id=order_id, status=status, limit=limit, offset=offset)
    shown = []
    for item in items:
        row = dict(item)
        row["facts"] = strip_secrets(row.get("facts"))
        row["text"] = strip_secrets(row.get("text"))
        shown.append(row)
    return page_body(total, limit, offset, shown)


def list_listings(session: Session, *, order_id: int | None, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(ChangePackage)
    if order_id is not None:
        stmt = stmt.where(ChangePackage.order_id == order_id)
    total, packages = _paginate(session, stmt, ChangePackage.id.desc(), limit, offset)
    package_ids = [row.id for row in packages]
    items = _for_packages(session, ChangeItem, ChangeItem.package_id, package_ids, ChangeItem.n)
    submissions = _for_packages(session, ListingSubmission, ListingSubmission.package_id, package_ids, ListingSubmission.id)
    approvals = _for_packages(session, ListingApproval, ListingApproval.package_id, package_ids, ListingApproval.id)
    item_ids = [row.id for row in items]
    prepared_ids = _prepared_item_ids(session, item_ids)
    checklists = _order_checklists(session, [row.order_id for row in packages if row.order_id is not None])
    grouped: list[dict[str, Any]] = []
    for package in packages:
        own_items = [row for row in items if row.package_id == package.id][:50]
        own_submissions = [row for row in submissions if row.package_id == package.id][:20]
        own_approvals = [row for row in approvals if row.package_id == package.id][:20]
        grouped.append(
            {
                "package_id": package.id,
                "order_id": package.order_id,
                "version": package.version,
                "status": package.status,
                "phase": listing_phase(package.status),
                "phase_label": PHASE_LABEL.get(listing_phase(package.status), package.status),
                "targets": _listing_targets(
                    checklists.get(package.order_id),
                    own_items,
                    own_submissions,
                    own_approvals,
                    prepared_ids,
                ),
                "items": [
                    {
                        "id": row.id,
                        "n": row.n,
                        "platform": row.platform,
                        "field": row.field,
                        "before": strip_secrets(row.before),
                        "after": strip_secrets(row.after),
                        "source_ref": strip_secrets(row.source_ref),
                        "status": row.status,
                        "phase": listing_phase(row.status),
                    }
                    for row in own_items
                ],
                "submissions": [_submission(row) for row in own_submissions],
                "approvals": [
                    {
                        "id": row.id,
                        "platform": row.platform,
                        "operation": row.operation,
                        "status": row.status,
                        "approved_at": _iso(row.approved_at),
                        "expires_at": _iso(row.expires_at),
                    }
                    for row in own_approvals
                ],
            }
        )
    body = page_body(total, limit, offset, grouped)
    body["truncated"] = len(items) >= ITEM_CAP or len(submissions) >= ITEM_CAP
    return body


def list_tasks(session: Session, *, status: str | None, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(HumanTask)
    if status:
        stmt = stmt.where(HumanTask.status == status)
    total, rows = _paginate(session, stmt, HumanTask.id.desc(), limit, offset)
    return page_body(
        total,
        limit,
        offset,
        [
            {
                "id": row.id,
                "reason": row.reason,
                "object_type": row.object_type,
                "object_id": row.object_id,
                "status": row.status,
                "details": strip_secrets(row.details),
                "resolution": None if row.resolution is None else strip_secrets(row.resolution),
                "created_at": _iso(row.created_at),
                "closed_at": _iso(row.closed_at),
            }
            for row in rows
        ],
    )


def list_reports(session: Session, *, limit: int, offset: int) -> dict[str, Any]:
    stmt = select(Report)
    total, rows = _paginate(session, stmt, Report.id.desc(), limit, offset)
    return page_body(total, limit, offset, [_report(row) for row in rows])


def report_detail(session: Session, report_id: int) -> dict[str, Any] | None:
    row = session.get(Report, report_id)
    if row is None:
        return None
    return envelope(report=_report(row), recomputed=False)


def listing_phase(status: str) -> str:
    return LISTING_PHASE.get(status, status)


def _prepared_item_ids(session: Session, item_ids: list[int]) -> set[int]:
    if not item_ids:
        return set()
    return set(
        session.scalars(
            select(ChangeLog.change_item_id).where(
                ChangeLog.change_item_id.in_(item_ids),
                ChangeLog.action == "prepare",
                ChangeLog.result == "prepared",
            )
        ).all()
    )


def _order_checklists(session: Session, order_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not order_ids:
        return {}
    rows = session.scalars(select(Intake).where(Intake.order_id.in_(order_ids))).all()
    return {row.order_id: row.checklist for row in rows if isinstance(row.checklist, dict)}


def _listing_targets(
    checklist: dict[str, Any] | None,
    items: list[ChangeItem],
    submissions: list[ListingSubmission],
    approvals: list[ListingApproval],
    prepared_ids: set[int],
) -> list[dict[str, Any]]:
    actions: dict[str, str] = {}
    raw = checklist.get("platforms") if isinstance(checklist, dict) else None
    if isinstance(raw, dict):
        for name, item in raw.items():
            if isinstance(item, dict):
                action = str(item.get("action") or "update")
                actions[str(name)] = action if action in {"create", "update"} else "update"
    names = list(actions)
    for row in items:
        if row.platform not in names:
            names.append(row.platform)
    targets: list[dict[str, Any]] = []
    for name in names:
        operation = actions.get(name) or next((row.operation for row in submissions if row.platform == name), "update")
        approval = next((row for row in approvals if row.platform == name and row.operation == operation), None)
        submission = next((row for row in submissions if row.platform == name and row.operation == operation), None)
        platform_items = [row for row in items if row.platform == name]
        targets.append(
            {
                "platform": name,
                "operation": operation,
                "preparation": "prepared" if any(row.id in prepared_ids for row in platform_items) else "not_prepared",
                "approval": "none" if approval is None else approval.status,
                "submission": "none" if submission is None else submission.status,
                "published": False,
                "live_publication": False,
            }
        )
    return targets


def _report(row: Report) -> dict[str, Any]:
    data = row.data if isinstance(row.data, dict) else {}
    stored_live = data.get("live_verified")
    return {
        "id": row.id,
        "order_id": row.order_id,
        "period": row.period,
        "status": row.status,
        "summary": None if row.summary is None else strip_secrets(row.summary),
        "has_pdf": bool(row.pdf_path),
        "has_xlsx": bool(row.xlsx_path),
        "stored_live_verified": stored_live if isinstance(stored_live, bool) else None,
        "live_publication": False,
        "verification_mode": data.get("verification_mode") or "unknown",
        "outcome": data.get("outcome"),
        "completed": data.get("completed") if isinstance(data.get("completed"), bool) else None,
        "recomputed": False,
    }


def _submission(row: ListingSubmission) -> dict[str, Any]:
    phase = listing_phase(row.status)
    evidence = row.evidence if isinstance(row.evidence, dict) else {}
    mode = evidence.get("mode")
    if mode not in {"offline", "dry_run", "prepared", "fixture", "stored"}:
        mode = "offline"
    return {
        "id": row.id,
        "platform": row.platform,
        "operation": row.operation,
        "status": row.status,
        "phase": phase,
        "phase_label": PHASE_LABEL.get(phase, row.status),
        "moderation_status": row.moderation_status,
        "evidence_mode": mode,
        "live_publication": False,
        "submitted_at": _iso(row.submitted_at),
        "detail": None if row.detail is None else strip_secrets(row.detail),
    }


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    body = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
    tasks = []
    for lane in ("automatic_preparation", "manual_work"):
        for task in body.get(lane) or []:
            if not isinstance(task, dict):
                continue
            tasks.append(
                {
                    "type": task.get("type"),
                    "platform": task.get("platform"),
                    "field": task.get("field"),
                    "status": task.get("status"),
                    "lane": task.get("lane") or lane,
                    "completed": task.get("completed") is True,
                }
            )
            if len(tasks) >= 100:
                break
    platforms = []
    for name, card in (body.get("platforms") or {}).items():
        if not isinstance(card, dict):
            continue
        platforms.append(
            {
                "platform": name,
                "match_status": card.get("match_status"),
                "status": card.get("status"),
                "public_url": card.get("url"),
            }
        )
    return {
        "id": plan.get("id"),
        "company_id": plan.get("company_id"),
        "audit_id": plan.get("audit_id"),
        "source_key": plan.get("source_key"),
        "source": plan.get("source"),
        "display": plan.get("display"),
        "listing_modified": False,
        "created_at": plan.get("created_at"),
        "platforms": platforms,
        "tasks": tasks,
    }


def _freshness(session: Session, company_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not company_ids:
        return {}
    rows = session.scalars(select(CardSnapshot).where(CardSnapshot.company_id.in_(company_ids))).all()
    latest: dict[int, CardSnapshot] = {}
    for row in rows:
        current = latest.get(row.company_id)
        stamp = "" if row.collected_at is None else row.collected_at.isoformat()
        current_stamp = "" if current is None or current.collected_at is None else current.collected_at.isoformat()
        if current is None or (stamp, row.id) >= (current_stamp, current.id):
            latest[row.company_id] = row
    return {
        company_id: {"verification_status": row.status, "collected_at": _iso(row.collected_at)}
        for company_id, row in latest.items()
    }


def collection_result(
    status: str,
    stored: dict[str, Any],
    card_states: dict[str, int],
    *,
    collected: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    rejected = card_states.get("rejected", 0)
    return collection_outcome(
        status,
        stored,
        collected=collected,
        limit=limit,
        rejected=rejected if isinstance(rejected, int) and not isinstance(rejected, bool) else 0,
    )


def collection_display_status(
    status: str,
    stored: dict[str, Any],
    card_states: dict[str, int],
    *,
    collected: int = 0,
    limit: int | None = None,
) -> str:
    """Panel status. Partial success is not full success, and the requested limit is not an error."""
    return str(collection_result(status, stored, card_states, collected=collected, limit=limit)["code"])


def _public_stop_reason(stored: dict[str, Any]) -> str | None:
    return stop_reason_code(stored.get("stop_reason"))


def _public_collection_plan(stored: dict[str, Any], row: CollectionBatch) -> dict[str, Any] | None:
    plan = stored.get("plan")
    if not isinstance(plan, dict):
        return None
    combinations = [item for item in plan.get("combinations") or [] if isinstance(item, dict)]
    if not combinations:
        return None
    def count(status: str) -> int:
        return sum(1 for item in combinations if item.get("status") == status)

    platform = plan.get("platform")
    if not isinstance(platform, str):
        platform = row.platforms[0] if row.platforms else ""
    return {
        "total": len(combinations),
        "completed": count("completed"),
        "pending": count("pending"),
        "interrupted": count("interrupted") + count("paused") + count("stopped"),
        "skipped": count("skipped_cap"),
        "current_city": plan.get("current_city") or row.city,
        "current_query": plan.get("current_query") or stored.get("active_query") or row.category,
        "current_source": platform,
        "cursor": plan.get("cursor") if isinstance(plan.get("cursor"), int) else 0,
    }


def _batch(session: Session, row: CollectionBatch) -> dict[str, Any]:
    counts = progress_counts(session, row.id)
    stored = row.progress if isinstance(row.progress, dict) else {}
    states = _card_states(session, row.id)
    job_id = stored.get("admin_job_id")
    result = collection_result(row.status, stored, states, collected=int(counts.get("completed") or 0), limit=row.item_limit)
    return envelope(
        id=row.id,
        job_id=job_id if isinstance(job_id, int) else None,
        city=row.city,
        category=row.category,
        platforms=list(row.platforms or []),
        recorded_source_mode=row.source_mode,
        status=row.status,
        item_limit=row.item_limit,
        progress=counts,
        active_query=stored.get("active_query") if isinstance(stored.get("active_query"), str) and stored.get("active_query") else row.category,
        outcome=_outcome(session, row, counts, stored),
        created_at=_iso(row.created_at),
        finished_at=_iso(row.finished_at),
        error=stored.get("error") if isinstance(stored.get("error"), str) else None,
        optional_screenshot=_optional_screenshot(stored),
        note=_batch_note(row.source_mode),
        metrics=_public_metrics(stored),
        card_states=states,
        interruption=_public_interruption(stored),
        retry_pending=stored.get("retry_pending") is True,
        manual_verification=_public_verification(stored),
        display_status=result["code"],
        result=result,
        collection_mode=collection_mode(row.source_mode, stored),
        timings=public_timings(stored.get("timings")),
        duration_seconds=_duration_seconds(row.created_at, row.finished_at),
        stop_reason=_public_stop_reason(stored),
        collection_plan=_public_collection_plan(stored, row),
    )


def _duration_seconds(started: Any, finished: Any) -> float | None:
    if started is None or finished is None:
        return None
    return round(max(0.0, (finished - started).total_seconds()), 1)


_LOCAL_PATH_KEYS = frozenset(
    {
        "source_path",
        "screenshot_path",
        "raw_path",
        "catalog_path",
        "fixtures_dir",
        "out_path",
        "page_file",
    }
)


def _public_batch_detail(value: object) -> object:
    """Batch item facts for the panel. Local file paths stay on the server."""
    return _drop_local_paths(strip_secrets(value))


def _drop_local_paths(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return None
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            if str(key) in _LOCAL_PATH_KEYS:
                continue
            cleaned[str(key)] = _drop_local_paths(item, depth=depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_drop_local_paths(item, depth=depth + 1) for item in value]
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        return None
    return value


_METRIC_KEYS = (
    "companies_processed",
    "companies_committed",
    "duplicate_companies",
    "snapshots_written",
    "failed_records",
    "average_persistence_ms",
    "browser_restarts",
    "retries",
)
_TEMPORARY_CLASSES = frozenset({"page_timeout", "network_failure", "browser_session"})


def _public_metrics(stored: dict[str, Any]) -> dict[str, int | float] | None:
    """Copy stored collector counters. Missing keys stay missing."""
    raw = stored.get("metrics")
    if not isinstance(raw, dict):
        return None
    shown: dict[str, int | float] = {}
    for key in _METRIC_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        shown[key] = value
    return shown or None


def _public_verification(stored: dict[str, Any]) -> dict[str, Any] | None:
    raw = stored.get("manual_verification")
    if not isinstance(raw, dict):
        return None
    outcome = raw.get("outcome")
    if outcome not in {"pending", "passed", "failed", "not_permitted", "unavailable"}:
        return None
    scope = raw.get("authorization_scope")
    if scope not in {"local", "commercial"}:
        scope = None
    source_kind = raw.get("source_kind")
    if source_kind not in {"live_2gis", "localhost", "local_html", "browser_yandex", "browser_google"}:
        source_kind = None
    message = raw.get("message")
    return {
        "outcome": outcome,
        "browser": "open" if raw.get("browser") == "open" else "closed",
        "authorization_scope": scope,
        "source_kind": source_kind,
        "message": message if isinstance(message, str) else "",
    }


def _public_interruption(stored: dict[str, Any]) -> dict[str, Any] | None:
    raw = stored.get("interruption")
    if not isinstance(raw, dict):
        return None
    classification = raw.get("classification")
    reason = raw.get("reason")
    if not isinstance(classification, str):
        return None
    return {
        "classification": classification,
        "retryable": raw.get("retryable") is True,
        "reason": reason if isinstance(reason, str) else "",
    }


def _item_state(row: CollectionBatchItem) -> str | None:
    detail = row.detail if isinstance(row.detail, dict) else {}
    if row.status == "completed":
        return "existed" if detail.get("reason") == "already_present" else "saved"
    if row.status in {"pending", "in_progress"} and detail.get("retryable") is True:
        if detail.get("classification") in _TEMPORARY_CLASSES:
            return "retry_wait"
    if row.status in {"failed", "blocked"}:
        return "rejected"
    return None


def _card_states(session: Session, batch_id: int) -> dict[str, int]:
    counts = {"saved": 0, "existed": 0, "retry_wait": 0, "rejected": 0}
    rows = session.scalars(select(CollectionBatchItem).where(CollectionBatchItem.batch_id == batch_id)).all()
    for row in rows:
        state = _item_state(row)
        if state in counts:
            counts[state] += 1
    return counts


def _optional_screenshot(stored: dict[str, Any]) -> str | None:
    raw = stored.get("optional_screenshot")
    status = raw.get("status") if isinstance(raw, dict) else None
    if status in {"present", "missing", "unreadable"}:
        return status
    return None


def _batch_note(mode: str) -> str:
    if mode == "live_2gis":
        return "Сбор через Catalog API 2ГИС. Сайт 2ГИС не открывается."
    if mode == "browser_yandex":
        from zhol.agents.scout.browser_yandex import browser_mode

        return str(browser_mode()["note"])
    if mode == "browser_google":
        from zhol.agents.scout.browser_google import browser_mode

        return str(browser_mode()["note"])
    if mode == "browser_2gis":
        return (
            "Существующий сборщик публичных карточек 2ГИС. Ключ Catalog API не используется. "
            "Сайт 2ГИС открывается только после письменного разрешения. "
            "Локальный HTML и localhost это разрешение не требуют."
        )
    if mode == "manual":
        return "Импорт сохранённых страниц. Живой сайт не открывался."
    if mode == "fixture":
        return "Тестовый каталог. Живой сайт не открывался."
    return "Живой обход площадок не выполнялся."


def _outcome(session: Session, row: CollectionBatch, counts: dict[str, int], stored: dict[str, Any]) -> dict[str, Any]:
    """Count companies already stored. This does not insert or skip a row."""
    linked = session.execute(
        select(CollectionBatchItem.company_id).where(
            CollectionBatchItem.batch_id == row.id,
            CollectionBatchItem.company_id.is_not(None),
        )
    ).all()
    ids = list({company_id for (company_id,) in linked})
    created: dict[int, datetime | None] = {}
    if ids:
        for company_id, stamp in session.execute(select(Company.id, Company.created_at).where(Company.id.in_(ids))):
            created[int(company_id)] = stamp
    added = 0
    already = 0
    for company_id in ids:
        stamp = created.get(company_id)
        if row.created_at is not None and stamp is not None and stamp >= row.created_at:
            added += 1
        else:
            already += 1
    skipped = stored.get("skipped")
    duplicates = stored.get("duplicates")
    return {
        "discovered": counts["total"],
        "added": added,
        "already_present": already,
        "failed": counts["failed"],
        "skipped": len(skipped) if isinstance(skipped, list) else None,
        "duplicates": duplicates if isinstance(duplicates, int) else None,
    }


def _snapshot(row: CardSnapshot) -> dict[str, Any]:
    fields = row.fields if isinstance(row.fields, dict) else {}
    sources = row.field_sources if isinstance(row.field_sources, dict) else {}
    shown = {key: fields.get(key) for key in CARD_KEYS if key in fields}
    return {
        "id": row.id,
        "platform": row.platform,
        "match_status": row.match_status,
        "verification_status": row.status,
        "collector": row.collected_by,
        "purpose": row.purpose,
        "collected_at": _iso(row.collected_at),
        "public_url": public_http(row.url),
        "screenshot_stored": bool(row.screenshot_path),
        "fields": strip_secrets(shown),
        "field_sources": {key: "sourced" if _sourced(sources, key) else "missing" for key in shown},
    }


def _sourced(sources: dict[str, Any], key: str) -> bool:
    value = sources.get(key)
    if value is None:
        return False
    if isinstance(value, dict):
        return any(value.get(name) for name in ("url", "seen_at", "page", "source"))
    return True


def _for_packages(session: Session, model: Any, column: Any, package_ids: list[int], order: Any) -> list[Any]:
    if not package_ids:
        return []
    return list(
        session.scalars(select(model).where(column.in_(package_ids)).order_by(order).limit(ITEM_CAP)).all()
    )


def _paginate(session: Session, stmt: Any, order: Any, limit: int, offset: int) -> tuple[int, list[Any]]:
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(session.scalars(stmt.order_by(order).limit(limit).offset(offset)).all())
    return total, rows


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _snapshots_for(session: Session, company_ids: list[int]) -> dict[int, list[CardSnapshot]]:
    if not company_ids:
        return {}
    grouped: dict[int, list[CardSnapshot]] = {}
    rows = session.scalars(select(CardSnapshot).where(CardSnapshot.company_id.in_(company_ids))).all()
    for row in rows:
        if row.company_id is None:
            continue
        grouped.setdefault(row.company_id, []).append(row)
    return grouped


def _platform_ids_for(session: Session, company_ids: list[int]) -> dict[int, list[CompanyPlatformId]]:
    if not company_ids:
        return {}
    grouped: dict[int, list[CompanyPlatformId]] = {}
    rows = session.scalars(select(CompanyPlatformId).where(CompanyPlatformId.company_id.in_(company_ids))).all()
    for row in rows:
        grouped.setdefault(row.company_id, []).append(row)
    return grouped


def _unresolved_matches(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(CollectionBatchItem)
        .where(CollectionBatchItem.status == "blocked", CollectionBatchItem.company_id.is_(None))
        .order_by(CollectionBatchItem.id.desc())
        .limit(30)
    ).all()
    found = []
    for row in rows:
        detail = row.detail or {}
        if detail.get("reason") not in {"ambiguous", "platform_id_conflict"}:
            continue
        platform = str(detail.get("platform") or "")
        reason = str(detail.get("reason") or "")
        found.append(
            {
                "item_key": row.id_2gis,
                "name": detail.get("name") or "Без названия",
                "platform": platform,
                "address": detail.get("address") or "Адрес не указан",
                "reason_label": "Конфликт номера площадки" if reason == "platform_id_conflict" else "Нужна проверка",
            }
        )
    return found


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _clip(value: Any) -> Any:
    cleaned = strip_secrets(value)
    if isinstance(cleaned, list):
        return cleaned[:20]
    if isinstance(cleaned, dict):
        return {key: cleaned[key] for key in list(cleaned)[:20]}
    return cleaned
