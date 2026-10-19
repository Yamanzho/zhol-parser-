"""Offline listing submission. A Save or Create click is not publication.

The default mode reads a saved cabinet page and a fake platform response.
Live cabinets stay closed. An uncertain Create is not retried.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zhol.browser.cabinet.editor import EditorSpec, load_create_form, load_platform_editor
from zhol.browser.cabinet.prepare import _fill_text, wait_for_person
from zhol.browser.cabinet.session import BrowserCrash, CabinetStopped, read_local_html
from zhol.browser.config import load_platforms
from zhol.browser.google.page import FixtureCabinet as GoogleCabinet
from zhol.browser.twogis.page import FixtureCabinet as TwoGisCabinet
from zhol.browser.yandex.page import FixtureCabinet as YandexCabinet
from zhol.db.models import (
    ChangeItem,
    ChangeLog,
    ChangePackage,
    CompanyPlatformId,
    HumanTask,
    ListingApproval,
    ListingSubmission,
    ListingSubmissionAttempt,
)
from zhol.fulfillment.adapters.base import CabinetAdapter
from zhol.fulfillment.compare import collapse
from zhol.fulfillment.engine import (
    FulfillmentError,
    FulfillmentRefused,
    _granted,
    _order_context,
    _platforms,
    default_adapters,
)
from zhol.fulfillment.store import ensure_listing_task, ensure_review_task
from zhol.guards import refuse_live_listing_submit
from zhol.redact import redact
from zhol.timeutil import now

APPROVAL_TTL = timedelta(hours=24)
CABINETS = {"2gis": TwoGisCabinet, "yandex": YandexCabinet, "google": GoogleCabinet}
ACCEPTED = frozenset({"submitted", "moderation_pending", "published"})
TERMINAL = frozenset({"submitted", "moderation_pending", "published", "rejected", "uncertain", "human_required"})
SECRET_TEXT = ("парол", "password", "смс", "sms", "token", "bearer", "session")


@dataclass(frozen=True)
class SubmitReport:
    order_id: int
    platform: str
    operation: str
    status: str
    detail: str | None
    submitted: bool
    published: bool
    package_id: int | None = None
    package_version: int | None = None
    approval_id: int | None = None
    changes: tuple[dict[str, Any], ...] = ()
    confirmation: str | None = None
    moderation_status: str | None = None
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "mode": "offline",
            "platform": self.platform,
            "operation": self.operation,
            "status": self.status,
            "detail": self.detail,
            "submitted": self.submitted,
            "published": self.published,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "approval_id": self.approval_id,
            "changes": list(self.changes),
            "confirmation": self.confirmation,
            "moderation_status": self.moderation_status,
            "duplicate": self.duplicate,
            "at": now().isoformat(),
        }


def format_submit(report: SubmitReport) -> str:
    lines = [
        f"order {report.order_id}",
        "mode offline",
        f"platform {report.platform}",
        f"operation {report.operation}",
        f"status {report.status}",
        f"submitted {str(report.submitted).lower()}",
        f"published {str(report.published).lower()}",
    ]
    if report.duplicate:
        lines.append("duplicate true")
    if report.detail:
        lines.append(f"detail {report.detail}")
    if report.confirmation:
        lines.append(f"confirmation {report.confirmation}")
    if report.moderation_status:
        lines.append(f"moderation {report.moderation_status}")
    return "\n".join(lines)


def approve_listing(
    session: Session,
    order_id: int,
    *,
    platform: str,
    operation: str,
    page: Any,
    evidence_dir: str | Path,
) -> SubmitReport:
    """Record a human approval of one package revision, operation, and draft."""
    folder = Path(evidence_dir)
    bound = _bind(session, order_id, platform, operation, page, folder)
    if isinstance(bound, SubmitReport):
        _write_evidence(folder, bound)
        return bound
    order, company, work, view, package, items, submission = bound
    review = _review(folder)
    if review is None:
        return _stop(session, folder, submission, "human_required", "prepared evidence is missing", items)
    if not _review_matches(review, items):
        return _stop(session, folder, submission, "human_required", "evidence does not match the change package", items)
    mismatch = _page_problem(session, company.id, company.name, platform, operation, view, items)
    if mismatch is not None:
        return _stop(session, folder, submission, "human_required", mismatch, items)
    if not _granted(session, company.id, platform, work):
        return _stop(session, folder, submission, "human_required", "missing owner permission", items)
    changes = _canonical(items)
    page_values = _controls(view)
    payload = {"changes": changes, "page": page_values, "package_version": package.version}
    digest = _digest(payload)
    current = _active_approval(session, submission)
    if current is not None and current.digest == digest and current.expires_at > now():
        submission.approval_id = current.id
        _move(submission, "approved")
        submission.fields = payload
        submission.updated_at = now()
        report = _report(submission, items, detail=None, approval_id=current.id)
        _write_evidence(folder, report)
        return report
    if current is not None:
        current.status = "invalidated"
        current.detail = "replaced by a new approval"
    approval = ListingApproval(
        order_id=order.id,
        package_id=package.id,
        platform=platform,
        operation=operation,
        digest=digest,
        fields=payload,
        evidence={
            "review": str(folder / "review.json"),
            "before": str(folder / "before.png"),
            "prepared": str(folder / "prepared.png"),
            "package_version": package.version,
        },
        status="active",
        approved_at=now(),
        expires_at=now() + APPROVAL_TTL,
    )
    session.add(approval)
    session.flush()
    submission.approval_id = approval.id
    submission.fields = payload
    submission.evidence = dict(approval.evidence)
    _move(submission, "approved")
    submission.detail = None
    submission.updated_at = now()
    _close_review(session, order.id, platform, operation)
    report = _report(submission, items, detail=None, approval_id=approval.id)
    _write_evidence(folder, report)
    return report


def submit_listing(
    session: Session,
    order_id: int,
    *,
    platform: str,
    operation: str,
    page: Any,
    evidence_dir: str | Path,
    mode: str = "offline",
) -> SubmitReport:
    """Submit one approved draft in offline mode. A click never sets published."""
    refuse_live_listing_submit(mode)
    folder = Path(evidence_dir)
    bound = _bind(session, order_id, platform, operation, page, folder)
    if isinstance(bound, SubmitReport):
        _write_evidence(folder, bound)
        return bound
    _order, company, work, view, package, items, submission = bound
    if submission.status == "submitting":
        return _uncertain(session, folder, submission, items, "the previous submit did not finish", clicked=False)
    if submission.status in TERMINAL:
        report = _report(
            submission,
            items,
            detail=submission.detail,
            approval_id=submission.approval_id,
            duplicate=submission.status in ACCEPTED or submission.status == "rejected",
        )
        _write_evidence(folder, report)
        return report
    approval = _active_approval(session, submission)
    if approval is None:
        _move(submission, "awaiting_approval")
        return _stop(session, folder, submission, "awaiting_approval", "approval is missing", items, move=False)
    stored_changes = approval.fields.get("changes") if isinstance(approval.fields, dict) else None
    if not _same(stored_changes, _canonical(items)) or approval.evidence.get("package_version") != package.version:
        approval.status = "invalidated"
        approval.detail = "planned values changed"
        submission.approval_id = None
        _move(submission, "awaiting_approval")
        _reopen_review(session, submission.order_id, platform, operation, items, str(folder / "submission.json"))
        return _stop(session, folder, submission, "awaiting_approval", "planned values changed", items, move=False)
    if approval.expires_at <= now():
        approval.status = "expired"
        approval.detail = "approval expired"
        submission.approval_id = None
        _move(submission, "awaiting_approval")
        return _stop(session, folder, submission, "awaiting_approval", "approval expired", items, move=False)
    if not _granted(session, company.id, platform, work):
        return _stop(session, folder, submission, "human_required", "missing owner permission", items)
    kind = _pause(view, default_adapters()[platform])
    if kind is not None:
        return _stop(session, folder, submission, "human_required", kind, items)
    mismatch = _page_problem(session, company.id, company.name, platform, operation, view, items)
    if mismatch is not None:
        status = "human_required" if mismatch != "stopped_unknown_ui" else "human_required"
        return _stop(session, folder, submission, status, mismatch, items)
    approved_page = approval.fields.get("page") if isinstance(approval.fields, dict) else None
    if not isinstance(approved_page, dict) or _controls(view) != _plain(approved_page):
        return _stop(session, folder, submission, "human_required", "cabinet page differs from the approved draft", items)
    adapter = default_adapters()[platform]
    button = adapter.submit_buttons.get(operation)
    if button is None or button not in view.body_text():
        return _stop(session, folder, submission, "human_required", "stopped_unknown_ui", items)
    return _click(session, folder, submission, items, view, adapter, button)


def verify_listing(
    session: Session,
    order_id: int,
    *,
    platform: str,
    operation: str,
    page: Any,
    evidence_dir: str | Path,
    mode: str = "offline",
) -> SubmitReport:
    """Read a later verification page. Publication is this step, not the submit click."""
    refuse_live_listing_submit(mode)
    folder = Path(evidence_dir)
    bound = _bind(session, order_id, platform, operation, page, folder)
    if isinstance(bound, SubmitReport):
        _write_evidence(folder, bound)
        return bound
    _order, company, work, view, _package, items, submission = bound
    if view.clicked():
        report = _report(submission, items, detail="verification must not click")
        _write_evidence(folder, report)
        return report
    if submission.status not in {"submitted", "moderation_pending"}:
        report = _report(submission, items, detail="verification waits until the form has been sent", duplicate=True)
        _write_evidence(folder, report)
        return report
    if not _granted(session, company.id, platform, work):
        return _stop(session, folder, submission, "human_required", "missing owner permission", items)
    mismatch = _page_problem(session, company.id, company.name, platform, operation, view, items, require_draft=False)
    if mismatch is not None:
        return _stop(session, folder, submission, "human_required", mismatch, items)
    verdict = getattr(view, "verification", None)
    if verdict == "published":
        _move(submission, "published")
        submission.moderation_status = "published"
        submission.updated_at = now()
        _log(session, items, operation, "moderation_check", "ok", True, "published", None, None, None, published=True)
        report = _report(submission, items, detail=None, moderation_status="published")
        _write_evidence(folder, report)
        return report
    if verdict == "rejected":
        _move(submission, "rejected")
        submission.moderation_status = "rejected"
        submission.updated_at = now()
        report = _report(submission, items, detail="moderation rejected", moderation_status="rejected")
        _write_evidence(folder, report)
        return report
    if verdict == "moderation_pending":
        if submission.status != "moderation_pending":
            _move(submission, "moderation_pending")
        submission.moderation_status = "pending"
        submission.updated_at = now()
        report = _report(submission, items, detail=None, moderation_status="pending")
        _write_evidence(folder, report)
        return report
    report = _report(submission, items, detail="verification did not confirm the listing")
    _write_evidence(folder, report)
    return report


def submit_prepared_platforms(
    session: Session,
    order_id: int,
    pages: Mapping[str, Any],
    evidence_dir: str | Path,
    *,
    mode: str = "offline",
) -> tuple[SubmitReport, ...]:
    """Submit each platform on its own. One failure stays on that platform."""
    refuse_live_listing_submit(mode)
    reports: list[SubmitReport] = []
    folder = Path(evidence_dir)
    for platform in sorted(pages):
        try:
            _order, _company, checklist, _profile = _order_context(session, order_id)
            work = _platforms(checklist).get(platform)
            operation = "" if work is None else str(work.get("action") or "")
            reports.append(
                submit_listing(
                    session,
                    order_id,
                    platform=platform,
                    operation=operation,
                    page=pages[platform],
                    evidence_dir=folder / platform,
                    mode=mode,
                )
            )
        except (FulfillmentError, FulfillmentRefused, PermissionError, CabinetStopped, OSError) as exc:
            reports.append(
                SubmitReport(
                    order_id,
                    platform,
                    "update",
                    "human_required",
                    str(exc),
                    False,
                    False,
                )
            )
    return tuple(reports)


def _click(
    session: Session,
    folder: Path,
    submission: ListingSubmission,
    items: list[ChangeItem],
    view: Any,
    adapter: CabinetAdapter,
    button: str,
) -> SubmitReport:
    _move(submission, "submitting")
    submission.updated_at = now()
    session.flush()
    before = folder / "submit-before.png"
    after = folder / "submit-after.png"
    try:
        view.screenshot(before)
        reaction = view.simulate_submit(button)
        applied = adapter.offline_submit(submission.operation, reaction)
    except BrowserCrash:
        return _uncertain(session, folder, submission, items, "the browser closed during submit", clicked=True, before=before)
    except (CabinetStopped, PermissionError) as exc:
        return _stop(session, folder, submission, "human_required", str(exc), items)
    except Exception as exc:
        return _uncertain(session, folder, submission, items, f"submit result is unknown: {type(exc).__name__}", clicked=True, before=before)
    if applied.details.get("published") is True or applied.result == "published":
        return _uncertain(session, folder, submission, items, "a click was reported as published", clicked=True, before=before)
    try:
        view.screenshot(after)
    except Exception:
        return _uncertain(session, folder, submission, items, "the after screenshot was not saved", clicked=True, before=before)
    status = str(applied.result)
    if status not in {"approved", "submitted", "moderation_pending", "rejected", "uncertain"}:
        status = "uncertain"
    confirmation = _public_text(applied.details.get("confirmation"))
    if status == "uncertain":
        return _uncertain(
            session,
            folder,
            submission,
            items,
            "the platform did not confirm the submit",
            clicked=bool(reaction.clicked),
            before=before,
            after=after,
            confirmation=confirmation,
        )
    if status == "approved":
        _move(submission, "approved")
        submission.detail = "the request was not sent"
        submission.updated_at = now()
        _attempt(session, submission, "network_failure", True, submission.detail, None, before, after)
        _log(session, items, submission.operation, _action(submission.operation), "failed", False, "approved", None, before, after)
        report = _report(submission, items, detail=submission.detail)
        _write_evidence(folder, report, before, after)
        return report
    _move(submission, status)
    moderation = {"submitted": "unconfirmed", "moderation_pending": "pending", "rejected": "rejected"}[status]
    submission.submitted_at = now()
    submission.moderation_status = moderation
    submission.confirmation = None if confirmation is None else {"text": confirmation, "moderation_status": moderation}
    submission.screenshot_before = str(before)
    submission.screenshot_after = str(after)
    submission.detail = None if status != "rejected" else "the platform rejected the submit"
    submission.updated_at = now()
    accepted = status in {"submitted", "moderation_pending", "rejected"}
    _attempt(session, submission, status, True, submission.detail, confirmation, before, after)
    result = "ok" if status in {"submitted", "moderation_pending"} else "failed"
    _log(
        session,
        items,
        submission.operation,
        _action(submission.operation),
        result,
        accepted,
        status,
        confirmation,
        before,
        after,
    )
    report = _report(submission, items, detail=submission.detail, confirmation=confirmation, moderation_status=moderation)
    _write_evidence(folder, report, before, after)
    return report


def _bind(
    session: Session,
    order_id: int,
    platform: str,
    operation: str,
    page: Any,
    folder: Path,
) -> SubmitReport | tuple[Any, Any, dict[str, Any], Any, ChangePackage, list[ChangeItem], ListingSubmission]:
    if platform not in CABINETS or operation not in {"create", "update"}:
        return SubmitReport(order_id, platform, operation or "update", "human_required", "listing action is missing", False, False)
    try:
        order, company, checklist, _profile = _order_context(session, order_id)
    except FulfillmentError as exc:
        return SubmitReport(order_id, platform, operation, "human_required", str(exc), False, False)
    work = _platforms(checklist).get(platform)
    if work is None or work.get("action") != operation:
        return SubmitReport(order_id, platform, operation, "human_required", "operation does not match the order", False, False)
    try:
        view = _open(page, platform)
    except PermissionError as exc:
        return SubmitReport(order_id, platform, operation, "human_required", str(exc), False, False)
    package, items = _prepared(session, order.id, platform)
    if package is None or not items:
        return SubmitReport(order_id, platform, operation, "human_required", "prepared draft is missing", False, False)
    submission = _ensure(session, order.id, package, platform, operation, items, folder)
    return order, company, work, view, package, items, submission


def _ensure(
    session: Session,
    order_id: int,
    package: ChangePackage,
    platform: str,
    operation: str,
    items: list[ChangeItem],
    folder: Path,
) -> ListingSubmission:
    key = _submission_key(order_id, package.id, platform, operation)
    row = session.scalar(select(ListingSubmission).where(ListingSubmission.idempotency_key == key))
    payload = {"changes": _canonical(items), "page": {}, "package_version": package.version}
    if row is None:
        row = ListingSubmission(
            order_id=order_id,
            package_id=package.id,
            platform=platform,
            operation=operation,
            status="prepared",
            idempotency_key=key,
            fields=payload,
            evidence={"package_version": package.version},
            created_at=now(),
            updated_at=now(),
        )
        session.add(row)
        session.flush()
    if row.status == "prepared" and _review_open(session, order_id, platform, operation):
        _move(row, "awaiting_approval")
        row.updated_at = now()
    elif row.status == "prepared":
        row.detail = str(folder / "review.json")
    return row


def _prepared(session: Session, order_id: int, platform: str) -> tuple[ChangePackage | None, list[ChangeItem]]:
    package = session.scalar(
        select(ChangePackage).where(ChangePackage.order_id == order_id).order_by(ChangePackage.version.desc())
    )
    if package is None:
        return None, []
    items = list(
        session.scalars(
            select(ChangeItem).where(ChangeItem.package_id == package.id, ChangeItem.platform == platform)
        ).all()
    )
    prepared: list[ChangeItem] = []
    for item in items:
        log_id = session.scalar(
            select(ChangeLog.id).where(
                ChangeLog.change_item_id == item.id,
                ChangeLog.action == "prepare",
                ChangeLog.result == "prepared",
            )
        )
        if log_id is not None:
            prepared.append(item)
    prepared.sort(key=lambda item: item.field)
    return package, prepared


def _page_problem(
    session: Session,
    company_id: int,
    company_name: str,
    platform: str,
    operation: str,
    view: Any,
    items: list[ChangeItem],
    *,
    require_draft: bool = True,
) -> str | None:
    markers = load_platforms()["markers"]
    expected = "create" if operation == "create" else "cabinet"
    if view.marker(markers["page_attr"]) != expected or view.marker(markers["platform_attr"]) != platform:
        return "stopped_unknown_ui"
    page_id = (view.marker(markers["external_id_attr"]) or "").strip()
    editor = _editor(platform, operation)
    shown = view.field_text(editor.organization_field) or ""
    stored = session.get(CompanyPlatformId, (company_id, platform))
    if operation == "update":
        expected_id = "" if stored is None or not stored.external_id else stored.external_id.strip()
        if not expected_id or page_id != expected_id or collapse(shown).casefold() != collapse(company_name).casefold():
            return "organization does not match the order"
    elif page_id or (shown.strip() and collapse(shown).casefold() != collapse(company_name).casefold()):
        return "organization does not match the order"
    role = view.field_text(editor.access_field) or ""
    allowed = {collapse(item).casefold() for item in editor.access_allowed}
    if collapse(role).casefold() not in allowed:
        return "missing owner permission"
    if not require_draft:
        return None
    adapter = default_adapters()[platform]
    for item in items:
        spec = adapter.spec.fields.get(item.field)
        control = editor.fields.get(item.field)
        if spec is None or control is None or item.field in adapter.forbidden:
            return "unrelated field"
        seen = view.control_value(control.role, control.name)
        if seen is None or collapse(seen) != collapse(_fill_text(spec.shape, item.after)):
            return "cabinet page differs from the approved draft"
    return None


def _controls(view: Any) -> dict[str, str]:
    controls = getattr(view, "controls", None)
    if controls is None:
        raise CabinetStopped("stopped_unknown_ui")
    found: dict[str, str] = {}
    for control in controls:
        if control.secret or not control.name:
            continue
        found[control.name] = collapse(control.value)
    return found


def _plain(values: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): collapse(str(value)) for key, value in values.items()}


def _pause(view: Any, adapter: CabinetAdapter) -> str | None:
    _text, kind = wait_for_person(view, adapter.spec.pause, wait_seconds=0, sleeper=lambda _seconds: None)
    return kind


def _editor(platform: str, operation: str) -> EditorSpec:
    if operation == "create":
        return load_create_form(platform).editor
    return load_platform_editor(platform)


def _open(page: Any, platform: str) -> Any:
    if hasattr(page, "simulate_submit") and hasattr(page, "body_text"):
        return page
    text = str(page)
    if text.startswith(("http://", "https://", "file://")):
        raise PermissionError("live listing submission is closed")
    return CABINETS[platform](read_local_html(text), load_platforms()["markers"])


def _canonical(items: list[ChangeItem]) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        ref = item.source_ref.get("ref") if isinstance(item.source_ref, dict) else item.source_ref
        rows.append({"after": item.after, "before": item.before, "field": item.field, "source_ref": ref})
    return rows


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _submission_key(order_id: int, package_id: int, platform: str, operation: str) -> str:
    return _digest({"operation": operation, "order_id": order_id, "package_id": package_id, "platform": platform})


def _review(folder: Path) -> dict[str, Any] | None:
    path = folder / "review.json"
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _review_matches(review: Mapping[str, Any], items: list[ChangeItem]) -> bool:
    prepared = [row for row in review.get("changes", []) if isinstance(row, dict) and row.get("action") == "prepared"]
    if {row.get("field") for row in prepared} != {item.field for item in items}:
        return False
    by_field = {item.field: item.after for item in items}
    return all(_same(row.get("after"), by_field.get(str(row.get("field")))) for row in prepared)


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, default=str
    )


def _active_approval(session: Session, submission: ListingSubmission) -> ListingApproval | None:
    if submission.approval_id is None:
        return None
    approval = session.get(ListingApproval, submission.approval_id)
    if approval is None or approval.status != "active":
        return None
    if approval.order_id != submission.order_id or approval.package_id != submission.package_id:
        return None
    if approval.platform != submission.platform or approval.operation != submission.operation:
        return None
    return approval


def _review_reason(platform: str, operation: str) -> str:
    return f"{platform}_create_review" if operation == "create" else f"{platform}_listing_review"


def _review_open(session: Session, order_id: int, platform: str, operation: str) -> bool:
    rows = session.scalars(
        select(HumanTask).where(
            HumanTask.object_type == "order",
            HumanTask.object_id == order_id,
            HumanTask.reason == _review_reason(platform, operation),
            HumanTask.status == "open",
        )
    ).all()
    return any(isinstance(row.details, dict) and row.details.get("platform") == platform for row in rows)


def _close_review(session: Session, order_id: int, platform: str, operation: str) -> None:
    rows = session.scalars(
        select(HumanTask).where(
            HumanTask.object_type == "order",
            HumanTask.object_id == order_id,
            HumanTask.reason == _review_reason(platform, operation),
            HumanTask.status == "open",
        )
    ).all()
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        if details.get("platform") != platform:
            continue
        row.status = "closed"
        row.resolution = "approved"
        row.closed_at = now()


def _reopen_review(
    session: Session,
    order_id: int,
    platform: str,
    operation: str,
    items: list[ChangeItem],
    evidence: str,
) -> None:
    ensure_review_task(
        session,
        order_id,
        tuple({"field": item.field, "before": item.before, "after": item.after} for item in items),
        evidence,
        reason=_review_reason(platform, operation),
        platform=platform,
    )


def _move(submission: ListingSubmission, status: str) -> None:
    if submission.status == status:
        return
    allowed = {
        "prepared": {"awaiting_approval", "approved", "human_required"},
        "awaiting_approval": {"approved", "human_required"},
        "approved": {"submitting", "awaiting_approval", "human_required"},
        "submitting": {"submitted", "moderation_pending", "rejected", "uncertain", "approved", "human_required"},
        "submitted": {"moderation_pending", "published", "rejected", "uncertain", "human_required"},
        "moderation_pending": {"published", "rejected", "uncertain", "human_required"},
        "uncertain": set(),
        "human_required": set(),
        "published": set(),
        "rejected": set(),
    }
    if status not in allowed.get(submission.status, set()):
        raise FulfillmentError(f"illegal submission transition {submission.status} -> {status}")
    submission.status = status


def _stop(
    session: Session,
    folder: Path,
    submission: ListingSubmission,
    status: str,
    detail: str,
    items: list[ChangeItem],
    *,
    move: bool = True,
) -> SubmitReport:
    if move and submission.status != status:
        if status == "awaiting_approval" and submission.status == "prepared":
            _move(submission, status)
        elif status == "human_required" and submission.status in {"prepared", "awaiting_approval", "approved", "submitting"}:
            _move(submission, status)
        elif status == "awaiting_approval" and submission.status in {"approved", "awaiting_approval", "prepared"}:
            if submission.status != "awaiting_approval":
                _move(submission, "awaiting_approval")
    submission.detail = detail
    submission.updated_at = now()
    if status == "human_required":
        ensure_listing_task(session, submission.order_id, submission.platform, "listing_submission_human", detail, str(folder / "submission.json"))
    report = _report(submission, items, detail=detail)
    _write_evidence(folder, report)
    return report


def _uncertain(
    session: Session,
    folder: Path,
    submission: ListingSubmission,
    items: list[ChangeItem],
    detail: str,
    *,
    clicked: bool,
    before: Path | None = None,
    after: Path | None = None,
    confirmation: str | None = None,
) -> SubmitReport:
    if submission.status == "uncertain":
        report = _report(submission, items, detail=submission.detail or detail)
        _write_evidence(folder, report, before, after)
        return report
    if submission.status == "submitting":
        _move(submission, "uncertain")
    submission.detail = detail
    submission.updated_at = now()
    _attempt(session, submission, "uncertain", clicked, detail, confirmation, before, after)
    _log(session, items, submission.operation, _action(submission.operation), "failed", False, "uncertain", confirmation, before, after)
    ensure_listing_task(
        session,
        submission.order_id,
        submission.platform,
        "listing_submission_uncertain",
        detail,
        str(folder / "submission.json"),
    )
    report = _report(submission, items, detail=detail, confirmation=confirmation)
    _write_evidence(folder, report, before, after)
    return report


def _attempt(
    session: Session,
    submission: ListingSubmission,
    outcome: str,
    clicked: bool,
    detail: str | None,
    confirmation: str | None,
    before: Path | None,
    after: Path | None,
) -> None:
    session.add(
        ListingSubmissionAttempt(
            submission_id=submission.id,
            outcome=outcome,
            clicked=clicked,
            detail=detail,
            confirmation=confirmation,
            screenshot_before=None if before is None else str(before),
            screenshot_after=None if after is None else str(after),
            at=now(),
        )
    )


def _log(
    session: Session,
    items: list[ChangeItem],
    operation: str,
    action: str,
    result: str,
    submitted: bool,
    status: str,
    confirmation: str | None,
    before: Path | None,
    after: Path | None,
    *,
    published: bool = False,
) -> None:
    del operation
    for item in items:
        session.add(
            ChangeLog(
                change_item_id=item.id,
                action=action,
                screenshot_before=None if before is None else str(before),
                screenshot_after=None if after is None else str(after),
                result=result,
                details={
                    "submitted": submitted,
                    "published": published,
                    "mode": "offline",
                    "status": status,
                    "confirmation": confirmation,
                },
                at=now(),
            )
        )


def _action(operation: str) -> str:
    return "create_card" if operation == "create" else "apply"


def _report(
    submission: ListingSubmission,
    items: list[ChangeItem],
    *,
    detail: str | None,
    approval_id: int | None = None,
    confirmation: str | None = None,
    moderation_status: str | None = None,
    duplicate: bool = False,
) -> SubmitReport:
    version = None
    if isinstance(submission.fields, dict):
        raw = submission.fields.get("package_version")
        version = raw if isinstance(raw, int) else None
    status = submission.status
    published = status == "published"
    submitted = status in {"submitted", "moderation_pending", "published", "rejected"}
    if confirmation is None and isinstance(submission.confirmation, dict):
        confirmation = _public_text(submission.confirmation.get("text"))
    if moderation_status is None:
        moderation_status = submission.moderation_status
    return SubmitReport(
        order_id=submission.order_id,
        platform=submission.platform,
        operation=submission.operation,
        status=status,
        detail=detail if detail is not None else submission.detail,
        submitted=submitted,
        published=published,
        package_id=submission.package_id,
        package_version=version,
        approval_id=submission.approval_id if approval_id is None else approval_id,
        changes=tuple(_canonical(items)),
        confirmation=confirmation,
        moderation_status=moderation_status,
        duplicate=duplicate,
    )


def _public_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = redact(value).strip()
    folded = cleaned.casefold()
    if any(word in folded for word in SECRET_TEXT):
        return None
    return cleaned


def _write_evidence(folder: Path, report: SubmitReport, before: Path | None = None, after: Path | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = report.as_dict()
    body["screenshot_before"] = None if before is None or not before.is_file() else str(before)
    body["screenshot_after"] = None if after is None or not after.is_file() else str(after)
    text = json.dumps(body, ensure_ascii=False, indent=2, default=str) + "\n"
    if any(word in text.casefold() for word in ("парол", "password=", "смс-код")):
        body["confirmation"] = None
        text = json.dumps(body, ensure_ascii=False, indent=2, default=str) + "\n"
    (folder / "submission.json").write_text(text, encoding="utf-8")


def _attempt_count(session: Session, submission_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ListingSubmissionAttempt).where(
                ListingSubmissionAttempt.submission_id == submission_id
            )
        )
        or 0
    )
