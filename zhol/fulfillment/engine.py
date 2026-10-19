"""Plan and dry-run listing creates and updates. One platform failure stays on that platform."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.browser.cabinet.evidence import save_page
from zhol.browser.cabinet.session import CabinetStopped, read_local_html
from zhol.db.models import (
    BrowserProfile,
    CardSnapshot,
    ChangeItem,
    ChangePackage,
    Company,
    CompanyPlatformId,
    CompanyProfile,
    Intake,
    Order,
)
from zhol.fulfillment.adapters import GoogleBusinessAdapter, TwoGisAdapter, YandexBusinessAdapter
from zhol.fulfillment.adapters.base import CabinetAdapter
from zhol.fulfillment.capabilities import CabinetSpec
from zhol.fulfillment.compare import build_field_plans, coerce_page
from zhol.fulfillment.content import ContentLibrary, load_content, load_files
from zhol.fulfillment.facts import critical_missing
from zhol.fulfillment.store import ensure_pause_task, persist_sets
from zhol.fulfillment.types import FIELD_BLOCKS, FieldPlan, ListingContext, ListingRun, PlatformOutcome, PreparedField
from zhol.guards import no_long_dash

SUMMARY = "План правок, форма не отправлялась."


class FulfillmentError(ValueError):
    pass


class FulfillmentRefused(PermissionError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(", ".join(self.reasons))


def default_adapters() -> dict[str, CabinetAdapter]:
    return {
        "2gis": TwoGisAdapter(),
        "yandex": YandexBusinessAdapter(),
        "google": GoogleBusinessAdapter(),
    }


def run_listing(
    session: Session,
    order_id: int,
    *,
    pages: Mapping[str, str] | None = None,
    evidence_dir: str | Path | None = None,
    adapters: Mapping[str, CabinetAdapter] | None = None,
) -> ListingRun:
    """Build the field plan and log a dry-run. Nothing is typed into a cabinet form."""
    if not no_long_dash(SUMMARY):
        raise FulfillmentError("client summary must not use a long dash")
    order, company, checklist, profile = _order_context(session, order_id)
    ambiguities = _ambiguities(checklist)
    if ambiguities:
        raise FulfillmentRefused(tuple(f"unresolved ambiguity: {item}" for item in ambiguities))
    missing = critical_missing(profile, company.name)
    if missing:
        raise FulfillmentRefused(tuple(f"missing {name}" for name in missing))
    platforms = _platforms(checklist)
    if not platforms:
        raise FulfillmentRefused(("no target platforms",))
    if not any(_granted(session, company.id, name, row) for name, row in platforms.items()):
        raise FulfillmentRefused(("owner permission is missing",))

    library = load_content(session, order.id)
    files = load_files(session, order.id)
    consents = _consents(session, order.id)
    chosen = default_adapters()
    if adapters is not None:
        chosen.update(adapters)
    outcomes: list[PlatformOutcome] = []
    for name in sorted(platforms):
        try:
            outcomes.append(
                _one_platform(
                    session,
                    order=order,
                    company=company,
                    profile=profile,
                    work=platforms[name],
                    library=library,
                    files=files,
                    consents=consents,
                    adapter=chosen.get(name),
                    page_source=None if pages is None else pages.get(name),
                    evidence_dir=evidence_dir,
                )
            )
        except Exception as exc:
            outcomes.append(PlatformOutcome(name, platforms[name].get("action") or "update", "failed", (), str(exc)))
    report = ListingRun(order.id, library.availability, tuple(outcomes))
    if evidence_dir is not None:
        folder = Path(evidence_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plan.json").write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def format_run(report: ListingRun) -> str:
    lines = [f"order {report.order_id}", "mode dry_run", f"content {report.content}"]
    for item in report.outcomes:
        lines.append(f"platform {item.platform} {item.action} {item.status}")
        if item.detail:
            lines.append(f"  detail {item.detail}")
        for plan in item.plans:
            if plan.operation == "already":
                continue
            lines.append(f"  {plan.field} {plan.operation}")
    return "\n".join(lines)


def _one_platform(
    session: Session,
    *,
    order: Order,
    company: Company,
    profile: dict[str, Any],
    work: Mapping[str, Any],
    library: ContentLibrary,
    files: Mapping[str, Any],
    consents: tuple[tuple[str, str, Any, str], ...],
    adapter: CabinetAdapter | None,
    page_source: str | None,
    evidence_dir: str | Path | None,
) -> PlatformOutcome:
    platform = str(work["platform"])
    action = str(work["action"])
    if adapter is None:
        return PlatformOutcome(platform, action, "failed", (), "no listing adapter")
    stored = session.get(CompanyPlatformId, (company.id, platform))
    snapshot = _snapshot(session, company.id, platform)
    page_html = None if page_source is None else read_local_html(page_source)
    ctx = ListingContext(
        order_id=order.id,
        company_id=company.id,
        company_name=company.name,
        platform=platform,
        action=action,
        external_id=None if stored is None else stored.external_id,
        public_url=_public_url(stored, snapshot, work),
        access_status="none" if stored is None else stored.access_status,
        claimed_authorization=str(work.get("owner_authorization") or "none"),
        profile_status=_profile_status(session, platform),
        page_html=page_html,
        snapshot_fields={} if snapshot is None or not isinstance(snapshot.fields, dict) else dict(snapshot.fields),
        snapshot_match=None if snapshot is None else snapshot.match_status,
        screenshot_path=None if snapshot is None else snapshot.screenshot_path,
    )
    plans: tuple[FieldPlan, ...] = ()
    evidence = _save_evidence(evidence_dir, platform, page_html, "opened" if page_html else "not_opened")
    try:
        access = adapter.validate_access(ctx)
        if access.status != "verified":
            plans = _plans(ctx, adapter.spec, profile, company.name, files, library, consents, use_page=False)
            if access.status == "paused":
                ensure_pause_task(session, order.id, platform, access.reason or "paused", evidence)
            return PlatformOutcome(platform, action, access.status, plans, access.reason, evidence)
        view = adapter.inspect_listing(ctx)
        block = _action_block(action, ctx.external_id, view.match_status)
        plans = _plans(ctx, adapter.spec, profile, company.name, files, library, consents, use_page=True)
        if block is not None:
            return PlatformOutcome(platform, action, "refused", plans, block, evidence)
        missing = _missing_required(adapter.spec, action, plans)
        if missing:
            return PlatformOutcome(platform, action, "refused", plans, "missing " + ", ".join(missing), evidence)
        selected = [plan for plan in plans if plan.operation == "set"]
        if not selected:
            status = "held" if any(plan.operation in FIELD_BLOCKS for plan in plans) else "already"
            return PlatformOutcome(platform, action, status, plans, None, evidence)
        fields = [
            PreparedField(plan.field, plan.shape, plan.before, plan.after, plan.source_ref or "")
            for plan in selected
        ]
        prepared = adapter.prepare_create(ctx, fields) if action == "create" else adapter.prepare_update(ctx, fields)
        if prepared.dropped:
            return PlatformOutcome(platform, action, "refused", plans, "unsupported field", evidence)
        applied = adapter.apply_changes(ctx, prepared)
        verified = adapter.verify_changes(ctx, prepared, applied)
        if not verified.ok:
            return PlatformOutcome(platform, action, "failed", plans, verified.reason, evidence)
        flags = persist_sets(
            session,
            order.id,
            selected,
            screenshot_before=ctx.screenshot_path,
            evidence=evidence,
        )
        status = "dry_run" if any(not seen for seen in flags.values()) else "already"
        return PlatformOutcome(platform, action, status, plans, None, evidence)
    except CabinetStopped as exc:
        return PlatformOutcome(platform, action, "stopped", plans, str(exc), evidence)
    except PermissionError as exc:
        return PlatformOutcome(platform, action, "refused", plans, str(exc), evidence)


def _plans(
    ctx: ListingContext,
    spec: CabinetSpec,
    profile: Mapping[str, Any],
    company_name: str,
    files: Mapping[str, Any],
    library: ContentLibrary,
    consents: tuple[tuple[str, str, Any, str], ...],
    *,
    use_page: bool,
) -> tuple[FieldPlan, ...]:
    current = dict(ctx.snapshot_fields)
    if use_page and ctx.page_html:
        document_fields = _page_fields(ctx, spec)
        for field in spec.fields.values():
            if field.shape in {"file", "content"} or field.field not in document_fields:
                continue
            value = coerce_page(field.shape, document_fields[field.field])
            current[field.snapshot_key or field.field] = value
    return build_field_plans(
        platform=ctx.platform,
        action=ctx.action,
        spec=spec,
        profile=profile,
        company_name=company_name,
        files=files,
        library=library,
        current=current,
        consents=consents,
    )


def _page_fields(ctx: ListingContext, spec: CabinetSpec) -> dict[str, str]:
    from zhol.browser.cabinet.classify import parse_cabinet

    if not ctx.page_html:
        return {}
    document = parse_cabinet(ctx.page_html)
    if document.platform != spec.platform:
        return {}
    return dict(document.fields)


def _order_context(session: Session, order_id: int) -> tuple[Order, Company, dict[str, Any], dict[str, Any]]:
    order = session.get(Order, order_id)
    if order is None or order.company_id is None:
        raise FulfillmentError("order not found")
    company = session.get(Company, order.company_id)
    intake = session.get(Intake, order_id)
    if company is None or intake is None or not isinstance(intake.checklist, dict):
        raise FulfillmentError("order not found")
    profile_row = session.scalar(
        select(CompanyProfile)
        .where(CompanyProfile.company_id == company.id)
        .order_by(CompanyProfile.version.desc())
    )
    profile = profile_row.data if profile_row is not None and isinstance(profile_row.data, dict) else {}
    return order, company, intake.checklist, profile


def _platforms(checklist: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = checklist.get("platforms")
    if not isinstance(raw, Mapping):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for name, item in raw.items():
        if not isinstance(item, Mapping):
            continue
        rows[str(name)] = {
            "platform": str(name),
            "action": str(item.get("action") or ""),
            "owner_authorization": str(item.get("owner_authorization") or "none"),
            "external_id": item.get("external_id"),
            "public_url": item.get("public_url"),
        }
    return rows


def _ambiguities(checklist: Mapping[str, Any]) -> tuple[str, ...]:
    raw = checklist.get("ambiguities")
    if not isinstance(raw, list):
        return ()
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _granted(session: Session, company_id: int, platform: str, work: Mapping[str, Any]) -> bool:
    stored = session.get(CompanyPlatformId, (company_id, platform))
    actual = "none" if stored is None else stored.access_status
    return actual == "verified" and work.get("owner_authorization") == "verified"


def _consents(session: Session, order_id: int) -> tuple[tuple[str, str, Any, str], ...]:
    package_ids = select(ChangePackage.id).where(ChangePackage.order_id == order_id)
    rows = session.scalars(select(ChangeItem).where(ChangeItem.package_id.in_(package_ids))).all()
    return tuple(
        (row.platform, row.field, row.after, row.nap_consent_ref)
        for row in rows
        if isinstance(row.nap_consent_ref, str) and row.nap_consent_ref
    )


def _snapshot(session: Session, company_id: int, platform: str) -> CardSnapshot | None:
    return session.scalar(
        select(CardSnapshot)
        .where(CardSnapshot.company_id == company_id, CardSnapshot.platform == platform)
        .order_by(CardSnapshot.collected_at.desc(), CardSnapshot.id.desc())
    )


def _profile_status(session: Session, platform: str) -> str | None:
    rows = session.scalars(select(BrowserProfile).where(BrowserProfile.platform == platform)).all()
    if not rows:
        return None
    statuses = {row.status for row in rows}
    if "blocked" in statuses:
        return "blocked"
    if "ok" in statuses:
        return None
    if "needs_login" in statuses:
        return "needs_login"
    return "needs_login"


def _public_url(stored: CompanyPlatformId | None, snapshot: CardSnapshot | None, work: Mapping[str, Any]) -> str | None:
    if stored is not None and stored.public_url:
        return stored.public_url
    listed = work.get("public_url")
    if isinstance(listed, str) and listed:
        return listed
    if snapshot is not None and snapshot.url:
        return snapshot.url
    return None


def _action_block(action: str, external_id: str | None, match: str | None) -> str | None:
    if action not in {"create", "update"}:
        return "listing action is missing"
    if action == "create" and (external_id or match == "found"):
        return "listing already exists"
    if action == "update" and match == "other_address":
        return "listing address is ambiguous"
    if action == "update" and match == "not_found":
        return "listing was not found"
    if action == "update" and not external_id and match != "found":
        return "listing id is missing"
    return None


def _missing_required(spec: CabinetSpec, action: str, plans: tuple[FieldPlan, ...]) -> tuple[str, ...]:
    if action != "create":
        return ()
    ready = {plan.field for plan in plans if plan.operation in {"set", "already"}}
    return tuple(field.field for field in spec.fields.values() if field.required_on_create and field.field not in ready)


def _save_evidence(evidence_dir: str | Path | None, platform: str, html: str | None, status: str) -> str | None:
    if evidence_dir is None or html is None:
        return None
    return save_page(Path(evidence_dir) / platform, html, status=status)
