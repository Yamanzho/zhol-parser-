"""Prepare approved cabinet edits and stop before Save.

The public card is not published. A later stage has to be approved by a person
before anything is submitted. This step does not create a listing or switch accounts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from zhol.browser.cabinet.classify import classify_pause
from zhol.db.models import CompanyPlatformId
from zhol.browser.cabinet.editor import ControlSpec, EditorSpec
from zhol.browser.cabinet.session import read_local_html
from zhol.browser.config import load_platforms
from zhol.fulfillment.adapters.base import CabinetAdapter
from zhol.fulfillment.compare import build_field_plans, collapse
from zhol.fulfillment.content import load_content, load_files
from zhol.fulfillment.engine import (
    FulfillmentRefused,
    _ambiguities,
    _consents,
    _granted,
    _order_context,
    _platforms,
)
from zhol.fulfillment.facts import critical_missing, public_services
from zhol.fulfillment.store import (
    PREPARED_SUMMARIES,
    ensure_pause_task,
    ensure_review_task,
    persist_prepared,
    prepared_already,
)
from zhol.fulfillment.types import FieldPlan
from zhol.guards import no_long_dash, refuse_listing_submit
from zhol.timeutil import now

HELD = frozenset({"waiting_nap_consent", "waiting_approval"})
SECRET_PAUSE = frozenset({"sms", "login"})
FILLABLE = frozenset({"text", "phones", "services"})


@dataclass(frozen=True)
class FieldChange:
    field: str
    action: str
    before: Any
    after: Any


@dataclass(frozen=True)
class PrepareReport:
    order_id: int
    platform: str
    status: str
    detail: str | None
    submitted: bool
    published: bool
    approval_required: bool
    changes: tuple[FieldChange, ...]
    mode: str = "cabinet_prepare"

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "mode": self.mode,
            "platform": self.platform,
            "status": self.status,
            "detail": self.detail,
            "submitted": False,
            "published": False,
            "approval_required": self.approval_required,
            "changes": [
                {"field": item.field, "action": item.action, "before": item.before, "after": item.after}
                for item in self.changes
            ],
            "at": now().isoformat(),
        }


def format_prepare(report: PrepareReport) -> str:
    approval = "required" if report.approval_required else "not_required"
    lines = [
        f"order {report.order_id}",
        f"mode {report.mode}",
        f"platform {report.platform}",
        f"status {report.status}",
        "submitted false",
        "published false",
        f"approval {approval}",
    ]
    if report.detail:
        lines.append(f"detail {report.detail}")
    for item in report.changes:
        if item.action == "already":
            continue
        lines.append(f"{item.field} {item.action}")
    return "\n".join(lines)


def wait_for_person(
    view: Any,
    pause: Mapping[str, tuple[str, ...] | list[str]],
    *,
    wait_seconds: float,
    sleeper: Callable[[float], None],
) -> tuple[str, str | None]:
    """Read visible text until the cabinet is usable, or the wait ends. Inputs are not read."""
    text = view.body_text()
    kind = classify_pause(text, pause)
    if kind is None or wait_seconds <= 0:
        return text, kind
    deadline = time.monotonic() + wait_seconds
    while kind is not None:
        if time.monotonic() >= deadline:
            return text, kind
        sleeper(1)
        text = view.body_text()
        kind = classify_pause(text, pause)
    return text, kind


def prepare_cabinet(
    session: Session,
    order_id: int,
    *,
    page: Any,
    evidence_dir: str | Path,
    platform: str,
    adapter: CabinetAdapter,
    editor: EditorSpec,
    fixture_cls: type,
    open_live: Callable[..., Any],
    review_reason: str,
    create_detail: str,
    wait_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
    browser: Any = None,
    approve: bool = False,
) -> PrepareReport:
    """Fill approved fields and stop before Save. approve=True is refused."""
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
    if work["action"] != "update":
        return _emit(folder, _result(order.id, platform, "stopped", create_detail, (), False))
    if not _granted(session, company.id, platform, work):
        return _emit(folder, _result(order.id, platform, "refused", "missing owner permission", (), False))
    stored = session.get(CompanyPlatformId, (company.id, platform))
    if stored is None or not stored.external_id:
        return _emit(folder, _result(order.id, platform, "stopped", "organization does not match the order", (), False))

    markers = load_platforms()["markers"]
    view, owned = _open(page, markers, browser, fixture_cls=fixture_cls, open_live=open_live)
    try:
        _text, kind = wait_for_person(view, adapter.spec.pause, wait_seconds=wait_seconds, sleeper=sleeper)
        if kind is not None:
            report = _result(order.id, platform, "paused", kind, (), False)
            _emit(folder, report, view, store_page=kind not in SECRET_PAUSE, prepared=False)
            ensure_pause_task(session, order.id, platform, kind, str(folder / "review.json"))
            return report
        if view.marker(markers["page_attr"]) != "cabinet" or view.marker(markers["platform_attr"]) != platform:
            report = _result(order.id, platform, "stopped", "stopped_unknown_ui", (), False)
            _emit(folder, report, view, store_page=True, prepared=False)
            return report
        page_id = (view.marker(markers["external_id_attr"]) or "").strip()
        shown = view.field_text(editor.organization_field) or ""
        if page_id != stored.external_id.strip() or collapse(shown).casefold() != collapse(company.name).casefold():
            report = _result(order.id, platform, "stopped", "organization does not match the order", (), False)
            _emit(folder, report, view, store_page=True, prepared=False)
            return report
        role = view.field_text(editor.access_field) or ""
        allowed = {collapse(item).casefold() for item in editor.access_allowed}
        if collapse(role).casefold() not in allowed:
            report = _result(order.id, platform, "refused", "missing owner permission", (), False)
            _emit(folder, report, view, store_page=True, prepared=False)
            return report
        return _fill(session, order.id, platform, company.name, profile, view, adapter, editor, folder, review_reason)
    finally:
        if owned:
            view.close()


def _fill(
    session: Session,
    order_id: int,
    platform: str,
    company_name: str,
    profile: Mapping[str, Any],
    view: Any,
    adapter: CabinetAdapter,
    editor: EditorSpec,
    folder: Path,
    review_reason: str,
) -> PrepareReport:
    current = _current(view, editor, adapter)
    plans = build_field_plans(
        platform=platform,
        action="update",
        spec=adapter.spec,
        profile=profile,
        company_name=company_name,
        files=load_files(session, order_id),
        library=load_content(session, order_id),
        current=current,
        consents=_consents(session, order_id),
    )
    changes, pending, missing = _select(session, order_id, plans, view, adapter, editor)
    folder.mkdir(parents=True, exist_ok=True)
    view.screenshot(folder / "before.png")
    if missing:
        report = _result(order_id, platform, "stopped", "stopped_unknown_ui", (), False)
        _emit(folder, report, view, store_page=True, prepared=False, shot_before=False)
        return report
    snapshot = _snapshot(view, editor)
    written: list[FieldPlan] = []
    for plan, control in pending:
        view.write_control(control.role, control.name, _fill_text(adapter.spec.fields[plan.field].shape, plan.after))
        written.append(plan)
    _unchanged(view, editor, snapshot, {control.name for _plan, control in pending})
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
            summary=PREPARED_SUMMARIES[platform],
        )
        ensure_review_task(
            session,
            order_id,
            tuple({"field": plan.field, "before": plan.before, "after": plan.after} for plan in written),
            str(folder / "review.json"),
            reason=review_reason,
            platform=platform,
        )
    status, approval = _status(changes, wrote=bool(written))
    report = _result(order_id, platform, status, None, changes, approval)
    _emit(folder, report, view, store_page=True, prepared=False, shot_before=False)
    return report


def _select(
    session: Session,
    order_id: int,
    plans: tuple[FieldPlan, ...],
    view: Any,
    adapter: CabinetAdapter,
    editor: EditorSpec,
) -> tuple[list[FieldChange], list[tuple[FieldPlan, ControlSpec]], list[str]]:
    changes: list[FieldChange] = []
    pending: list[tuple[FieldPlan, ControlSpec]] = []
    missing: list[str] = []
    for plan in plans:
        if plan.operation in HELD:
            changes.append(FieldChange(plan.field, "held", plan.before, plan.after))
            continue
        if plan.operation == "already":
            changes.append(FieldChange(plan.field, "already", plan.before, plan.after))
            continue
        if plan.operation != "set":
            changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        spec = adapter.spec.fields.get(plan.field)
        control = editor.fields.get(plan.field)
        if spec is None or control is None or spec.shape not in FILLABLE or plan.field in adapter.forbidden:
            changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        if view.control_value(control.role, control.name) is None:
            missing.append(plan.field)
            continue
        if prepared_already(session, order_id, plan.platform, plan.field, plan.after):
            changes.append(FieldChange(plan.field, "already", plan.before, plan.after))
            continue
        if not _fill_text(spec.shape, plan.after).strip():
            changes.append(FieldChange(plan.field, "not_filled", plan.before, plan.after))
            continue
        pending.append((plan, control))
        changes.append(FieldChange(plan.field, "prepared", plan.before, plan.after))
    return changes, pending, missing


def _current(view: Any, editor: EditorSpec, adapter: CabinetAdapter) -> dict[str, Any]:
    current: dict[str, Any] = {}
    for field, control in editor.fields.items():
        raw = view.control_value(control.role, control.name)
        if raw is None:
            continue
        current[field] = _coerce(adapter.spec.fields[field].shape, raw)
    return current


def _snapshot(view: Any, editor: EditorSpec) -> dict[str, str]:
    found: dict[str, str] = {}
    for control in editor.fields.values():
        raw = view.control_value(control.role, control.name)
        if raw is not None:
            found[control.name] = raw
    return found


def _unchanged(view: Any, editor: EditorSpec, snapshot: Mapping[str, str], written: set[str]) -> None:
    for control in editor.fields.values():
        if control.name in written or control.name not in snapshot:
            continue
        if view.control_value(control.role, control.name) != snapshot[control.name]:
            raise PermissionError("an unrelated field changed")


def _status(changes: list[FieldChange], *, wrote: bool) -> tuple[str, bool]:
    if wrote:
        return "prepared", True
    if any(item.action == "held" for item in changes):
        return "held", False
    return "already", False


def _coerce(shape: str, raw: str) -> Any:
    if shape == "services":
        return services_from_text(raw)
    if shape == "phones":
        return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    return raw.strip()


def services_from_text(text: str) -> Any:
    if not text.strip():
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if ":" not in cleaned:
            rows.append({"name": cleaned})
            continue
        name, price = cleaned.split(":", 1)
        digits = price.strip().replace(" ", "").replace("\u00a0", "")
        if not name.strip() or not digits.isdigit():
            return text
        rows.append({"name": name.strip(), "price": int(digits)})
    return rows


def format_services(value: Any) -> str:
    lines: list[str] = []
    for row in public_services(value):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if "price" in row:
            lines.append(f"{name}: {row['price']}")
        else:
            lines.append(name)
    return "\n".join(lines)


def _fill_text(shape: str, after: Any) -> str:
    if shape == "phones":
        text = ", ".join(str(item) for item in after) if isinstance(after, list) else str(after)
    elif shape == "services":
        text = format_services(after)
    else:
        text = "" if after is None else str(after)
    if not no_long_dash(text):
        raise PermissionError("prepared text must not use a long dash")
    return text


def _open(
    page: Any,
    markers: Mapping[str, str],
    browser: Any,
    *,
    fixture_cls: type,
    open_live: Callable[..., Any],
) -> tuple[Any, bool]:
    if hasattr(page, "body_text") and hasattr(page, "write_control"):
        return page, False
    text = str(page)
    if text.startswith("https://"):
        return open_live(text, playwright=browser), True
    if text.startswith(("http://", "file://")):
        raise PermissionError("the cabinet address must be https")
    return fixture_cls(read_local_html(text), markers), True


def _result(
    order_id: int,
    platform: str,
    status: str,
    detail: str | None,
    changes: list[FieldChange] | tuple[FieldChange, ...],
    approval: bool,
    mode: str = "cabinet_prepare",
) -> PrepareReport:
    return PrepareReport(order_id, platform, status, detail, False, False, approval, tuple(changes), mode)


def _emit(
    folder: Path,
    report: PrepareReport,
    view: Any = None,
    *,
    store_page: bool = False,
    prepared: bool = False,
    shot_before: bool = True,
) -> PrepareReport:
    folder.mkdir(parents=True, exist_ok=True)
    if view is not None and store_page:
        if shot_before:
            view.screenshot(folder / "before.png")
        (folder / "page.html").write_text(view.redacted_html(), encoding="utf-8")
    if view is not None and prepared:
        view.screenshot(folder / "prepared.png")
    (folder / "review.json").write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report
