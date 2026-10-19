"""Values shared by the listing plan and the platform adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PROBLEM_STATUSES = frozenset({"refused", "paused", "stopped", "failed"})
FIELD_BLOCKS = frozenset({"waiting_nap_consent", "waiting_approval"})


@dataclass(frozen=True)
class ListingContext:
    order_id: int
    company_id: int
    company_name: str
    platform: str
    action: str
    external_id: str | None
    public_url: str | None
    access_status: str
    claimed_authorization: str
    profile_status: str | None
    page_html: str | None
    snapshot_fields: Mapping[str, Any]
    snapshot_match: str | None
    screenshot_path: str | None


@dataclass(frozen=True)
class AccessCheck:
    status: str
    reason: str | None


@dataclass(frozen=True)
class ListingView:
    external_id: str | None
    match_status: str
    page_fields: dict[str, str] | None
    screenshot_path: str | None
    source: str


@dataclass(frozen=True)
class PreparedField:
    field: str
    shape: str
    before: Any
    after: Any
    source_ref: str


@dataclass(frozen=True)
class PreparedListing:
    platform: str
    action: str
    external_id: str | None
    fields: tuple[PreparedField, ...]
    dropped: tuple[str, ...]
    submit: bool


@dataclass(frozen=True)
class ApplyResult:
    result: str
    submitted: bool
    screenshot_before: str | None
    screenshot_after: str | None
    details: dict[str, Any]


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str


@dataclass(frozen=True)
class SubmitReaction:
    """Fake cabinet response. A click result is never treated as published."""

    button: str
    clicked: bool
    outcome: str
    confirmation: str | None
    request_sent: str
    claimed_published: bool


@dataclass(frozen=True)
class FieldPlan:
    platform: str
    field: str
    operation: str
    before: Any
    after: Any
    source_ref: str | None
    is_nap: bool
    nap_consent_ref: str | None
    reason: str | None
    shape: str


@dataclass(frozen=True)
class PlatformOutcome:
    platform: str
    action: str
    status: str
    plans: tuple[FieldPlan, ...]
    detail: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class ListingRun:
    order_id: int
    content: str
    outcomes: tuple[PlatformOutcome, ...]

    @property
    def ok(self) -> bool:
        if not self.outcomes:
            return False
        if any(item.status in PROBLEM_STATUSES for item in self.outcomes):
            return False
        return not any(plan.operation in FIELD_BLOCKS for item in self.outcomes for plan in item.plans)

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "mode": "dry_run",
            "content": self.content,
            "submitted": False,
            "outcomes": [
                {
                    "platform": item.platform,
                    "action": item.action,
                    "status": item.status,
                    "detail": item.detail,
                    "evidence": item.evidence,
                    "plans": [_plan_dict(plan) for plan in item.plans],
                }
                for item in self.outcomes
            ],
        }


def _plan_dict(plan: FieldPlan) -> dict[str, Any]:
    return {
        "field": plan.field,
        "operation": plan.operation,
        "before": plan.before,
        "after": plan.after,
        "source_ref": plan.source_ref,
        "is_nap": plan.is_nap,
        "nap_consent_ref": plan.nap_consent_ref,
        "reason": plan.reason,
    }
