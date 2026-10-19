"""Fulfillment check and client completion report.

A Save or Create click is not publication. A field is verified only when a
later public snapshot, stored with its own source and screenshot, shows the
approved value. This module does not open a listing page.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.db.models import (
    Audit,
    CardSnapshot,
    ChangeItem,
    ChangeLog,
    ChangePackage,
    Company,
    CompanyProfile,
    ContentItem,
    HumanTask,
    Intake,
    ListingApproval,
    ListingImprovementPlan,
    ListingSubmission,
    Order,
    Report,
)
from zhol.fulfillment.compare import same_value
from zhol.fulfillment.facts import approved_fact
from zhol.guards import no_long_dash
from zhol.redact import redact
from zhol.timeutil import now

PERIOD = "setup"
KIND = "completion"
MODE = "stored_offline"
EVIDENCE_MAX_AGE = timedelta(days=14)
PLATFORM_ORDER = ("2gis", "yandex", "google")
FULFILLMENT = frozenset(
    {
        "not_submitted",
        "submitted",
        "moderation_pending",
        "published_verified",
        "rejected",
        "uncertain",
        "human_required",
    }
)
CLICK_ACTIONS = frozenset({"apply", "create_card", "prepare"})
DRAFT_RESULTS = frozenset({"dry_run", "prepared"})
DESCRIPTION_KINDS = frozenset({"company_description", "platform_description", "service_description", "description"})
ILLUSTRATION_KINDS = frozenset({"cover", "promo_banner", "price_image"})
ILLUSTRATION_LABEL = "Иллюстрация, не фотография компании."
SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "ciphertext",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "session",
        "login_hint",
    }
)
ISSUE_ORDER = (
    "unsupported_claim",
    "stale_evidence",
    "missing_screenshot",
    "incomplete_evidence",
    "changed_package_version",
    "changed_content_version",
    "publication_not_independently_confirmed",
    "evidence_without_submission",
    "rejected_but_snapshot_matches",
    "partial_fields",
)
FACT_PATH = {
    "link": "website",
    "website": "website",
    "whatsapp": "messengers.whatsapp",
    "instagram": "socials.instagram",
}
FIELD_ALIASES = {
    "link": ("link", "website"),
    "website": ("website", "link"),
    "phones": ("phones", "phone"),
}
STATUS_LABELS = {
    "not_submitted": "не отправлено",
    "submitted": "отправлено, публикация не подтверждена",
    "moderation_pending": "на модерации",
    "published_verified": "совпало с сохраненным снимком",
    "rejected": "отклонено",
    "uncertain": "неясно",
    "human_required": "нужен человек",
}
BANNER = (
    "Это отчет по сохраненным данным. Живая проверка карточек не выполнялась. "
    "Черновик и пробный прогон не считаются выполненной работой. "
    "Нажатие Сохранить или Создать не означает публикацию."
)


class CompletionError(ValueError):
    pass


def verify_order(session: Session, order_id: int) -> dict[str, Any]:
    """Read stored records and classify each target platform. Nothing is submitted."""
    order = session.get(Order, order_id)
    if order is None or order.company_id is None:
        raise CompletionError("order not found")
    company = session.get(Company, order.company_id)
    if company is None:
        raise CompletionError("order not found")
    document = _document(session, order, company)
    problems = _quality(document)
    if problems:
        raise CompletionError("completion report failed quality checks: " + ", ".join(problems))
    return document


def generate_completion_report(session: Session, order_id: int, out_dir: str | Path) -> dict[str, Any]:
    """Write JSON and HTML for one order. A second call with the same evidence keeps one row and the same files."""
    fresh = verify_order(session, order_id)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    existing = _find_report(session, order_id)
    if existing is not None and isinstance(existing.data, dict) and existing.data.get("digest") == fresh["digest"]:
        document = json.loads(json.dumps(existing.data, ensure_ascii=False, sort_keys=True))
    else:
        document = fresh
        _save_report(session, order_id, document, existing)
    json_path = folder / "completion.json"
    html_path = folder / "completion.html"
    json_path.write_text(_dumps(document), encoding="utf-8")
    html_path.write_text(render_html(document), encoding="utf-8")
    return {"json": str(json_path), "html": str(html_path), "report": document}


def format_verification(document: dict[str, Any]) -> str:
    lines = [
        f"order {document['order_id']}",
        f"mode {document['verification_mode']}",
        f"live_verified {str(document['live_verified']).lower()}",
        f"completed {str(document['completed']).lower()}",
        f"outcome {document['outcome']}",
    ]
    for platform in document["platforms"]:
        lines.append(f"{platform['platform']} {platform['status']}")
    if document["issues"]:
        lines.append("issues " + ",".join(document["issues"]))
    return "\n".join(lines)


def render_html(document: dict[str, Any]) -> str:
    company = document["company"]
    sections = [
        ("Компания", _company_html(company)),
        ("Площадки", _platforms_html(document["platforms"])),
        ("Пробелы аудита", _rows_html(document["deficiencies"], ("platform", "text"))),
        ("Запрошенные правки", _rows_html(document["requested_changes"], ("platform", "text", "status"))),
        ("Подготовленные правки", _changes_html(document["prepared_changes"])),
        ("Правки, совпавшие со снимком", _changes_html(document["verified_changes"])),
        ("Что осталось", _rows_html(document["outstanding"], ("kind", "text"))),
        ("Что нужно от клиента", _rows_html(document["client_actions"], ("kind", "text"))),
        ("Модерация", _rows_html(document["moderation"], ("platform", "submission_status", "moderation_status"))),
        ("Доказательства", _evidence_html(document["platforms"])),
        ("Описания, услуги и иллюстрации", _content_html(document["content"])),
        ("Время", _times_html(document)),
    ]
    body = "".join(f"<section><h2>{escape(title)}</h2>{html}</section>" for title, html in sections)
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>Отчет по заказу {escape(str(document['order_id']))}</title></head><body>"
        f"<p>{escape(BANNER)}</p>{body}</body></html>\n"
    )


def _document(session: Session, order: Order, company: Company) -> dict[str, Any]:
    profile = _profile(session, company.id)
    intake = session.get(Intake, order.id)
    targets = _targets(intake)
    packages = list(
        session.scalars(
            select(ChangePackage).where(ChangePackage.order_id == order.id).order_by(ChangePackage.version, ChangePackage.id)
        ).all()
    )
    latest = packages[-1] if packages else None
    items = _items(session, [row.id for row in packages])
    logs = _logs(session, [item.id for item in items])
    submissions = list(
        session.scalars(select(ListingSubmission).where(ListingSubmission.order_id == order.id).order_by(ListingSubmission.id)).all()
    )
    approvals = list(
        session.scalars(
            select(ListingApproval).where(ListingApproval.order_id == order.id).order_by(ListingApproval.id)
        ).all()
    )
    snapshots = list(
        session.scalars(
            select(CardSnapshot).where(CardSnapshot.company_id == company.id).order_by(CardSnapshot.collected_at, CardSnapshot.id)
        ).all()
    )
    content = list(
        session.scalars(select(ContentItem).where(ContentItem.order_id == order.id).order_by(ContentItem.id)).all()
    )
    tasks = list(
        session.scalars(
            select(HumanTask)
            .where(HumanTask.object_type == "order", HumanTask.object_id == order.id)
            .order_by(HumanTask.id)
        ).all()
    )
    audit = session.scalar(select(Audit).where(Audit.company_id == company.id).order_by(Audit.id.desc()))
    plan = session.scalar(
        select(ListingImprovementPlan)
        .where(ListingImprovementPlan.company_id == company.id)
        .order_by(ListingImprovementPlan.id.desc())
    )
    platforms = [
        _platform(
            name,
            latest=latest,
            items=[item for item in items if item.platform == name],
            logs=logs,
            submissions=[row for row in submissions if row.platform == name],
            approvals=[row for row in approvals if row.platform == name],
            snapshots=[row for row in snapshots if row.platform == name],
            content=content,
            profile=profile,
        )
        for name in targets
    ]
    deficiencies = _deficiencies(audit, plan)
    requested = _requested(plan)
    prepared = [change for platform in platforms for change in platform["changes"] if change["prepared"] is not None]
    verified = [change for platform in platforms for change in platform["changes"] if change["status"] == "published_verified"]
    content_block, content_issues = _content_block(content, profile)
    task_rows = [_task(row) for row in tasks]
    issues = _ordered(
        [issue for platform in platforms for issue in platform["issues"]]
        + content_issues
        + ([ "evidence_without_submission" ] if _orphan_snapshot(snapshots, submissions) else [])
    )
    open_tasks = [row for row in task_rows if row["status"] == "open"]
    outcome, completed = _outcome([row["status"] for row in platforms], issues, bool(open_tasks))
    actions = _actions(platforms, open_tasks, issues)
    outstanding = _outstanding(platforms, open_tasks, actions)
    moderation = [
        {
            "platform": row["platform"],
            "submission_status": row["submission_status"],
            "moderation_status": row["moderation_status"],
        }
        for row in platforms
    ]
    stamped = now().isoformat()
    summary = f"Заказ {order.id}, итог {outcome}. Живая проверка не выполнялась."
    if not no_long_dash(summary) or not no_long_dash(BANNER):
        raise CompletionError("client text must not use a long dash")
    document: dict[str, Any] = {
        "kind": KIND,
        "order_id": order.id,
        "company": {
            "id": company.id,
            "id_2gis": company.id_2gis,
            "name": company.name,
            "city": company.city,
        },
        "target_platforms": list(targets),
        "generated_at": stamped,
        "verified_at": stamped,
        "live_verified": False,
        "verification_mode": MODE,
        "completed": completed,
        "outcome": outcome,
        "summary": summary,
        "package": None
        if latest is None
        else {"id": latest.id, "version": latest.version, "status": latest.status},
        "deficiencies": deficiencies,
        "requested_changes": requested,
        "prepared_changes": prepared,
        "verified_changes": verified,
        "outstanding": outstanding,
        "client_actions": actions,
        "moderation": moderation,
        "platforms": platforms,
        "content": content_block,
        "issues": issues,
        "limits": {
            "live_check": False,
            "click_is_publication": False,
            "publication_requires": "post_submission_public_snapshot",
            "evidence_max_age_days": 14,
        },
    }
    cleaned = _scrub(document)
    body = {key: value for key, value in cleaned.items() if key not in {"generated_at", "verified_at", "digest"}}
    cleaned["digest"] = hashlib.sha256(_dumps(body).encode()).hexdigest()
    return cleaned


def _platform(
    name: str,
    *,
    latest: ChangePackage | None,
    items: list[ChangeItem],
    logs: dict[int, list[ChangeLog]],
    submissions: list[ListingSubmission],
    approvals: list[ListingApproval],
    snapshots: list[CardSnapshot],
    content: list[ContentItem],
    profile: dict[str, Any],
) -> dict[str, Any]:
    if latest is not None:
        items = [item for item in items if item.package_id == latest.id]
    submission = submissions[-1] if submissions else None
    approval = next((row for row in reversed(approvals) if row.status == "active"), None)
    issues: list[str] = []
    for item in items:
        for log in logs.get(item.id, []):
            details = log.details if isinstance(log.details, dict) else {}
            if details.get("published") is True and (log.action in CLICK_ACTIONS or log.result in DRAFT_RESULTS):
                issues.append("unsupported_claim")
            shot_missing = _missing_file(log.screenshot_before) or _missing_file(log.screenshot_after)
            if shot_missing:
                issues.append("missing_screenshot")
    if submission is not None:
        if _missing_file(submission.screenshot_before) or _missing_file(submission.screenshot_after):
            issues.append("missing_screenshot")
    package_mismatch = _package_mismatch(latest, items, submission, approval)
    if package_mismatch:
        issues.append("changed_package_version")
    stale_content = _stale_for_platform(name, items, content)
    if stale_content:
        issues.append("changed_content_version")
    approved_map = _approved_values(submission, approval)
    base, base_issues = _base_status(submission)
    issues.extend(base_issues)
    candidate = _candidate_snapshot(snapshots, None if submission is None else submission.submitted_at)
    field_rows, evidence_issues, discrepancies, partial = _fields(
        name,
        items,
        logs,
        approved_map,
        candidate,
        profile,
        base,
    )
    issues.extend(evidence_issues)
    status = _combine(base, field_rows, candidate, package_mismatch, stale_content, bool(approved_map))
    if status not in FULFILLMENT:
        status = "uncertain"
        issues.append("incomplete_evidence")
    if status != "published_verified":
        for row in field_rows:
            if row["status"] == "published_verified":
                row["observed_match"] = True
                row["status"] = status
    before = _before_evidence(items, logs, snapshots, None if submission is None else submission.submitted_at)
    submit_evidence = _submit_evidence(submission)
    after = _after_evidence(candidate) if status == "published_verified" else _after_note(candidate, status)
    return {
        "platform": name,
        "operation": None if submission is None else submission.operation,
        "status": status,
        "submission_status": None if submission is None else submission.status,
        "moderation_status": None if submission is None else submission.moderation_status,
        "submission_id": None if submission is None else submission.id,
        "submitted_at": _iso(None if submission is None else submission.submitted_at),
        "package_id": None if submission is None else submission.package_id,
        "package_version": _package_version(submission, approval, latest),
        "live_verified": False,
        "partial": partial,
        "detail": None if submission is None else submission.detail,
        "confirmation": _confirmation(submission),
        "changes": field_rows,
        "evidence": {"before": before, "submit": submit_evidence, "after": after},
        "discrepancies": discrepancies,
        "issues": _ordered(issues),
    }


def _fields(
    platform: str,
    items: list[ChangeItem],
    logs: dict[int, list[ChangeLog]],
    approved_map: dict[str, Any],
    candidate: CardSnapshot | None,
    profile: dict[str, Any],
    base: str,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], bool]:
    issues: list[str] = []
    discrepancies: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    if candidate is not None and base not in {"not_submitted", "human_required"}:
        gate, gate_issues = _snapshot_gate(candidate)
        issues.extend(gate_issues)
    else:
        gate = "absent" if candidate is None else "ignored"
    matched = 0
    unmatched = 0
    for item in sorted(items, key=lambda row: (row.n, row.field)):
        item_logs = logs.get(item.id, [])
        draft = any(log.result in DRAFT_RESULTS or log.action == "prepare" for log in item_logs) and base == "not_submitted"
        approved = approved_map.get(item.field)
        fact = approved_fact(profile, FACT_PATH.get(item.field, item.field))
        original = fact.value if fact.status == "approved" else None
        observed = None
        source = None
        observed_match = False
        field_status = "not_submitted" if base == "not_submitted" else base
        if gate == "ok" and approved is not None and candidate is not None:
            observed, seen_key = _lookup(candidate.fields or {}, item.field)
            source_row, source_ok, source_issue = _source_ok(candidate, item.field)
            matched_value = bool(seen_key) and source_ok and _matches(item.field, approved, observed)
            if base == "rejected":
                field_status = "rejected"
                if matched_value:
                    observed_match = True
                    source = source_row
                    matched += 1
                else:
                    unmatched += 1
            elif not seen_key:
                field_status = "uncertain"
                unmatched += 1
                discrepancies.append({"platform": platform, "field": item.field, "kind": "not_seen", "approved": approved, "observed": None})
            elif not source_ok:
                field_status = "uncertain"
                issues.append(source_issue or "incomplete_evidence")
                unmatched += 1
            elif matched_value:
                field_status = "published_verified"
                source = source_row
                observed_match = True
                matched += 1
            else:
                field_status = "uncertain"
                unmatched += 1
                discrepancies.append(
                    {"platform": platform, "field": item.field, "kind": "differs", "approved": approved, "observed": observed}
                )
        elif gate in {"stale", "missing_screenshot", "unsupported_live", "incomplete"} and approved is not None and base not in {
            "not_submitted",
            "human_required",
            "rejected",
        }:
            field_status = "uncertain"
        prepared = item.after
        rows.append(
            {
                "platform": platform,
                "n": item.n,
                "field": item.field,
                "before": item.before,
                "original_fact": original,
                "prepared": prepared,
                "approved": approved,
                "observed": observed,
                "status": field_status if field_status in FULFILLMENT else "uncertain",
                "source_ref": _source_ref(item.source_ref),
                "draft": draft,
                "observed_match": observed_match,
                "source": source,
            }
        )
    partial = matched > 0 and unmatched > 0
    if partial:
        issues.append("partial_fields")
    if gate == "ok" and base == "rejected" and matched and unmatched == 0 and rows:
        issues.append("rejected_but_snapshot_matches")
        discrepancies.append({"platform": platform, "field": "*", "kind": "rejected_but_snapshot_matches"})
    return rows, issues, discrepancies, partial


def _combine(
    base: str,
    rows: list[dict[str, Any]],
    candidate: CardSnapshot | None,
    package_mismatch: bool,
    stale_content: bool,
    has_approved: bool,
) -> str:
    if package_mismatch or stale_content:
        return "human_required"
    if base in {"not_submitted", "human_required"}:
        return base
    if candidate is None:
        return base
    gate, _issues = _snapshot_gate(candidate)
    if gate != "ok":
        if base == "rejected":
            return "rejected"
        return "uncertain"
    if not has_approved or not rows:
        return "uncertain"
    statuses = [row["status"] for row in rows if row["approved"] is not None]
    if base == "rejected":
        if statuses and all(row.get("observed_match") for row in rows if row["approved"] is not None):
            return "uncertain"
        return "rejected"
    if statuses and all(status == "published_verified" for status in statuses):
        return "published_verified"
    return "uncertain"


def _snapshot_gate(snapshot: CardSnapshot) -> tuple[str, list[str]]:
    if _claims_live(snapshot):
        return "unsupported_live", ["unsupported_claim"]
    age = now() - _aware(snapshot.collected_at)
    if age > EVIDENCE_MAX_AGE:
        return "stale", ["stale_evidence"]
    if snapshot.status != "verified":
        return "incomplete", ["incomplete_evidence"]
    if _missing_file(snapshot.screenshot_path) or not snapshot.screenshot_path:
        return "missing_screenshot", ["missing_screenshot"]
    return "ok", []


def _candidate_snapshot(snapshots: list[CardSnapshot], submitted_at: datetime | None) -> CardSnapshot | None:
    if submitted_at is None:
        return None
    submitted = _aware(submitted_at)
    chosen: CardSnapshot | None = None
    for row in snapshots:
        if row.purpose != "report":
            continue
        if _aware(row.collected_at) <= submitted:
            continue
        if chosen is None or (_aware(row.collected_at), row.id) >= (_aware(chosen.collected_at), chosen.id):
            chosen = row
    return chosen


def _base_status(submission: ListingSubmission | None) -> tuple[str, list[str]]:
    if submission is None:
        return "not_submitted", []
    status = submission.status
    if status in {"prepared", "awaiting_approval", "approved"}:
        return "not_submitted", []
    if status == "published":
        return "uncertain", ["publication_not_independently_confirmed"]
    if status == "submitting":
        return "uncertain", ["incomplete_evidence"]
    if status in FULFILLMENT:
        return status, []
    return "uncertain", ["incomplete_evidence"]


def _package_mismatch(
    latest: ChangePackage | None,
    items: list[ChangeItem],
    submission: ListingSubmission | None,
    approval: ListingApproval | None,
) -> bool:
    anchors: list[tuple[int | None, Any]] = []
    if submission is not None:
        anchors.append((submission.package_id, submission.fields))
    if approval is not None and approval.status == "active":
        anchors.append((approval.package_id, approval.fields))
    if not anchors:
        return False
    current = {item.field: item.after for item in items}
    for package_id, fields in anchors:
        if latest is not None and package_id != latest.id:
            return True
        if not isinstance(fields, dict):
            return True
        version = fields.get("package_version")
        if latest is not None and version is not None and version != latest.version:
            return True
        stored = _after_map(fields.get("changes"))
        if not stored:
            return True
        if set(stored) != set(current):
            return True
        if any(not _json_same(stored[field], current[field]) for field in stored):
            return True
    return False


def _approved_values(submission: ListingSubmission | None, approval: ListingApproval | None) -> dict[str, Any]:
    if approval is not None and approval.status == "active" and isinstance(approval.fields, dict):
        return _after_map(approval.fields.get("changes"))
    if submission is not None and submission.status not in {"prepared", "awaiting_approval"} and isinstance(submission.fields, dict):
        return _after_map(submission.fields.get("changes"))
    return {}


def _stale_for_platform(platform: str, items: list[ChangeItem], content: list[ContentItem]) -> bool:
    stale_ids = [row.id for row in content if _content_stale(row)]
    if not stale_ids:
        return False
    if any(row.platform == platform and row.id in stale_ids for row in content):
        return True
    blob = " ".join(_source_ref(item.source_ref) or "" for item in items)
    return any(f"content:{item_id}" in blob for item_id in stale_ids)


def _content_stale(row: ContentItem) -> bool:
    brief = row.brief if isinstance(row.brief, dict) else {}
    approval = brief.get("approval")
    if not isinstance(approval, dict):
        return False
    return approval.get("facts_hash") != brief.get("input_hash") or approval.get("content_hash") != brief.get("content_hash")


def _content_block(content: list[ContentItem], profile: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    descriptions = []
    illustrations = []
    for row in content:
        brief = row.brief if isinstance(row.brief, dict) else {}
        if row.kind in ILLUSTRATION_KINDS or brief.get("origin") == "ai_illustration" or brief.get("illustrative") is True:
            if brief.get("illustrative") is not True or brief.get("origin") != "ai_illustration":
                issues.append("unsupported_claim")
            illustrations.append(
                {
                    "id": row.id,
                    "kind": row.kind,
                    "platform": row.platform,
                    "label": ILLUSTRATION_LABEL,
                    "illustrative": True,
                    "real_photo": False,
                    "path": row.result_path,
                    "status": row.status,
                    "version": brief.get("version"),
                }
            )
            continue
        if row.kind in DESCRIPTION_KINDS:
            if _content_stale(row):
                issues.append("changed_content_version")
                continue
            approval = brief.get("approval")
            if row.status != "checked" or not isinstance(approval, dict):
                continue
            descriptions.append(
                {
                    "id": row.id,
                    "kind": row.kind,
                    "platform": row.platform,
                    "text": brief.get("text"),
                    "version": brief.get("version"),
                    "approved_at": approval.get("at"),
                }
            )
    services = approved_fact(profile, "services")
    return (
        {
            "descriptions": descriptions,
            "services": [] if services.status != "approved" else services.value,
            "illustrations": illustrations,
        },
        issues,
    )


def _deficiencies(audit: Audit | None, plan: ListingImprovementPlan | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if audit is not None and isinstance(audit.observations, dict):
        for text in audit.observations.get("texts") or []:
            if str(text).strip():
                rows.append({"platform": None, "text": str(text).strip(), "source": "audit"})
    gaps = audit.gaps if audit is not None else None
    gap_items = gaps.get("items") if isinstance(gaps, dict) else gaps if isinstance(gaps, list) else []
    for item in gap_items or []:
        if isinstance(item, dict):
            rows.append(
                {
                    "platform": item.get("platform"),
                    "text": str(item.get("text") or item.get("note") or item.get("field") or ""),
                    "source": "audit_gap",
                }
            )
    for task in _plan_tasks(plan):
        if task.get("completed") is True:
            continue
        text = str(task.get("current") or task.get("desired") or task.get("type") or "")
        rows.append({"platform": task.get("platform"), "text": text, "source": "bek_plan"})
    return rows


def _requested(plan: ListingImprovementPlan | None) -> list[dict[str, Any]]:
    rows = []
    for task in _plan_tasks(plan):
        rows.append(
            {
                "platform": task.get("platform"),
                "type": task.get("type"),
                "status": task.get("status"),
                "text": str(task.get("desired") or task.get("current") or task.get("type") or ""),
                "completed": False,
            }
        )
    return rows


def _plan_tasks(plan: ListingImprovementPlan | None) -> list[dict[str, Any]]:
    if plan is None or not isinstance(plan.plan, dict):
        return []
    tasks = plan.plan.get("tasks") or []
    return [item for item in tasks if isinstance(item, dict)]


def _actions(platforms: list[dict[str, Any]], tasks: list[dict[str, Any]], issues: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for task in tasks:
        platform = task.get("platform") or ""
        suffix = f" ({platform})" if platform else ""
        rows.append({"kind": "human_task", "text": f"Нужно закрыть ручную задачу {task['reason']}{suffix}."})
    for platform in platforms:
        status = platform["status"]
        name = platform["platform"]
        if status == "not_submitted":
            rows.append({"kind": "submit", "text": f"{name}: правки еще не отправлены."})
        elif status == "moderation_pending":
            rows.append({"kind": "moderation", "text": f"{name}: дождитесь решения модерации."})
        elif status == "human_required":
            rows.append({"kind": "human", "text": f"{name}: нужно решение человека, прежде чем считать правку проверенной."})
        elif status == "rejected":
            rows.append({"kind": "rejected", "text": f"{name}: площадка отклонила отправку."})
        elif status == "uncertain":
            rows.append({"kind": "verify", "text": f"{name}: нужен свежий публичный снимок с скриншотом."})
    if "changed_content_version" in issues:
        rows.append({"kind": "content", "text": "Нужно заново утвердить текст: версия содержимого изменилась."})
    if "changed_package_version" in issues:
        rows.append({"kind": "package", "text": "Пакет правок изменился после утверждения. Нужно новое утверждение."})
    if "stale_evidence" in issues:
        rows.append({"kind": "evidence", "text": "Снимок старше 14 дней. Нужен новый просмотр карточки."})
    if "missing_screenshot" in issues:
        rows.append({"kind": "screenshot", "text": "Не хватает файла скриншота."})
    return rows


def _outstanding(
    platforms: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    actions: list[dict[str, str]],
) -> list[dict[str, str]]:
    rows = [{"kind": row["kind"], "text": row["text"]} for row in actions]
    for platform in platforms:
        for item in platform["discrepancies"]:
            rows.append(
                {
                    "kind": "discrepancy",
                    "text": f"{platform['platform']} {item.get('field')}: {item.get('kind')}",
                }
            )
    for task in tasks:
        if not any(task["reason"] in row["text"] for row in rows):
            rows.append({"kind": "human_task", "text": task["reason"]})
    return rows


def _outcome(statuses: list[str], issues: list[str], open_tasks: bool) -> tuple[str, bool]:
    unique = set(statuses) or {"not_submitted"}
    blocking = {
        "unsupported_claim",
        "stale_evidence",
        "missing_screenshot",
        "changed_package_version",
        "changed_content_version",
        "incomplete_evidence",
        "publication_not_independently_confirmed",
        "rejected_but_snapshot_matches",
        "partial_fields",
    }
    blocked = bool(set(issues) & blocking) or open_tasks
    if unique == {"published_verified"} and not blocked:
        return "stored_verified", True
    if unique <= {"not_submitted"}:
        return "not_submitted", False
    if unique == {"submitted"}:
        return "submitted", False
    if unique == {"moderation_pending"}:
        return "moderation_pending", False
    if unique == {"rejected"}:
        return "rejected", False
    if unique == {"uncertain"}:
        return "uncertain", False
    if "human_required" in unique and "published_verified" not in unique:
        return "blocked", False
    return "partial", False


def _before_evidence(
    items: list[ChangeItem],
    logs: dict[int, list[ChangeLog]],
    snapshots: list[CardSnapshot],
    submitted_at: datetime | None,
) -> dict[str, Any] | None:
    shot = None
    shot_at = None
    for item in items:
        for log in logs.get(item.id, []):
            if log.screenshot_before:
                shot = log.screenshot_before
                shot_at = _iso(log.at)
    prior = [row for row in snapshots if row.purpose != "report" or (submitted_at and _aware(row.collected_at) <= _aware(submitted_at))]
    chosen = prior[-1] if prior else None
    if shot is None and chosen is None and not items:
        return None
    return {
        "screenshot": shot,
        "screenshot_exists": bool(shot and Path(shot).is_file()),
        "at": shot_at,
        "snapshot_id": None if chosen is None else chosen.id,
        "collected_at": None if chosen is None else _iso(chosen.collected_at),
        "collected_by": None if chosen is None else chosen.collected_by,
        "url": None if chosen is None else chosen.url,
        "live": False,
    }


def _submit_evidence(submission: ListingSubmission | None) -> dict[str, Any] | None:
    if submission is None or submission.status in {"prepared", "awaiting_approval", "approved"}:
        return None
    return {
        "submission_id": submission.id,
        "status": submission.status,
        "at": _iso(submission.submitted_at),
        "screenshot_before": submission.screenshot_before,
        "screenshot_after": submission.screenshot_after,
        "screenshot_before_exists": bool(submission.screenshot_before and Path(submission.screenshot_before).is_file()),
        "screenshot_after_exists": bool(submission.screenshot_after and Path(submission.screenshot_after).is_file()),
        "independent": False,
        "counts_as_publication": False,
        "live": False,
    }


def _after_evidence(snapshot: CardSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "kind": "public_snapshot",
        "independent": True,
        "live": False,
        "snapshot_id": snapshot.id,
        "purpose": snapshot.purpose,
        "status": snapshot.status,
        "collected_at": _iso(snapshot.collected_at),
        "collected_by": snapshot.collected_by,
        "url": snapshot.url,
        "screenshot": snapshot.screenshot_path,
        "screenshot_exists": bool(snapshot.screenshot_path and Path(snapshot.screenshot_path).is_file()),
    }


def _after_note(snapshot: CardSnapshot | None, status: str) -> dict[str, Any] | None:
    if snapshot is None or status == "not_submitted":
        return None
    note = _after_evidence(snapshot)
    if note is None:
        return None
    note["independent"] = False
    note["used_for_verification"] = False
    return note


def _task(row: HumanTask) -> dict[str, Any]:
    details = row.details if isinstance(row.details, dict) else {}
    return {
        "id": row.id,
        "reason": row.reason,
        "status": row.status,
        "platform": details.get("platform"),
        "detail": details.get("detail") if isinstance(details.get("detail"), str) else None,
        "created_at": _iso(row.created_at),
    }


def _targets(intake: Intake | None) -> tuple[str, ...]:
    checklist = intake.checklist if intake is not None and isinstance(intake.checklist, dict) else {}
    names = checklist.get("platforms") if isinstance(checklist.get("platforms"), dict) else {}
    chosen = tuple(name for name in PLATFORM_ORDER if name in names)
    return chosen or PLATFORM_ORDER


def _profile(session: Session, company_id: int) -> dict[str, Any]:
    row = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == company_id).order_by(CompanyProfile.version.desc())
    )
    return row.data if row is not None and isinstance(row.data, dict) else {}


def _items(session: Session, package_ids: list[int]) -> list[ChangeItem]:
    if not package_ids:
        return []
    return list(
        session.scalars(select(ChangeItem).where(ChangeItem.package_id.in_(package_ids)).order_by(ChangeItem.n, ChangeItem.id)).all()
    )


def _logs(session: Session, item_ids: list[int]) -> dict[int, list[ChangeLog]]:
    if not item_ids:
        return {}
    rows = session.scalars(select(ChangeLog).where(ChangeLog.change_item_id.in_(item_ids)).order_by(ChangeLog.id)).all()
    grouped: dict[int, list[ChangeLog]] = {}
    for row in rows:
        if row.change_item_id is None:
            continue
        grouped.setdefault(row.change_item_id, []).append(row)
    return grouped


def _orphan_snapshot(snapshots: list[CardSnapshot], submissions: list[ListingSubmission]) -> bool:
    submitted = {row.platform for row in submissions if row.submitted_at is not None}
    return any(row.purpose == "report" and row.platform not in submitted for row in snapshots)


def _find_report(session: Session, order_id: int) -> Report | None:
    rows = session.scalars(
        select(Report).where(Report.order_id == order_id, Report.period == PERIOD).order_by(Report.id)
    ).all()
    for row in rows:
        if isinstance(row.data, dict) and row.data.get("kind") == KIND:
            return row
    return None


def _save_report(session: Session, order_id: int, document: dict[str, Any], existing: Report | None) -> None:
    payload = json.loads(_dumps(document))
    if existing is None:
        session.add(
            Report(
                order_id=order_id,
                period=PERIOD,
                data=payload,
                summary=str(payload.get("summary") or ""),
                status="draft",
                pdf_path=None,
                xlsx_path=None,
            )
        )
    else:
        existing.data = payload
        existing.summary = str(payload.get("summary") or "")
        existing.pdf_path = None
    session.flush()


def _quality(document: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if document.get("live_verified") is not False or document.get("verification_mode") != MODE:
        problems.append("live_claim")
    if document.get("limits", {}).get("click_is_publication") is not False:
        problems.append("click_claim")
    if document.get("completed") is True and document.get("outcome") != "stored_verified":
        problems.append("completed")
    for platform in document.get("platforms") or []:
        if platform.get("live_verified") is not False:
            problems.append("live_claim")
        if platform.get("status") == "published_verified":
            after = (platform.get("evidence") or {}).get("after") or {}
            if after.get("independent") is not True or after.get("live") is not False or after.get("screenshot_exists") is not True:
                problems.append("verified_without_evidence")
        for change in platform.get("changes") or []:
            if change.get("draft") and change.get("status") == "published_verified":
                problems.append("draft_verified")
    for image in (document.get("content") or {}).get("illustrations") or []:
        if image.get("label") != ILLUSTRATION_LABEL or image.get("illustrative") is not True or image.get("real_photo") is not False:
            problems.append("illustration")
    raw = _dumps(document)
    if re.search(r"(?i)(password|passwd|token|secret)\s*[=:]\s*(?!\[redacted\]).+", raw):
        problems.append("secret")
    if re.search(r"(?i)\bbearer\s+(?!\[redacted\])[A-Za-z0-9]", raw):
        problems.append("secret")
    if any(dash in raw for dash in ("\u2013", "\u2014")):
        problems.append("long_dash")
    return problems


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items() if str(key).lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _company_html(company: dict[str, Any]) -> str:
    return (
        "<dl>"
        f"<dt>Название</dt><dd>{escape(str(company['name']))}</dd>"
        f"<dt>Город</dt><dd>{escape(str(company['city']))}</dd>"
        f"<dt>2ГИС</dt><dd>{escape(str(company['id_2gis']))}</dd>"
        "</dl>"
    )


def _platforms_html(platforms: list[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(row['platform'])}</td>"
        f"<td>{escape(row['status'])}</td>"
        f"<td>{escape(STATUS_LABELS.get(row['status'], row['status']))}</td>"
        f"<td>{escape(str(row.get('moderation_status') or ''))}</td>"
        "</tr>"
        for row in platforms
    )
    return "<table><thead><tr><th>Площадка</th><th>Код</th><th>Состояние</th><th>Модерация</th></tr></thead>" f"<tbody>{rows}</tbody></table>"


def _rows_html(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    if not rows:
        return "<p>Нет.</p>"
    body = "".join("<li>" + escape(" | ".join(str(row.get(key) or "") for key in keys if row.get(key))) + "</li>" for row in rows)
    return f"<ul>{body}</ul>"


def _changes_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>Нет.</p>"
    items = []
    for row in rows:
        note = "Черновик, форма не отправлена." if row.get("draft") else STATUS_LABELS.get(row["status"], row["status"])
        items.append(
            "<li>"
            + escape(f"{row['platform']} {row['field']}: {note}")
            + "</li>"
        )
    return "<ul>" + "".join(items) + "</ul>"


def _evidence_html(platforms: list[dict[str, Any]]) -> str:
    chunks = []
    for row in platforms:
        evidence = row["evidence"]
        before = evidence.get("before") or {}
        submit = evidence.get("submit") or {}
        after = evidence.get("after") or {}
        chunks.append(
            "<li>"
            + escape(
                f"{row['platform']}: до {before.get('screenshot') or before.get('snapshot_id') or 'нет'}, "
                f"отправка не является публикацией ({submit.get('status') or 'нет'}), "
                f"снимок после {after.get('snapshot_id') or 'нет'}"
            )
            + "</li>"
        )
    return "<ul>" + "".join(chunks) + "</ul>"


def _content_html(content: dict[str, Any]) -> str:
    descriptions = content.get("descriptions") or []
    services = content.get("services") or []
    images = content.get("illustrations") or []
    desc = "".join(f"<li>{escape(str(row.get('text') or ''))}</li>" for row in descriptions) or "<li>Нет утвержденного описания.</li>"
    service_rows = "".join(f"<li>{escape(_service_line(row))}</li>" for row in services) or "<li>Нет утвержденных услуг.</li>"
    image_rows = "".join(
        f"<li>{escape(row['kind'])}: {escape(row['label'])}</li>" for row in images
    ) or "<li>Нет иллюстраций.</li>"
    return f"<h3>Описания</h3><ul>{desc}</ul><h3>Услуги</h3><ul>{service_rows}</ul><h3>Иллюстрации</h3><ul>{image_rows}</ul>"


def _times_html(document: dict[str, Any]) -> str:
    return (
        "<dl>"
        f"<dt>Проверка</dt><dd>{escape(str(document['verified_at']))}</dd>"
        f"<dt>Отчет</dt><dd>{escape(str(document['generated_at']))}</dd>"
        "</dl>"
    )


def _service_line(row: Any) -> str:
    if not isinstance(row, dict):
        return str(row)
    name = row.get("name") or ""
    price = row.get("price")
    return f"{name}, {price} ₸" if price is not None else str(name)


def _source_ok(snapshot: CardSnapshot, field: str) -> tuple[dict[str, Any] | None, bool, str | None]:
    source, key = _lookup(snapshot.field_sources or {}, field)
    if not isinstance(source, dict):
        return None, False, "incomplete_evidence"
    url = source.get("url")
    seen = source.get("seen_at")
    if not url or not seen:
        return None, False, "incomplete_evidence"
    shot = source.get("screenshot") or snapshot.screenshot_path
    if not shot or not Path(str(shot)).is_file():
        return None, False, "missing_screenshot"
    return (
        {
            "field": key,
            "url": url,
            "seen_at": seen,
            "screenshot": str(shot),
            "read_by": source.get("read_by"),
            "live": False,
        },
        True,
        None,
    )


def _lookup(mapping: dict[str, Any], field: str) -> tuple[Any, str | None]:
    for key in FIELD_ALIASES.get(field, (field,)):
        if key in mapping:
            return mapping[key], key
    return None, None


def _matches(field: str, approved: Any, observed: Any) -> bool:
    shape = "phones" if field == "phones" else "services" if field == "services" else "text"
    if shape == "text" and isinstance(approved, str) and isinstance(observed, str):
        return same_value(shape, observed, approved)
    if shape in {"phones", "services"}:
        return same_value(shape, observed, approved)
    return _json_same(approved, observed)


def _claims_live(snapshot: CardSnapshot) -> bool:
    if snapshot.collected_by in {"live", "mock_live"}:
        return True
    sources = snapshot.field_sources if isinstance(snapshot.field_sources, dict) else {}
    meta = sources.get("_collection")
    return isinstance(meta, dict) and meta.get("live") is True


def _missing_file(path: str | None) -> bool:
    return bool(path) and not Path(path).is_file()


def _after_map(changes: Any) -> dict[str, Any]:
    if not isinstance(changes, list):
        return {}
    found: dict[str, Any] = {}
    for row in changes:
        if isinstance(row, dict) and row.get("field"):
            found[str(row["field"])] = row.get("after")
    return found


def _source_ref(value: Any) -> str | None:
    if isinstance(value, dict):
        ref = value.get("ref")
        return None if ref is None else str(ref)
    if isinstance(value, str):
        return value
    return None


def _package_version(
    submission: ListingSubmission | None,
    approval: ListingApproval | None,
    latest: ChangePackage | None,
) -> int | None:
    for fields in (
        None if submission is None or not isinstance(submission.fields, dict) else submission.fields,
        None if approval is None or not isinstance(approval.fields, dict) else approval.fields,
    ):
        if isinstance(fields, dict) and isinstance(fields.get("package_version"), int):
            return fields["package_version"]
    return None if latest is None else latest.version


def _ordered(issues: list[str]) -> list[str]:
    rank = {name: index for index, name in enumerate(ISSUE_ORDER)}
    unique = sorted(set(issues), key=lambda name: (rank.get(name, len(ISSUE_ORDER)), name))
    return unique


def _json_same(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, default=str
    )


def _confirmation(submission: ListingSubmission | None) -> str | None:
    if submission is None or not isinstance(submission.confirmation, dict):
        return None
    text = submission.confirmation.get("text")
    return text if isinstance(text, str) else None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CompletionError("timestamp must be timezone-aware")
    return value


def _dumps(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
