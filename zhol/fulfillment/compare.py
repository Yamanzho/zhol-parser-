"""Compare an approved value with the listing value already stored for that platform."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from zhol.fulfillment.capabilities import CabinetSpec, FieldSpec
from zhol.fulfillment.content import ContentLibrary, FileRef
from zhol.fulfillment.facts import FactValue, approved_fact, public_services
from zhol.fulfillment.types import FieldPlan
from zhol.guards import is_nap_field, listing_media_allowed, refuse_unapproved_fact


def collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def phone_numbers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = re.split(r"[,;\n]+", value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = [str(item) for item in value]
    else:
        return ()
    return tuple(sorted(re.sub(r"\D", "", part) for part in parts if re.sub(r"\D", "", part)))


def same_value(shape: str, before: Any, after: Any) -> bool:
    if shape == "phones":
        left = phone_numbers(before)
        right = phone_numbers(after)
        return bool(left) and left == right
    if shape == "services":
        return public_services(before) == public_services(after) and bool(public_services(after))
    if shape == "text":
        if not isinstance(before, str) or not isinstance(after, str):
            return False
        return bool(collapse(before)) and collapse(before) == collapse(after)
    return before == after


def blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False


def coerce_page(shape: str, text: str) -> Any:
    cleaned = text.strip()
    if shape == "phones":
        return [part.strip() for part in re.split(r"[,;\n]+", cleaned) if part.strip()]
    if cleaned == "":
        return None
    return cleaned


def photos_empty(value: Any) -> bool | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) == 0
    return None


def build_field_plans(
    *,
    platform: str,
    action: str,
    spec: CabinetSpec,
    profile: Mapping[str, Any],
    company_name: str,
    files: Mapping[str, tuple[FileRef, ...]],
    library: ContentLibrary,
    current: Mapping[str, Any],
    consents: Sequence[tuple[str, str, Any, str]],
) -> tuple[FieldPlan, ...]:
    plans: list[FieldPlan] = []
    mapped_roots: set[str] = set()
    for field in spec.fields.values():
        if field.profile:
            mapped_roots.add(field.profile.split(".", 1)[0])
        plan = _field_plan(
            platform=platform,
            action=action,
            field=field,
            profile=profile,
            company_name=company_name,
            files=files,
            library=library,
            current=current,
            consents=consents,
        )
        if plan is not None:
            plans.append(plan)
    for key in sorted(profile):
        if key in mapped_roots:
            continue
        fact = approved_fact(profile, key)
        if fact.status != "approved" or blank(fact.value):
            continue
        plans.append(
            _plan(
                platform,
                key,
                "unsupported",
                None,
                None,
                fact.source_ref,
                False,
                None,
                "this platform has no such listing field",
                "text",
            )
        )
    return tuple(plans)


def _field_plan(
    *,
    platform: str,
    action: str,
    field: FieldSpec,
    profile: Mapping[str, Any],
    company_name: str,
    files: Mapping[str, tuple[FileRef, ...]],
    library: ContentLibrary,
    current: Mapping[str, Any],
    consents: Sequence[tuple[str, str, Any, str]],
) -> FieldPlan | None:
    if field.source == "profile":
        return _profile_plan(platform, action, field, profile, company_name, current, consents, library)
    if field.source == "client_file":
        return _file_plan(platform, action, field, files, current)
    return _content_plan(platform, field, library)


def _profile_plan(
    platform: str,
    action: str,
    field: FieldSpec,
    profile: Mapping[str, Any],
    company_name: str,
    current: Mapping[str, Any],
    consents: Sequence[tuple[str, str, Any, str]],
    library: ContentLibrary,
) -> FieldPlan | None:
    assert field.profile is not None
    fact = approved_fact(profile, field.profile)
    if fact.status == "absent" and field.profile == "name" and company_name.strip():
        fact = FactValue("approved", company_name.strip(), "field:company.name")
    if fact.status == "unapproved":
        try:
            refuse_unapproved_fact(field.field, False)
        except PermissionError:
            return _plan(
                platform,
                field.field,
                "waiting_approval",
                _current(field, current),
                None,
                fact.source_ref,
                _nap(field),
                None,
                "the client fact is not approved",
                field.shape,
            )
        return None
    if fact.status != "approved" or blank(fact.value):
        if field.field == "description" and fact.status == "absent":
            piece = _generated_description(library, platform)
            if piece is not None and piece.text:
                fact = FactValue("approved", piece.text, f"content:{piece.id}")
            else:
                return None
        else:
            return None
    refuse_unapproved_fact(field.field, True)
    after = public_services(fact.value) if field.shape == "services" else fact.value
    if blank(after):
        return None
    before = _current(field, current)
    if str(fact.source_ref or "").startswith("content:") and not blank(before) and not same_value(field.shape, before, after):
        return None
    if same_value(field.shape, before, after):
        return _plan(platform, field.field, "already", before, after, fact.source_ref, _nap(field), None, None, field.shape)
    consent = fact.source_ref if action == "create" and _nap(field) else None
    if _nap(field) and action == "update":
        consent = _find_consent(consents, platform, field.field, field.shape, after)
        if not consent:
            return _plan(
                platform,
                field.field,
                "waiting_nap_consent",
                before,
                after,
                fact.source_ref,
                True,
                None,
                "name, address, phone, or category needs a separate client yes",
                field.shape,
            )
    return _plan(
        platform,
        field.field,
        "set",
        before,
        after,
        fact.source_ref,
        _nap(field),
        consent,
        None,
        field.shape,
    )


def _file_plan(
    platform: str,
    action: str,
    field: FieldSpec,
    files: Mapping[str, tuple[FileRef, ...]],
    current: Mapping[str, Any],
) -> FieldPlan | None:
    assert field.file_kind is not None
    rows = tuple(sorted(files.get(field.file_kind, ()), key=lambda item: item.id))
    if not rows:
        return None
    after = [{"file_id": item.id, "path": item.path} for item in rows]
    source_ref = f"file:{rows[0].id}"
    before = _current(field, current)
    if action == "create":
        return _plan(platform, field.field, "set", None, after, source_ref, False, None, None, field.shape)
    if field.on_update == "when_empty" and photos_empty(before) is True:
        return _plan(platform, field.field, "set", before, after, source_ref, False, None, None, field.shape)
    return _plan(
        platform,
        field.field,
        "not_compared",
        before,
        after,
        source_ref,
        False,
        None,
        "the listing photo inventory was not compared",
        field.shape,
    )


def _content_plan(platform: str, field: FieldSpec, library: ContentLibrary) -> FieldPlan | None:
    assert field.content_kind is not None
    piece = library.approved_for(field.content_kind, platform)
    if piece is not None:
        try:
            listing_media_allowed(piece.kind, status="checked", illustrative=True)
        except PermissionError:
            piece = None
    if piece is not None:
        after = {"content_id": piece.id, "path": piece.path, "kind": piece.kind, "illustrative": True}
        if piece.origin:
            after["origin"] = piece.origin
        return _plan(platform, field.field, "set", None, after, f"content:{piece.id}", False, None, None, field.shape)
    if field.content_kind in library.pending_kinds:
        return _plan(
            platform,
            field.field,
            "content_unavailable",
            None,
            None,
            None,
            False,
            None,
            "approved illustrative content is not available",
            field.shape,
        )
    return None


def _generated_description(library: ContentLibrary, platform: str):
    piece = library.approved_text("platform_description", platform)
    if piece is None:
        piece = library.approved_text("company_description", None)
    return piece


def _current(field: FieldSpec, current: Mapping[str, Any]) -> Any:
    key = field.snapshot_key or field.field
    return current.get(key)


def _nap(field: FieldSpec) -> bool:
    return field.nap or is_nap_field(field.field)


def _find_consent(
    consents: Sequence[tuple[str, str, Any, str]],
    platform: str,
    field: str,
    shape: str,
    after: Any,
) -> str | None:
    for item_platform, item_field, item_after, ref in consents:
        if item_platform == platform and item_field == field and ref and same_value(shape, item_after, after):
            return ref
    return None


def _plan(
    platform: str,
    field: str,
    operation: str,
    before: Any,
    after: Any,
    source_ref: str | None,
    is_nap: bool,
    nap_consent_ref: str | None,
    reason: str | None,
    shape: str,
) -> FieldPlan:
    return FieldPlan(
        platform=platform,
        field=field,
        operation=operation,
        before=before,
        after=after,
        source_ref=source_ref,
        is_nap=is_nap,
        nap_consent_ref=nap_consent_ref,
        reason=reason,
        shape=shape,
    )
