"""Prepare a new listing draft and stop before Create.

The public card is not created. A person still has to approve the draft
before a later batch may submit it. This step does not click Create, Save, or Publish.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.browser.cabinet.editor import ControlSpec, CreateFormSpec
from zhol.browser.cabinet.prepare import (
    FILLABLE,
    SECRET_PAUSE,
    FieldChange,
    PrepareReport,
    _current,
    _emit,
    _fill_text,
    _open,
    _result,
    _snapshot,
    _status,
    _unchanged,
    wait_for_person,
)
from zhol.browser.config import load_platforms
from zhol.db.models import CardSnapshot, CompanyPlatformId
from zhol.fulfillment.adapters.base import CabinetAdapter
from zhol.fulfillment.compare import build_field_plans, collapse
from zhol.fulfillment.content import load_content, load_files
from zhol.fulfillment.engine import (
    FulfillmentRefused,
    _ambiguities,
    _granted,
    _order_context,
    _platforms,
)
from zhol.fulfillment.existence import (
    SnapshotEvidence,
    classify_existence,
    confirmation_from_checklist,
    page_signal,
)
from zhol.fulfillment.facts import critical_missing
from zhol.fulfillment.store import (
    CREATE_SUMMARIES,
    ensure_listing_task,
    ensure_pause_task,
    ensure_review_task,
    persist_prepared,
    prepared_already,
)
from zhol.fulfillment.types import FieldPlan, ListingContext, PreparedField
from zhol.guards import refuse_listing_submit
from zhol.timeutil import now

HELD = frozenset({"waiting_nap_consent", "waiting_approval"})
MODE = "create_prepare"
TASKS = {
    "existing_listing": "listing_duplicate",
    "uncertain": "listing_absence_uncertain",
    "ownership_required": "listing_ownership_required",
}


def prepare_create_listing(
    session: Session,
    order_id: int,
    *,
    page: Any,
    evidence_dir: str | Path,
    platform: str,
    adapter: CabinetAdapter,
    form: CreateFormSpec,
    fixture_cls: type,
    open_live: Callable[..., Any],
    wait_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
    browser: Any = None,
    approve: bool = False,
) -> PrepareReport:
    """Fill a blank create form from approved facts and stop before Create."""
    refuse_listing_submit(bool(approve))
    order, company, checklist, profile = _order_context(session, order_id)
    ambiguities = _ambiguities(checklist)
    if ambiguities:
        raise FulfillmentRefused(tuple(f"unresolved ambiguity: {item}" for item in ambiguities))
    missing = critical_missing(profile, company.name)
    if missing:
        raise FulfillmentRefused(tuple(f"missing {name}" for name in missing))
    platforms = _platforms(checklist)
    work = platforms.get(platform)
    if work is None:
        raise FulfillmentRefused((f"{platform} is not a target platform",))
    folder = Path(evidence_dir)
    if work["action"] != "create":
        return _emit(folder, _result(order.id, platform, "stopped", "create was not requested", (), False, MODE))
    if not _granted(session, company.id, platform, work):
        return _blocked(session, folder, order.id, platform, "ownership_required", "missing owner permission")

    stored = session.get(CompanyPlatformId, (company.id, platform))
    verdict = classify_existence(
        external_id=None if stored is None else stored.external_id,
        public_url=_stored_url(stored, work),
        snapshots=_evidence(session, company.id, platform),
        confirmation=confirmation_from_checklist(checklist, platform),
        now=now(),
    )
    if verdict.status != "missing_confirmed":
        return _blocked(session, folder, order.id, platform, verdict.status, verdict.reason)

    markers = load_platforms()["markers"]
    view, owned = _open(page, markers, browser, fixture_cls=fixture_cls, open_live=open_live)
    try:
        _text, kind = wait_for_person(view, adapter.spec.pause, wait_seconds=wait_seconds, sleeper=sleeper)
        if kind is not None:
            report = _result(order.id, platform, "paused", kind, (), False, MODE)
            _emit(folder, report, view, store_page=kind not in SECRET_PAUSE, prepared=False)
            ensure_pause_task(session, order.id, platform, kind, str(folder / "review.json"))
            return report
        if view.marker(markers["page_attr"]) != "create" or view.marker(markers["platform_attr"]) != platform:
            return _stop(folder, order.id, platform, "stopped", "stopped_unknown_ui", view)
        page_id = (view.marker(markers["external_id_attr"]) or "").strip()
        state = view.field_text(form.listing_state_field)
        if state is None:
            return _stop(folder, order.id, platform, "stopped", "stopped_unknown_ui", view)
        signal = page_signal(
            external_id=page_id,
            listing_state=state,
            absence_phrases=form.absence_phrases,
            found_phrases=form.found_phrases,
            ownership_phrases=form.ownership_phrases,
        )
        if signal is not None:
            report = _result(order.id, platform, signal.status, signal.reason, (), False, MODE)
            _emit(folder, report, view, store_page=True, prepared=False)
            _escalate(session, order.id, platform, signal.status, signal.reason, str(folder / "review.json"))
            return report
        shown = view.field_text(form.editor.organization_field) or ""
        if shown.strip() and collapse(shown).casefold() != collapse(company.name).casefold():
            return _stop(folder, order.id, platform, "stopped", "organization does not match the order", view)
        role = view.field_text(form.editor.access_field)
        allowed = {collapse(item).casefold() for item in form.editor.access_allowed}
        if role is None:
            return _stop(folder, order.id, platform, "stopped", "stopped_unknown_ui", view)
        if collapse(role).casefold() not in allowed:
            report = _result(order.id, platform, "ownership_required", "missing owner permission", (), False, MODE)
            _emit(folder, report, view, store_page=True, prepared=False)
            _escalate(session, order.id, platform, "ownership_required", "missing owner permission", str(folder / "review.json"))
            return report
        unknown = _unknown_controls(view, form)
        if unknown:
            return _stop(folder, order.id, platform, "stopped", "stopped_unknown_ui", view)
        return _fill(session, order.id, company.id, platform, company.name, profile, view, adapter, form, folder)
    finally:
        if owned:
            view.close()


def _fill(
    session: Session,
    order_id: int,
    company_id: int,
    platform: str,
    company_name: str,
    profile: Mapping[str, Any],
    view: Any,
    adapter: CabinetAdapter,
    form: CreateFormSpec,
    folder: Path,
) -> PrepareReport:
    plans = build_field_plans(
        platform=platform,
        action="create",
        spec=adapter.spec,
        profile=profile,
        company_name=company_name,
        files=load_files(session, order_id),
        library=load_content(session, order_id),
        current=_current(view, form.editor, adapter),
        consents=(),
    )
    required = _required(adapter, form)
    unsupported = [plan.field for plan in plans if plan.field in required and plan.operation == "unsupported"]
    if unsupported:
        return _stop(folder, order_id, platform, "refused", "unsupported field", view)
    held = [plan.field for plan in plans if plan.field in required and plan.operation in HELD]
    if held:
        return _stop(folder, order_id, platform, "held", "the client fact is not approved", view)
    ready = {plan.field for plan in plans if plan.operation in {"set", "already"}}
    missing = [name for name in required if name not in ready]
    if missing:
        return _stop(folder, order_id, platform, "refused", "missing " + ", ".join(missing), view)
    changes, pending, blocked = _select(session, order_id, plans, view, adapter, form, required)
    folder.mkdir(parents=True, exist_ok=True)
    view.screenshot(folder / "before.png")
    if blocked:
        return _stop(folder, order_id, platform, "stopped", "stopped_unknown_ui", view, shot_before=False)
    _guard_plan(adapter, order_id, company_id, company_name, platform, pending, str(folder / "before.png"))
    snapshot = _snapshot(view, form.editor)
    written: list[FieldPlan] = []
    for plan, control in pending:
        view.write_control(control.role, control.name, _fill_text(adapter.spec.fields[plan.field].shape, plan.after))
        written.append(plan)
    _unchanged(view, form.editor, snapshot, {control.name for _plan, control in pending})
    refuse_listing_submit(False)
    if view.clicked():
        raise PermissionError("the form was not supposed to be sent")
    view.screenshot(folder / "prepared.png")
    if written:
        persist_prepared(
            session,
            order_id,
            written,
            screenshot_before=str(folder / "before.png"),
            screenshot_after=str(folder / "prepared.png"),
            evidence=str(folder / "review.json"),
            summary=CREATE_SUMMARIES[platform],
        )
        ensure_review_task(
            session,
            order_id,
            tuple({"field": plan.field, "before": plan.before, "after": plan.after} for plan in written),
            str(folder / "review.json"),
            reason=f"{platform}_create_review",
            platform=platform,
        )
    status, approval = _status(changes, wrote=bool(written))
    report = _result(order_id, platform, status, None, changes, approval, MODE)
    _emit(folder, report, view, store_page=True, prepared=False, shot_before=False)
    return report


def _select(
    session: Session,
    order_id: int,
    plans: tuple[FieldPlan, ...],
    view: Any,
    adapter: CabinetAdapter,
    form: CreateFormSpec,
    required: Sequence[str],
) -> tuple[list[FieldChange], list[tuple[FieldPlan, ControlSpec]], list[str]]:
    changes: list[FieldChange] = []
    pending: list[tuple[FieldPlan, ControlSpec]] = []
    blocked: list[str] = []
    for plan in plans:
        if plan.operation in HELD or plan.operation == "unsupported":
            changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        if plan.operation == "already":
            changes.append(FieldChange(plan.field, "already", plan.before, plan.after))
            continue
        if plan.operation != "set":
            changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        spec = adapter.spec.fields.get(plan.field)
        control = form.editor.fields.get(plan.field)
        visible = control is not None and view.control_value(control.role, control.name) is not None
        if spec is None or control is None or spec.shape not in FILLABLE or plan.field in adapter.forbidden or not visible:
            if plan.field in required:
                blocked.append(plan.field)
            else:
                changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        if prepared_already(session, order_id, plan.platform, plan.field, plan.after):
            changes.append(FieldChange(plan.field, "already", plan.before, plan.after))
            continue
        if plan.source_ref is None or not _fill_text(spec.shape, plan.after).strip():
            if plan.field in required:
                blocked.append(plan.field)
            else:
                changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        pending.append((plan, control))
        changes.append(FieldChange(plan.field, "prepared", plan.before, plan.after))
    return changes, pending, blocked


def _required(adapter: CabinetAdapter, form: CreateFormSpec) -> tuple[str, ...]:
    names = ["name", "address", "phones", "category"]
    for field in adapter.spec.fields.values():
        if field.required_on_create and field.field in form.editor.fields and field.field not in adapter.forbidden:
            names.append(field.field)
    return tuple(dict.fromkeys(name for name in names if name in form.editor.fields and name in adapter.spec.fields))


def _guard_plan(
    adapter: CabinetAdapter,
    order_id: int,
    company_id: int,
    company_name: str,
    platform: str,
    pending: Sequence[tuple[FieldPlan, ControlSpec]],
    screenshot: str,
) -> None:
    if not pending:
        return
    ctx = ListingContext(
        order_id=order_id,
        company_id=company_id,
        company_name=company_name,
        platform=platform,
        action="create",
        external_id=None,
        public_url=None,
        access_status="verified",
        claimed_authorization="verified",
        profile_status=None,
        page_html=None,
        snapshot_fields={},
        snapshot_match="not_found",
        screenshot_path=screenshot,
    )
    fields = tuple(
        PreparedField(plan.field, plan.shape, plan.before, plan.after, plan.source_ref or "") for plan, _control in pending
    )
    prepared = adapter.prepare_create(ctx, fields)
    if prepared.submit or prepared.dropped or prepared.action != "create":
        raise PermissionError("the form was not supposed to be sent")
    applied = adapter.apply_changes(ctx, prepared)
    verified = adapter.verify_changes(ctx, prepared, applied)
    if applied.submitted or applied.result != "dry_run" or applied.screenshot_after is not None or not verified.ok:
        raise PermissionError("the form was not supposed to be sent")


def _unknown_controls(view: Any, form: CreateFormSpec) -> list[str]:
    controls = getattr(view, "controls", None)
    if controls is None:
        return []
    allowed = {item.name for item in form.editor.fields.values()}
    unknown: list[str] = []
    for control in controls:
        if control.secret or not control.name or control.name in allowed:
            continue
        unknown.append(control.name)
    return unknown


def _evidence(session: Session, company_id: int, platform: str) -> tuple[SnapshotEvidence, ...]:
    rows = session.scalars(
        select(CardSnapshot)
        .where(CardSnapshot.company_id == company_id, CardSnapshot.platform == platform)
        .order_by(CardSnapshot.collected_at.desc(), CardSnapshot.id.desc())
    ).all()
    return tuple(
        SnapshotEvidence(
            match_status=row.match_status,
            url=row.url,
            collected_at=row.collected_at,
            status=row.status,
            purpose=row.purpose,
            collected_by=row.collected_by,
        )
        for row in rows
    )


def _stored_url(stored: CompanyPlatformId | None, work: Mapping[str, Any]) -> str | None:
    if stored is not None and stored.public_url:
        return stored.public_url
    listed = work.get("public_url")
    if isinstance(listed, str) and listed.strip():
        return listed
    return None


def _blocked(session: Session, folder: Path, order_id: int, platform: str, status: str, detail: str) -> PrepareReport:
    report = _result(order_id, platform, status, detail, (), False, MODE)
    _emit(folder, report)
    _escalate(session, order_id, platform, status, detail, str(folder / "review.json"))
    return report


def _stop(
    folder: Path,
    order_id: int,
    platform: str,
    status: str,
    detail: str,
    view: Any,
    *,
    shot_before: bool = True,
) -> PrepareReport:
    report = _result(order_id, platform, status, detail, (), False, MODE)
    _emit(folder, report, view, store_page=True, prepared=False, shot_before=shot_before)
    return report


def _escalate(session: Session, order_id: int, platform: str, status: str, detail: str, evidence: str) -> None:
    reason = TASKS.get(status)
    if reason is None:
        return
    ensure_listing_task(session, order_id, platform, reason, detail, evidence)
