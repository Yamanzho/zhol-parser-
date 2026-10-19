"""Platform adapters. apply_changes records a dry-run and does not send the form."""

from __future__ import annotations

from collections.abc import Sequence

from zhol.browser.cabinet.classify import classify_pause, page_is_cabinet, parse_cabinet
from zhol.browser.cabinet.session import CabinetStopped, require_human_check
from zhol.fulfillment.capabilities import CabinetSpec, load_cabinet
from zhol.fulfillment.compare import coerce_page, same_value
from zhol.fulfillment.types import (
    AccessCheck,
    ApplyResult,
    ListingContext,
    ListingView,
    PreparedField,
    PreparedListing,
    SubmitReaction,
    VerifyResult,
)
from zhol.guards import refuse_listing_submit, refuse_live_listing_submit, require_verified_owner


class CabinetAdapter:
    platform: str
    forbidden: frozenset[str] = frozenset()
    submit_buttons = {"create": "Создать", "update": "Сохранить"}

    def __init__(self, spec: CabinetSpec | None = None) -> None:
        self.spec = spec or load_cabinet(self.platform)
        if self.spec.platform != self.platform:
            raise ValueError(f"{self.platform} adapter received {self.spec.platform} settings")

    def validate_access(self, ctx: ListingContext) -> AccessCheck:
        if ctx.claimed_authorization != ctx.access_status:
            return AccessCheck("refused", "owner authorization mismatch")
        try:
            require_verified_owner(self.platform, ctx.access_status)
        except PermissionError:
            return AccessCheck("refused", "missing owner permission")
        if ctx.profile_status in {"needs_login", "blocked"}:
            try:
                require_human_check("login")
            except PermissionError:
                return AccessCheck("paused", ctx.profile_status or "needs_login")
        if not ctx.page_html:
            return AccessCheck("verified", None)
        pause = classify_pause(ctx.page_html, self.spec.pause)
        if pause is not None:
            try:
                require_human_check(pause)
            except PermissionError:
                return AccessCheck("paused", pause)
        document = parse_cabinet(ctx.page_html)
        if not page_is_cabinet(document, self.platform):
            return AccessCheck("stopped", "unexpected page")
        if ctx.action == "create" and document.external_id:
            return AccessCheck("stopped", "listing already has an id")
        if ctx.action == "update" and (not ctx.external_id or document.external_id != ctx.external_id):
            return AccessCheck("stopped", "organization does not match the order")
        return AccessCheck("verified", None)

    def inspect_listing(self, ctx: ListingContext) -> ListingView:
        page_fields = None
        external_id = ctx.external_id
        if ctx.page_html:
            document = parse_cabinet(ctx.page_html)
            if not page_is_cabinet(document, self.platform):
                raise CabinetStopped("unexpected page")
            if ctx.action == "update" and (not ctx.external_id or document.external_id != ctx.external_id):
                raise CabinetStopped("organization does not match the order")
            page_fields = dict(document.fields)
            external_id = document.external_id or ctx.external_id
        match = ctx.snapshot_match
        if match is None:
            match = "found" if external_id else "not_found"
        source = "saved_page" if page_fields is not None and not ctx.snapshot_fields else "snapshot"
        if page_fields is not None and ctx.snapshot_fields:
            source = "snapshot+saved_page"
        if page_fields is None and not ctx.snapshot_fields and ctx.snapshot_match is None:
            source = "none"
        return ListingView(
            external_id=external_id,
            match_status=match,
            page_fields=page_fields,
            screenshot_path=ctx.screenshot_path,
            source=source,
        )

    def prepare_create(self, ctx: ListingContext, fields: Sequence[PreparedField]) -> PreparedListing:
        if not self.spec.create:
            raise PermissionError(f"{self.platform} does not create listings in this batch")
        if ctx.action != "create":
            raise CabinetStopped("create was not requested")
        if ctx.external_id:
            raise CabinetStopped("listing already exists")
        kept, dropped = self._split(fields)
        return PreparedListing(self.platform, "create", None, kept, dropped, False)

    def prepare_update(self, ctx: ListingContext, fields: Sequence[PreparedField]) -> PreparedListing:
        if ctx.action != "update":
            raise CabinetStopped("update was not requested")
        if not ctx.external_id and not ctx.public_url:
            raise CabinetStopped("listing id is missing")
        kept, dropped = self._split(fields)
        return PreparedListing(self.platform, "update", ctx.external_id, kept, dropped, False)

    def apply_changes(self, ctx: ListingContext, prepared: PreparedListing) -> ApplyResult:
        refuse_listing_submit(prepared.submit)
        if prepared.platform != self.platform or prepared.action not in {"create", "update"}:
            raise CabinetStopped("prepared listing does not match this adapter")
        if any(field.field in self.forbidden or field.field not in self.spec.fields for field in prepared.fields):
            raise PermissionError("unsupported field")
        if prepared.action == "update" and ctx.external_id and prepared.external_id != ctx.external_id:
            raise CabinetStopped("organization does not match the order")
        return ApplyResult(
            result="dry_run",
            submitted=False,
            screenshot_before=ctx.screenshot_path,
            screenshot_after=None,
            details={"submitted": False, "mode": "dry_run", "platform": self.platform},
        )

    def offline_submit(self, operation: str, reaction: SubmitReaction) -> ApplyResult:
        """Interpret a fake Save or Create response. The click does not mean published."""
        refuse_live_listing_submit("offline")
        button = self.submit_buttons.get(operation)
        if button is None or button in {"Опубликовать", "Publish", "Submit", "Отправить"}:
            raise PermissionError("publish is not a submit action")
        if reaction.button != button:
            status = "uncertain"
            accepted = False
        elif reaction.outcome == "network_failure" and reaction.request_sent == "false":
            status = "approved"
            accepted = False
        elif reaction.outcome in {"network_failure", "browser_crash"} or reaction.outcome not in {
            "submitted",
            "moderation_pending",
            "rejected",
        }:
            status = "uncertain"
            accepted = False
        else:
            status = reaction.outcome
            accepted = True
        return ApplyResult(
            result=status,
            submitted=accepted,
            screenshot_before=None,
            screenshot_after=None,
            details={
                "published": False,
                "status": status,
                "button": button,
                "mode": "offline",
                "platform": self.platform,
                "operation": operation,
                "confirmation": reaction.confirmation,
                "claimed_published": reaction.claimed_published,
            },
        )

    def verify_changes(self, ctx: ListingContext, prepared: PreparedListing, applied: ApplyResult) -> VerifyResult:
        if applied.submitted or applied.result != "dry_run" or applied.screenshot_after is not None:
            return VerifyResult(False, "the form was not supposed to be sent")
        if prepared.submit:
            return VerifyResult(False, "the form was not supposed to be sent")
        if prepared.action == "update" and ctx.external_id and prepared.external_id != ctx.external_id:
            return VerifyResult(False, "organization does not match the order")
        view = self.inspect_listing(ctx) if ctx.page_html else None
        for field in prepared.fields:
            if field.field in self.forbidden or field.field not in self.spec.fields:
                return VerifyResult(False, f"{field.field} is not writable")
            if view is None or not view.page_fields or field.field not in view.page_fields:
                continue
            seen = coerce_page(field.shape, view.page_fields[field.field])
            if same_value(field.shape, seen, field.after) and not same_value(field.shape, field.before, field.after):
                return VerifyResult(False, f"{field.field} already shows the new value")
        return VerifyResult(True, "dry_run")

    def _split(self, fields: Sequence[PreparedField]) -> tuple[tuple[PreparedField, ...], tuple[str, ...]]:
        kept: list[PreparedField] = []
        dropped: list[str] = []
        for field in fields:
            writable = field.field in self.spec.fields and field.field not in self.spec.read_only
            if field.field in self.forbidden or not writable:
                dropped.append(field.field)
                continue
            kept.append(field)
        return tuple(kept), tuple(dropped)
