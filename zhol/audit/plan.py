"""Turn audit evidence into listing tasks. A finding is not a completed edit."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zhol.browser.config import load_platforms
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter
from zhol.fulfillment.adapters.twogis import TwoGisAdapter
from zhol.fulfillment.adapters.yandex import YandexBusinessAdapter
from zhol.fulfillment.capabilities import load_cabinet
from zhol.fulfillment.facts import approved_fact
from zhol.guards import is_nap_field

TASK_TYPES = (
    "create_listing_candidate",
    "update_listing",
    "add_description",
    "update_contacts",
    "update_hours",
    "add_services",
    "add_prices",
    "request_real_photos",
    "prepare_review_replies",
    "request_owner_verification",
    "reconcile_platform_data",
    "request_client_facts",
)
STATUSES = frozenset({"ready_for_preparation", "blocked", "unsupported", "manual"})
MANUAL_TYPES = frozenset(
    {
        "prepare_review_replies",
        "request_owner_verification",
        "request_client_facts",
        "request_real_photos",
        "reconcile_platform_data",
    }
)
PREPARE_COMMAND = {
    "2gis": "prepare-2gis-listing",
    "yandex": "prepare-yandex-listing",
    "google": "prepare-google-listing",
}
FORBIDDEN = {
    "2gis": TwoGisAdapter.forbidden,
    "yandex": YandexBusinessAdapter.forbidden,
    "google": GoogleBusinessAdapter.forbidden,
}
FACT_FOR_FIELD = {
    "name": "name",
    "address": "address",
    "phones": "phones",
    "category": "category",
    "hours": "hours",
    "description": "description",
    "services": "services",
    "link": "website",
    "website": "website",
    "whatsapp": "messengers.whatsapp",
    "instagram": "socials.instagram",
}
# 2GIS writes these only when the open cabinet already shows the control. This import does not open one.
UNCONFIRMED_CONTROLS = frozenset({("2gis", "whatsapp"), ("2gis", "instagram")})
FILLABLE = frozenset({"text", "phones", "services"})
PHOTOS_BELOW = 5
REPLY_SHARE_BELOW = 0.2
PLATFORMS = ("2gis", "yandex", "google")
COMPLETED = False


def editor_support(platform: str, field: str | None) -> tuple[bool, str | None]:
    if field is None:
        return False, "no editor field"
    if field in FORBIDDEN[platform]:
        return False, "the adapter does not write this field"
    spec = load_cabinet(platform).fields.get(field)
    controls = set((load_platforms()["platforms"][platform]["cabinet"]["editor"]["fields"]))
    if spec is None or spec.shape not in FILLABLE or field not in controls:
        return False, "the supervised prepare step does not fill this control"
    return True, None


def findings_for(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """evidence is one company: platforms, observations, sheet, source_date."""
    found: list[dict[str, Any]] = []
    observations = [text for text in evidence.get("observations") or [] if str(text).strip()]
    if observations:
        found.append(
            _finding(
                "reconcile_platform_data",
                "2gis",
                {"text": observations},
                {"resolved": True},
                _evidence(evidence, "2gis", "discrepancies", observations),
                field=None,
            )
        )
    for platform in PLATFORMS:
        card = (evidence.get("platforms") or {}).get(platform)
        if not card or card.get("skipped_newer"):
            continue
        found.extend(_platform_findings(evidence, platform, card))
    if evidence.get("google_listing") == "needs_verification":
        found.append(
            _finding(
                "reconcile_platform_data",
                "google",
                {"match_status": "needs_verification"},
                {"match_status": "verified"},
                _evidence(
                    evidence,
                    "google",
                    "match_status",
                    "не найдена",
                    {"match_status": {"seen_at": evidence.get("source_date"), "csv_column": "Карточка"}},
                ),
                field=None,
            )
        )
    return found


def classify(findings: list[dict[str, Any]], readiness: Mapping[str, Any], branch_id: int) -> list[dict[str, Any]]:
    tasks = [_task(item, readiness, branch_id) for item in findings]
    for platform in PLATFORMS:
        missing = _missing_facts(tasks, platform)
        if not missing:
            continue
        extra = _finding(
            "request_client_facts",
            platform,
            {"facts": missing},
            {"facts": "approved client values"},
            {"field_ref": f"{platform}.facts", "value": missing, "source": "bek_audit"},
            field=None,
        )
        tasks.append(_task(extra, readiness, branch_id))
    return tasks


def platform_status(tasks: list[dict[str, Any]], *, skipped: bool) -> str:
    if skipped:
        return "skipped_newer_verified"
    if not tasks:
        return "no_action"
    statuses = {item["status"] for item in tasks}
    for status in ("blocked", "ready_for_preparation", "manual", "unsupported"):
        if status in statuses:
            return status
    return "no_action"


def _platform_findings(evidence: Mapping[str, Any], platform: str, card: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = card.get("fields") or {}
    sources = card.get("sources") or {}
    status = card.get("match_status")
    found: list[dict[str, Any]] = []
    if status == "not_found":
        found.append(
            _finding(
                "create_listing_candidate",
                platform,
                {"match_status": "not_found"},
                {"match_status": "found"},
                _evidence(evidence, platform, "match_status", "not_found", sources),
                field=None,
            )
        )
        return found
    if status == "other_address":
        found.append(
            _finding(
                "reconcile_platform_data",
                platform,
                {"match_status": "other_address"},
                {"match_status": "found"},
                _evidence(evidence, platform, "match_status", "other_address", sources),
                field=None,
            )
        )
        return found
    if status != "found":
        return found
    found.extend(_blank_fields(evidence, platform, fields, sources))
    found.extend(_prices(evidence, platform, fields, sources))
    found.extend(_photos(evidence, platform, fields, sources))
    found.extend(_replies(evidence, platform, fields, sources))
    found.extend(_owner(evidence, platform, fields, sources))
    return found


def _blank_fields(evidence, platform, fields, sources) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if _blank(platform, "description", fields, sources):
        found.append(_gap(evidence, platform, "add_description", "description", fields, sources, {"description": "client text"}))
    if _blank(platform, "hours", fields, sources):
        found.append(_gap(evidence, platform, "update_hours", "hours", fields, sources, {"hours": "client hours"}))
    if _blank(platform, "category", fields, sources):
        found.append(_gap(evidence, platform, "update_listing", "category", fields, sources, {"category": "client rubric"}))
    contacts = {
        "phones": "phones",
        "whatsapp": "whatsapp",
        "instagram": "instagram",
        "link": "link",
        "website": "website",
    }
    for key, editor_field in contacts.items():
        if platform == "2gis" and key == "website":
            continue
        if platform != "2gis" and key == "link":
            continue
        if key in {"phones", "whatsapp", "instagram"} and platform == "2gis":
            flag = {"phones": "has_phone", "whatsapp": "has_whatsapp", "instagram": "has_instagram"}[key]
            if not _observed(sources, flag) or fields.get(flag) is not False:
                continue
            current = {flag: False}
        elif not _blank(platform, key, fields, sources):
            continue
        else:
            current = {key: fields.get(key)}
        desired_key = "website" if key in {"link", "website"} else key
        found.append(
            _gap(evidence, platform, "update_contacts", editor_field, fields, sources, {desired_key: "client value"}, current=current)
        )
    return found


def _prices(evidence, platform, fields, sources) -> list[dict[str, Any]]:
    if not _observed(sources, "prices"):
        return []
    value = fields.get("prices")
    if platform == "2gis" and value in {"empty", "photo_only"}:
        return [
            _gap(evidence, platform, "add_prices", "services", fields, sources, {"prices": "company_price"}, current={"prices": value}),
            _gap(
                evidence,
                platform,
                "add_services",
                "services",
                fields,
                sources,
                {"services": "client service list"},
                current={"prices": value},
            ),
        ]
    if platform == "yandex" and value == "no":
        return [
            _gap(evidence, platform, "add_prices", "services", fields, sources, {"prices": "yes"}, current={"prices": value}),
            _gap(
                evidence,
                platform,
                "add_services",
                "services",
                fields,
                sources,
                {"services": "client service list"},
                current={"prices": value},
            ),
        ]
    if platform == "yandex" and value == "yes_outdated":
        return [
            _gap(
                evidence,
                platform,
                "add_prices",
                "services",
                fields,
                sources,
                {"prices": "yes"},
                current={"prices": value},
            )
        ]
    return []


def _photos(evidence, platform, fields, sources) -> list[dict[str, Any]]:
    if not _observed(sources, "photos_count"):
        return []
    count = fields.get("photos_count")
    if not isinstance(count, int) or isinstance(count, bool) or count >= PHOTOS_BELOW:
        return []
    return [
        _finding(
            "request_real_photos",
            platform,
            {"photos_count": count},
            {"photos": "real client photos"},
            _evidence(evidence, platform, "photos_count", count, sources),
            field="photos",
            materials="photo_real",
        )
    ]


def _replies(evidence, platform, fields, sources) -> list[dict[str, Any]]:
    if platform != "2gis":
        return []
    if not _observed(sources, "replies_count") or not _observed(sources, "reviews_shown"):
        return []
    replies, shown = fields.get("replies_count"), fields.get("reviews_shown")
    if not isinstance(replies, int) or not isinstance(shown, int) or isinstance(replies, bool) or isinstance(shown, bool):
        return []
    if shown <= 0 or replies / shown >= REPLY_SHARE_BELOW:
        return []
    return [
        _finding(
            "prepare_review_replies",
            platform,
            {"replies_count": replies, "reviews_shown": shown},
            {"replies_for_shown_reviews": True},
            _evidence(evidence, platform, "replies_count", replies, sources),
            field=None,
        )
    ]


def _owner(evidence, platform, fields, sources) -> list[dict[str, Any]]:
    key = "owner_claimed" if platform == "google" else "owner_maintains" if platform == "yandex" else None
    if key is None or not _observed(sources, key) or fields.get(key) is not False:
        return []
    return [
        _finding(
            "request_owner_verification",
            platform,
            {key: False},
            {key: True},
            _evidence(evidence, platform, key, False, sources),
            field=key,
        )
    ]


def _gap(evidence, platform, task_type, field, fields, sources, desired, current=None) -> dict[str, Any]:
    column_key = field if current is None else next(iter(current))
    if current is None:
        current = {field: fields.get(field)}
    return _finding(
        task_type,
        platform,
        current,
        desired,
        _evidence(evidence, platform, column_key, current.get(column_key, fields.get(field)), sources),
        field=field,
    )


def _finding(task_type, platform, current, desired, evidence, *, field, materials=None) -> dict[str, Any]:
    supported, reason = (False, "prepare command does not create a listing") if task_type == "create_listing_candidate" else editor_support(platform, field)
    if task_type in MANUAL_TYPES:
        supported, reason = False, reason or "not a listing-editor write"
    lane = "automatic_preparation" if supported else "manual"
    fact = FACT_FOR_FIELD.get(field) if supported else None
    confirmed = (platform, field) not in UNCONFIRMED_CONTROLS if supported else False
    return {
        "type": task_type,
        "platform": platform,
        "current": current,
        "desired": desired,
        "evidence": evidence,
        "editor_field": field,
        "editor_supported": supported,
        "editor_reason": reason,
        "fact": fact,
        "materials": materials,
        "owner": bool(supported),
        "nap": bool(supported and field and is_nap_field(field)),
        "cabinet_confirmed": confirmed,
        "lane": lane,
    }


def _task(finding: Mapping[str, Any], readiness: Mapping[str, Any], branch_id: int) -> dict[str, Any]:
    dependencies = _dependencies(finding, readiness)
    status = _status(finding, dependencies)
    if status not in STATUSES:
        raise RuntimeError(status)
    command = PREPARE_COMMAND[finding["platform"]] if finding["editor_supported"] else None
    return {
        "type": finding["type"],
        "platform": finding["platform"],
        "branch_id": branch_id,
        "status": status,
        "lane": finding["lane"],
        "current": finding["current"],
        "desired": finding["desired"],
        "evidence": finding["evidence"],
        "editor": {
            "supported": finding["editor_supported"],
            "field": finding["editor_field"],
            "command": command,
            "reason": finding["editor_reason"],
        },
        "dependencies": dependencies,
        "completed": COMPLETED,
    }


def _status(finding: Mapping[str, Any], dependencies: list[dict[str, Any]]) -> str:
    if finding["type"] == "create_listing_candidate" or (
        finding["editor_field"] and not finding["editor_supported"] and finding["type"] not in MANUAL_TYPES
    ):
        return "unsupported"
    if not finding["editor_supported"]:
        return "manual"
    blocking = [item for item in dependencies if item["blocks"] == "preparation" and not item["met"]]
    if blocking:
        return "blocked"
    return "ready_for_preparation"


def _dependencies(finding: Mapping[str, Any], readiness: Mapping[str, Any]) -> list[dict[str, Any]]:
    fact = finding.get("fact")
    fact_met, fact_detail = _fact_met(readiness, fact)
    materials = finding.get("materials")
    materials_met = not materials or int(readiness.get("photo_files") or 0) > 0
    owner_met = (readiness.get("access") or {}).get(finding["platform"]) == "verified"
    if finding["editor_supported"]:
        cabinet_met = bool(finding["cabinet_confirmed"])
        cabinet_detail = finding["editor_field"] if cabinet_met else "cabinet control was not confirmed"
    else:
        cabinet_met = False
        cabinet_detail = finding["editor_reason"] or "not an editor control"
    nap = bool(finding.get("nap"))
    consent = (finding["platform"], finding.get("editor_field")) in set(readiness.get("nap_consents") or ())
    return [
        {
            "kind": "approved_business_facts",
            "met": fact_met if fact else True,
            "detail": fact_detail,
            "blocks": "preparation" if fact else "none",
        },
        {
            "kind": "client_materials",
            "met": materials_met,
            "detail": materials or "not required",
            "blocks": "preparation" if materials and finding["editor_supported"] else "none",
        },
        {
            "kind": "ownership_verification",
            "met": owner_met if finding["owner"] else True,
            "detail": (readiness.get("access") or {}).get(finding["platform"]) or "none",
            "blocks": "preparation" if finding["owner"] else "none",
        },
        {
            "kind": "supported_cabinet_controls",
            "met": cabinet_met,
            "detail": cabinet_detail,
            "blocks": "preparation" if finding["editor_supported"] else "none",
        },
        {
            "kind": "human_approval",
            "met": consent if nap else False,
            "detail": "nap consent" if nap else "approval stays with the prepare review task",
            "blocks": "preparation" if nap else "submit",
        },
    ]


def _fact_met(readiness: Mapping[str, Any], fact: str | None) -> tuple[bool, str]:
    if not fact:
        return True, "not required"
    result = approved_fact(readiness.get("profile") or {}, fact)
    return result.status == "approved", fact if result.status == "approved" else f"{fact} {result.status}"


def _missing_facts(tasks: list[dict[str, Any]], platform: str) -> list[str]:
    missing: list[str] = []
    for task in tasks:
        if task["platform"] != platform or task["lane"] != "automatic_preparation":
            continue
        for item in task["dependencies"]:
            if item["kind"] == "approved_business_facts" and item["blocks"] == "preparation" and not item["met"]:
                detail = str(item["detail"]).split()[0]
                if detail and detail not in missing:
                    missing.append(detail)
    return missing


def _blank(platform: str, key: str, fields: Mapping[str, Any], sources: Mapping[str, Any]) -> bool:
    del platform
    if not _observed(sources, key):
        return False
    value = fields.get(key)
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _observed(sources: Mapping[str, Any], key: str) -> bool:
    source = sources.get(key) or {}
    return bool(source.get("csv_column")) and not source.get("formula")


def _evidence(evidence, platform, key, value, sources=None) -> dict[str, Any]:
    source = (sources or {}).get(key) or {}
    return {
        "field_ref": f"{platform}.{key}",
        "value": value,
        "seen_at": source.get("seen_at") or evidence.get("source_date"),
        "source": "bek_audit",
        "sheet": evidence.get("sheet"),
        "column": source.get("csv_column"),
    }

