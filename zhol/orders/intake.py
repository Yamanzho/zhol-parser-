"""Local test-order intake and fulfillment readiness.

The order is created at ``created`` through ``transition()``. Payment edges are
not taken. Nothing here opens a browser or writes a listing.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zhol.db.models import (
    ChangeItem,
    ChangePackage,
    ClientFile,
    ClientMessage,
    Company,
    CompanyPlatformId,
    CompanyProfile,
    Credential,
    Deal,
    Intake,
    Order,
)
from zhol.guards import client_supplied_fact, is_nap_field, kzt, no_long_dash, require_verified_owner
from zhol.timeutil import now
from zhol.transitions import transition

QUOTES_KZT = {1: 30_000, 2: 50_000, 3: 75_000}
LOCAL_DEAL_PREFIX = "local-test:"
SUMMARY = "Локальный заказ: карточки не менялись."

PLATFORM_NAMES = {
    "2gis": "2gis",
    "2гис": "2gis",
    "google": "google",
    "google maps": "google",
    "yandex": "yandex",
    "yandex maps": "yandex",
}
ACTIONS = frozenset({"create", "update"})
AUTHORIZATION = frozenset({"none", "invited", "verified", "lost"})
FACT_FIELDS = frozenset(
    {
        "name",
        "address",
        "category",
        "description",
        "entrance_hint",
        "hours",
        "phones",
        "messengers",
        "website",
        "socials",
        "payment_methods",
        "services",
        "features",
        "brand",
    }
)
CHANGE_FIELDS = FACT_FIELDS | {"category", "prices"}
CHECKLIST_ITEMS = (
    "name",
    "address",
    "entrance_hint",
    "hours",
    "phones",
    "messengers",
    "website",
    "socials",
    "services",
    "prices",
    "payment_methods",
    "logo",
    "photos",
    "videos",
)
ALWAYS_REQUIRED = ("name", "address", "phones")
CREATE_REQUIRED = ("hours", "services")
MATERIAL_ITEMS = {"logo": "logo", "photo_real": "photos", "video_real": "videos", "price": "prices"}
MATERIAL_KINDS = frozenset(MATERIAL_ITEMS)
OPTIONAL_NOT_APPLICABLE = frozenset(CHECKLIST_ITEMS) - frozenset(ALWAYS_REQUIRED)
CLOSED_KEYS = frozenset({"bek", "webhook", "payment", "invoice", "syntony", "whatsapp", "amount_kzt"})
SPEC_KEYS = frozenset(
    {
        "key",
        "company_id",
        "company",
        "branches",
        "platforms",
        "facts",
        "materials",
        "messages",
        "changes",
        "missing_fields",
        "ambiguities",
        "not_applicable",
        "credentials",
    }
)
COMPANY_KEYS = frozenset({"id_2gis", "name", "city"})
TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SERVICE_KEYS = frozenset({"name", "category", "price", "price_from", "price_to", "duration_min"})


class OrderIntakeError(ValueError):
    pass


class DuplicateOrderError(OrderIntakeError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"test order {key!r} already exists")


@dataclass(frozen=True)
class Fact:
    field: str
    value: Any
    source_ref: str
    approved: bool


@dataclass(frozen=True)
class PlatformWork:
    platform: str
    action: str
    external_id: str | None
    public_url: str | None
    owner_authorization: str


@dataclass(frozen=True)
class Material:
    kind: str
    path: str
    ref: str | None
    original_name: str | None
    mime: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class Change:
    platform: str
    field: str
    before: Any
    after: Any
    source_ref: str
    nap_consent_ref: str | None


@dataclass(frozen=True)
class CredentialInput:
    platform: str
    login_hint: str | None
    ciphertext: bytes


@dataclass(frozen=True)
class NewCompany:
    id_2gis: str
    name: str
    city: str


@dataclass(frozen=True)
class Spec:
    key: str
    company_id: int | None
    company: NewCompany | None
    branches: int
    amount_kzt: int
    platforms: tuple[PlatformWork, ...]
    facts: tuple[Fact, ...]
    materials: tuple[Material, ...]
    messages: tuple[tuple[str, str], ...]
    changes: tuple[Change, ...]
    missing_fields: tuple[str, ...]
    ambiguities: tuple[str, ...]
    not_applicable: tuple[str, ...]
    credentials: tuple[CredentialInput, ...]


@dataclass(frozen=True)
class Readiness:
    status: str
    missing_company_information: tuple[str, ...]
    missing_materials: tuple[str, ...]
    missing_owner_permissions: tuple[str, ...]
    unresolved_ambiguities: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "missing_company_information": list(self.missing_company_information),
            "missing_materials": list(self.missing_materials),
            "missing_owner_permissions": list(self.missing_owner_permissions),
            "unresolved_ambiguities": list(self.unresolved_ambiguities),
        }


@dataclass(frozen=True)
class OrderView:
    order_id: int
    stage: str
    company_id: int
    company_name: str
    amount_kzt: int
    platforms: tuple[tuple[str, str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "stage": self.stage,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "amount_kzt": self.amount_kzt,
            "platforms": [
                {"platform": platform, "action": action, "owner_authorization": auth}
                for platform, action, auth in self.platforms
            ],
        }


def create_test_order(session: Session, spec: Mapping[str, Any]) -> Order:
    """Insert one local setup order. The caller owns the transaction."""
    payload = parse_spec(spec)
    if not no_long_dash(SUMMARY):
        raise OrderIntakeError("client summary must not use a long dash")
    try:
        with session.begin_nested():
            return _persist(session, payload)
    except IntegrityError as exc:
        raise _translate_integrity(exc, payload.key) from exc


def parse_spec(spec: Mapping[str, Any]) -> Spec:
    if not isinstance(spec, Mapping):
        raise OrderIntakeError("order spec must be an object")
    unknown = sorted(set(spec) - SPEC_KEYS)
    closed = sorted(set(spec) & CLOSED_KEYS)
    if closed:
        raise OrderIntakeError("Bek, payment, and WhatsApp are out of scope for a local test order")
    if unknown:
        raise OrderIntakeError(f"unknown order fields: {', '.join(unknown)}")

    key = _token(spec.get("key"), "key")
    branches = _whole(spec.get("branches", 1), "branches")
    if branches not in QUOTES_KZT:
        raise OrderIntakeError("branch count must be 1, 2, or 3")
    company_id = spec.get("company_id")
    company_body = spec.get("company")
    if company_id is not None and company_body is not None:
        raise OrderIntakeError("select an existing company or provide a new one")
    if company_id is None and company_body is None:
        raise OrderIntakeError("company identity is required")
    parsed_id = _whole(company_id, "company_id") if company_id is not None else None
    if parsed_id is not None and parsed_id < 1:
        raise OrderIntakeError("company_id must refer to an existing company")
    company = _new_company(company_body) if company_body is not None else None

    messages = _messages(spec.get("messages", []))
    message_ids = {item[0] for item in messages}
    materials = _materials(spec.get("materials", []))
    material_refs = {item.ref for item in materials if item.ref}
    known_refs = message_ids | material_refs

    facts = _facts(spec.get("facts", []), known_refs, company.name if company else None)
    platforms = _platforms(spec.get("platforms"))
    platform_names = {item.platform for item in platforms}
    changes = _changes(spec.get("changes", []), platform_names, known_refs, facts)
    missing_fields = _name_list(spec.get("missing_fields", []), CHANGE_FIELDS, "missing_fields")
    overlap = sorted(set(missing_fields) & {fact.field for fact in facts})
    if overlap:
        raise OrderIntakeError(f"missing fields were also supplied: {', '.join(overlap)}")
    ambiguities = _texts(spec.get("ambiguities", []), "ambiguities")
    not_applicable = _name_list(spec.get("not_applicable", []), OPTIONAL_NOT_APPLICABLE, "not_applicable")
    credentials = _credentials(spec.get("credentials", []), platform_names)
    return Spec(
        key=key,
        company_id=parsed_id,
        company=company,
        branches=branches,
        amount_kzt=QUOTES_KZT[branches],
        platforms=platforms,
        facts=facts,
        materials=materials,
        messages=messages,
        changes=changes,
        missing_fields=missing_fields,
        ambiguities=ambiguities,
        not_applicable=not_applicable,
        credentials=credentials,
    )


def order_status(session: Session, order_id: int) -> OrderView | None:
    order = session.get(Order, order_id)
    if order is None or order.company_id is None:
        return None
    company = session.get(Company, order.company_id)
    intake = session.get(Intake, order_id)
    if company is None or intake is None:
        return None
    platforms = _platform_rows(intake.checklist)
    return OrderView(
        order_id=order.id,
        stage=order.stage,
        company_id=company.id,
        company_name=company.name,
        amount_kzt=order.amount_kzt,
        platforms=tuple(
            (name, row["action"], row["owner_authorization"]) for name, row in sorted(platforms.items())
        ),
    )


def check_readiness(session: Session, order_id: int) -> Readiness | None:
    order = session.get(Order, order_id)
    intake = session.get(Intake, order_id) if order is not None else None
    if order is None or intake is None:
        return None
    checklist = intake.checklist if isinstance(intake.checklist, dict) else {}
    items = checklist.get("items") if isinstance(checklist.get("items"), dict) else {}
    platforms = _platform_rows(checklist)
    actions = {row["action"] for row in platforms.values()}
    required = list(ALWAYS_REQUIRED)
    if "create" in actions:
        required.extend(CREATE_REQUIRED)

    company_missing: list[str] = []
    material_missing: list[str] = []
    permissions: list[str] = []
    ambiguities: list[str] = [text for text in checklist.get("ambiguities") or [] if isinstance(text, str)]

    for name in required:
        status = _item_status(items, name)
        if status == "missing":
            company_missing.append(name)
        elif status == "not_applicable":
            company_missing.append(name)
    material_names = frozenset({"logo", "photos", "videos", "prices"})
    for name, row in items.items():
        if name in material_names:
            continue
        if isinstance(row, dict) and row.get("status") == "received":
            ambiguities.append(str(name))
    for name in checklist.get("missing_fields") or []:
        if isinstance(name, str) and name not in company_missing:
            company_missing.append(name)
    for name in ("logo", "photos", "videos"):
        if _item_status(items, name) == "missing":
            material_missing.append(name)

    for platform, row in platforms.items():
        stored = session.get(CompanyPlatformId, (order.company_id, platform))
        claimed = row["owner_authorization"]
        actual = stored.access_status if stored is not None else "none"
        if claimed != "verified" or actual != "verified":
            permissions.append(platform)
        if claimed != actual:
            ambiguities.append(f"owner authorization mismatch: {platform}")

    package_ids = select(ChangePackage.id).where(ChangePackage.order_id == order_id)
    change_rows = session.scalars(select(ChangeItem).where(ChangeItem.package_id.in_(package_ids))).all()
    for item in change_rows:
        if item.is_nap and not item.nap_consent_ref:
            ambiguities.append(f"nap consent missing: {item.platform}.{item.field}")

    company_missing = sorted(set(company_missing))
    material_missing = sorted(set(material_missing))
    permissions = sorted(set(permissions))
    ambiguities = sorted(set(ambiguities))
    ready = not company_missing and not material_missing and not permissions and not ambiguities
    return Readiness(
        status="ready" if ready else "not_ready",
        missing_company_information=tuple(company_missing),
        missing_materials=tuple(material_missing),
        missing_owner_permissions=tuple(permissions),
        unresolved_ambiguities=tuple(ambiguities),
    )


def require_execution_allowed(session: Session, order_id: int) -> None:
    """Refuse listing work until readiness and owner authorization both pass.

    This does not edit a listing, enqueue a browser job, or move the order stage.
    """
    report = check_readiness(session, order_id)
    view = order_status(session, order_id)
    if report is None or view is None:
        raise OrderIntakeError("order not found")
    for platform, _action, auth in view.platforms:
        stored = session.get(CompanyPlatformId, (view.company_id, platform))
        actual = stored.access_status if stored is not None else "none"
        if auth != "verified":
            require_verified_owner(platform, auth)
        if actual != "verified":
            require_verified_owner(platform, actual)
    if report.status != "ready":
        raise PermissionError("order is not ready for listing changes")
    for platform, _action, auth in view.platforms:
        require_verified_owner(platform, auth)


def format_status(view: OrderView) -> str:
    lines = [
        f"order {view.order_id}",
        f"stage {view.stage}",
        f"company {view.company_id} {view.company_name}",
        f"amount {kzt(view.amount_kzt)}",
    ]
    lines.extend(f"platform {platform} {action} {auth}" for platform, action, auth in view.platforms)
    return "\n".join(lines)


def format_readiness(report: Readiness) -> str:
    def cell(items: tuple[str, ...]) -> str:
        return ", ".join(items) if items else "-"

    return "\n".join(
        [
            f"status {report.status}",
            f"missing company information: {cell(report.missing_company_information)}",
            f"missing materials: {cell(report.missing_materials)}",
            f"missing owner permissions: {cell(report.missing_owner_permissions)}",
            f"unresolved ambiguities: {cell(report.unresolved_ambiguities)}",
        ]
    )


def _persist(session: Session, spec: Spec) -> Order:
    company = _company(session, spec)
    session.flush()
    _reject_company_named_service(company.name, spec)
    _check_platforms(session, company, spec.platforms)
    deal = Deal(syntony_deal_id=f"{LOCAL_DEAL_PREFIX}{spec.key}", company_id=company.id, created_at=now())
    session.add(deal)
    session.flush()
    for message_id, _text in spec.messages:
        if session.get(ClientMessage, message_id) is not None:
            raise OrderIntakeError(f"message {message_id} already stored")

    order = Order(
        deal_id=deal.id,
        company_id=company.id,
        kind="setup",
        branches=spec.branches,
        amount_kzt=spec.amount_kzt,
        created_at=now(),
    )
    transition(order, "created", session)
    _store_messages(session, order.id, company.id, spec.messages)
    file_ids = _store_materials(session, order.id, company.id, spec)
    _store_profile(session, company, spec, file_ids)
    _store_platforms(session, company.id, spec.platforms)
    session.add(Intake(order_id=order.id, checklist=_checklist(spec, company.name, file_ids), updated_at=now()))
    _store_package(session, order.id, spec)
    _store_credentials(session, company.id, spec.credentials)
    session.flush()
    return order


def _company(session: Session, spec: Spec) -> Company:
    if spec.company_id is not None:
        company = session.get(Company, spec.company_id)
        if company is None:
            raise OrderIntakeError("company not found")
        if not company.name.strip():
            raise OrderIntakeError("company name is required")
        return company
    assert spec.company is not None
    existing = session.scalar(select(Company).where(Company.id_2gis == spec.company.id_2gis))
    if existing is not None:
        raise OrderIntakeError("company already exists; select it by company_id")
    company = Company(
        id_2gis=spec.company.id_2gis,
        name=spec.company.name,
        city=spec.company.city,
        source="local_order",
        created_at=now(),
    )
    session.add(company)
    return company


def _check_platforms(session: Session, company: Company, platforms: tuple[PlatformWork, ...]) -> None:
    for work in platforms:
        row = session.get(CompanyPlatformId, (company.id, work.platform))
        stored_id = None if row is None else row.external_id
        if work.action == "create" and stored_id:
            raise OrderIntakeError(f"{work.platform} already has a listing id; use update")
        if work.action == "update" and stored_id and work.external_id and stored_id != work.external_id:
            raise OrderIntakeError(f"{work.platform} listing id does not match the stored id")


def _reject_company_named_service(company_name: str, spec: Spec) -> None:
    for fact in spec.facts:
        if fact.field != "services":
            continue
        for item in fact.value:
            if _same_name(str(item.get("name", "")), company_name):
                raise OrderIntakeError("the company name is not a service")


def _store_messages(session: Session, order_id: int, company_id: int, messages: tuple[tuple[str, str], ...]) -> None:
    for message_id, text in messages:
        session.add(
            ClientMessage(id=message_id, company_id=company_id, order_id=order_id, text=text, received_at=now())
        )


def _store_materials(session: Session, order_id: int, company_id: int, spec: Spec) -> dict[str, int]:
    ids: dict[str, int] = {}
    for material in spec.materials:
        row = ClientFile(
            company_id=company_id,
            order_id=order_id,
            kind=material.kind,
            path=material.path,
            original_name=material.original_name,
            mime=material.mime,
            size_bytes=material.size_bytes,
            received_at=now(),
        )
        session.add(row)
        session.flush()
        if material.ref:
            ids[material.ref] = row.id
        ids[f"#{row.id}"] = row.id
    return ids


def _store_profile(session: Session, company: Company, spec: Spec, file_ids: dict[str, int]) -> None:
    latest = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == company.id).order_by(CompanyProfile.version.desc())
    )
    data = copy.deepcopy(latest.data) if latest is not None and isinstance(latest.data, dict) else {}
    version = 1 if latest is None else int(latest.version) + 1
    for fact in spec.facts:
        source_ref = _stored_ref(fact.source_ref, file_ids)
        client_supplied_fact(True, fact.value)
        if fact.field == "services":
            data["services"] = fact.value
            for item in data["services"]:
                item["source_ref"] = source_ref
                item["needs_confirmation"] = not fact.approved
        elif fact.field == "name":
            if not _same_name(str(fact.value), company.name):
                continue
            data["name"] = {"value": company.name, "source_ref": source_ref, "needs_confirmation": not fact.approved}
        else:
            data[fact.field] = {
                "value": fact.value,
                "source_ref": source_ref,
                "needs_confirmation": not fact.approved,
            }
    if spec.facts or latest is None:
        session.add(CompanyProfile(company_id=company.id, version=version, data=data, created_at=now()))


def _store_platforms(session: Session, company_id: int, platforms: tuple[PlatformWork, ...]) -> None:
    for work in platforms:
        row = session.get(CompanyPlatformId, (company_id, work.platform))
        external_id = None if work.action == "create" else work.external_id
        public_url = None if work.action == "create" else work.public_url
        if row is None:
            session.add(
                CompanyPlatformId(
                    company_id=company_id,
                    platform=work.platform,
                    external_id=external_id,
                    public_url=public_url,
                    access_status=work.owner_authorization,
                )
            )
            continue
        if work.action == "update":
            if external_id:
                row.external_id = external_id
            if public_url:
                row.public_url = public_url
        row.access_status = work.owner_authorization


def _store_package(session: Session, order_id: int, spec: Spec) -> None:
    package = ChangePackage(order_id=order_id, version=1, status="draft", summary_for_client=SUMMARY, created_at=now())
    session.add(package)
    session.flush()
    for number, change in enumerate(spec.changes, start=1):
        after = change.after
        session.add(
            ChangeItem(
                package_id=package.id,
                n=number,
                platform=change.platform,
                field=change.field,
                before=change.before,
                after=after,
                source_ref={"ref": change.source_ref},
                is_nap=is_nap_field(change.field),
                nap_consent_ref=change.nap_consent_ref,
                counts_to_limit=False,
                status="draft",
                idempotency_key=_change_key(order_id, change.platform, change.field, after),
            )
        )


def _store_credentials(session: Session, company_id: int, credentials: tuple[CredentialInput, ...]) -> None:
    for item in credentials:
        session.add(
            Credential(
                company_id=company_id,
                platform=item.platform,
                login_hint=item.login_hint,
                ciphertext=item.ciphertext,
                created_at=now(),
            )
        )


def _checklist(spec: Spec, company_name: str, file_ids: dict[str, int]) -> dict[str, Any]:
    items = {name: {"status": "missing", "source_ref": None} for name in CHECKLIST_ITEMS}
    items["name"] = {"status": "verified", "source_ref": "field:company.name"}
    for fact in spec.facts:
        if fact.field not in items:
            continue
        if fact.field == "name" and not _same_name(str(fact.value), company_name):
            items["name"] = {"status": "received", "source_ref": _stored_ref(fact.source_ref, file_ids)}
            continue
        items[fact.field] = {
            "status": "verified" if fact.approved else "received",
            "source_ref": _stored_ref(fact.source_ref, file_ids),
        }
    for material in spec.materials:
        item = MATERIAL_ITEMS[material.kind]
        ref = f"file:{file_ids[material.ref]}" if material.ref else None
        items[item] = {"status": "received", "source_ref": ref}
    for name in spec.not_applicable:
        if items[name]["status"] == "missing":
            items[name] = {"status": "not_applicable", "source_ref": None}
    return {
        "local_key": spec.key,
        "payment": "out_of_scope",
        "authorization_source": "local_attestation",
        "platforms": {
            work.platform: {
                "action": work.action,
                "owner_authorization": work.owner_authorization,
                "external_id": work.external_id if work.action == "update" else None,
                "public_url": work.public_url if work.action == "update" else None,
            }
            for work in spec.platforms
        },
        "items": items,
        "missing_fields": list(spec.missing_fields),
        "ambiguities": list(spec.ambiguities),
    }


def _new_company(body: Any) -> NewCompany:
    if not isinstance(body, Mapping):
        raise OrderIntakeError("company must be an object")
    extra = sorted(set(body) - COMPANY_KEYS)
    if extra:
        raise OrderIntakeError("address, contacts, and services belong in client facts, not the company record")
    return NewCompany(
        id_2gis=_required_text(body.get("id_2gis"), "id_2gis"),
        name=_required_text(body.get("name"), "company name"),
        city=_required_text(body.get("city"), "city"),
    )


def _platforms(value: Any) -> tuple[PlatformWork, ...]:
    if not isinstance(value, list) or not value:
        raise OrderIntakeError("requested work must name at least one platform")
    rows: list[PlatformWork] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise OrderIntakeError("platform work must be an object")
        platform = PLATFORM_NAMES.get(_text(item.get("platform")).casefold())
        if platform is None:
            raise OrderIntakeError("platform must be 2GIS, Google Maps, or Yandex Maps")
        if platform in seen:
            raise OrderIntakeError(f"duplicate platform {platform}")
        seen.add(platform)
        action = _text(item.get("action")).casefold()
        if action not in ACTIONS:
            raise OrderIntakeError("each platform needs action create or update")
        external_id = _optional_text(item.get("external_id"), "external_id")
        public_url = _optional_text(item.get("public_url"), "public_url")
        if action == "create" and (external_id or public_url):
            raise OrderIntakeError("create does not take an existing listing id or url")
        if action == "update" and not external_id and not public_url:
            raise OrderIntakeError("update needs a listing id or public url supplied by the caller")
        if public_url and not public_url.startswith(("https://", "http://")):
            raise OrderIntakeError("public url must be an http or https address")
        authorization = _text(item.get("owner_authorization", "none")).casefold()
        if authorization not in AUTHORIZATION:
            raise OrderIntakeError("owner authorization must be none, invited, verified, or lost")
        rows.append(
            PlatformWork(
                platform=platform,
                action=action,
                external_id=external_id,
                public_url=public_url,
                owner_authorization=authorization,
            )
        )
    return tuple(rows)


def _facts(value: Any, known_refs: set[str], company_name: str | None) -> tuple[Fact, ...]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise OrderIntakeError("facts must be a list")
    rows: list[Fact] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise OrderIntakeError("a fact must be an object")
        field = _text(item.get("field"))
        if field not in FACT_FIELDS:
            raise OrderIntakeError(f"unknown business fact {field or '(blank)'}")
        if field in seen:
            raise OrderIntakeError(f"duplicate fact {field}")
        seen.add(field)
        if "value" not in item or item.get("value") is None:
            raise OrderIntakeError(f"{field} was not supplied by the client")
        client_supplied_fact(True, item.get("value"))
        source_ref = _source_ref(item.get("source_ref"), known_refs)
        approved = item.get("approved", False)
        if not isinstance(approved, bool):
            raise OrderIntakeError("approved must be true or false")
        parsed = _fact_value(field, item.get("value"), company_name)
        rows.append(Fact(field=field, value=parsed, source_ref=source_ref, approved=approved))
    return tuple(rows)


def _fact_value(field: str, value: Any, company_name: str | None) -> Any:
    if field == "phones":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
            raise OrderIntakeError("phones must be a list of numbers supplied by the client")
        return [item.strip() for item in value]
    if field == "services":
        return _services(value, company_name)
    if field in {"messengers", "socials", "payment_methods", "features"}:
        if not isinstance(value, list | dict) or not value:
            raise OrderIntakeError(f"{field} must be supplied by the client")
        return value
    if field == "brand":
        if not isinstance(value, dict) or not value:
            raise OrderIntakeError("brand must be supplied by the client")
        return value
    if not isinstance(value, str) or not value.strip():
        raise OrderIntakeError(f"{field} must be text supplied by the client")
    return " ".join(value.split())


def _services(value: Any, company_name: str | None) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise OrderIntakeError("services must be a list supplied by the client")
    rows: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if isinstance(item, str):
            body: Mapping[str, Any] = {"name": item}
        elif isinstance(item, Mapping):
            extra = sorted(set(item) - SERVICE_KEYS)
            if extra:
                raise OrderIntakeError(f"unknown service fields: {', '.join(extra)}")
            body = item
        else:
            raise OrderIntakeError("a service must be a name or an object")
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise OrderIntakeError("a service name must be supplied by the client")
        cleaned = " ".join(name.split())
        if company_name and _same_name(cleaned, company_name):
            raise OrderIntakeError("the company name is not a service")
        if cleaned.casefold() in names:
            raise OrderIntakeError(f"duplicate service {cleaned}")
        names.add(cleaned.casefold())
        row: dict[str, Any] = {"name": cleaned}
        for key in ("category",):
            if key in body and body[key] is not None:
                if not isinstance(body[key], str) or not body[key].strip():
                    raise OrderIntakeError(f"service {key} must be text")
                row[key] = body[key].strip()
        for key in ("price", "price_from", "price_to", "duration_min"):
            if key in body and body[key] is not None:
                row[key] = _whole(body[key], f"service {key}")
                if row[key] < 0:
                    raise OrderIntakeError(f"service {key} must be zero or more")
        rows.append(row)
    return rows


def _materials(value: Any) -> tuple[Material, ...]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise OrderIntakeError("materials must be a list")
    rows: list[Material] = []
    refs: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise OrderIntakeError("a material must be an object")
        kind = _text(item.get("kind"))
        if kind not in MATERIAL_KINDS:
            raise OrderIntakeError("material kind must be photo_real, video_real, logo, or price")
        path = _required_text(item.get("path"), "material path")
        if "://" in path:
            raise OrderIntakeError("material path must be a local file, not a url")
        ref = _optional_text(item.get("ref"), "material ref")
        if ref is not None:
            if not TOKEN.fullmatch(ref):
                raise OrderIntakeError("material ref must be a short token")
            if ref in refs:
                raise OrderIntakeError(f"duplicate material ref {ref}")
            refs.add(ref)
        size = item.get("size_bytes")
        rows.append(
            Material(
                kind=kind,
                path=path,
                ref=ref,
                original_name=_optional_text(item.get("original_name"), "original_name"),
                mime=_optional_text(item.get("mime"), "mime"),
                size_bytes=None if size is None else _whole(size, "size_bytes"),
            )
        )
    return tuple(rows)


def _messages(value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise OrderIntakeError("messages must be a list")
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise OrderIntakeError("a message must be an object")
        message_id = _token(item.get("id"), "message id")
        if message_id in seen:
            raise OrderIntakeError(f"duplicate message {message_id}")
        seen.add(message_id)
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise OrderIntakeError("message text is required")
        rows.append((message_id, text.strip()))
    return tuple(rows)


def _changes(
    value: Any,
    platforms: set[str],
    known_refs: set[str],
    facts: tuple[Fact, ...],
) -> tuple[Change, ...]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise OrderIntakeError("changes must be a list")
    by_field = {fact.field: fact for fact in facts}
    rows: list[Change] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise OrderIntakeError("a change must be an object")
        platform = PLATFORM_NAMES.get(_text(item.get("platform")).casefold())
        if platform is None or platform not in platforms:
            raise OrderIntakeError("a change must name one of the order platforms")
        field = _text(item.get("field"))
        if field not in CHANGE_FIELDS:
            raise OrderIntakeError(f"unknown change field {field or '(blank)'}")
        if (platform, field) in seen:
            raise OrderIntakeError(f"duplicate change {platform}.{field}")
        seen.add((platform, field))
        if "after" not in item or item.get("after") is None:
            raise OrderIntakeError(f"{field} change has no client value")
        client_supplied_fact(True, item.get("after"))
        after = item.get("after")
        fact = by_field.get(field)
        if fact is not None and fact.value != after:
            raise OrderIntakeError(f"{field} change does not match the client fact")
        before = item.get("before") if "before" in item else None
        consent = _optional_text(item.get("nap_consent_ref"), "nap_consent_ref")
        rows.append(
            Change(
                platform=platform,
                field=field,
                before=before,
                after=after,
                source_ref=_source_ref(item.get("source_ref"), known_refs),
                nap_consent_ref=consent,
            )
        )
    return tuple(rows)


def _credentials(value: Any, platforms: set[str]) -> tuple[CredentialInput, ...]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise OrderIntakeError("credentials must be a list")
    rows: list[CredentialInput] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise OrderIntakeError("a credential must be an object")
        if {"password", "secret", "token"} & set(item):
            raise OrderIntakeError("passwords are not accepted; pass ciphertext_b64")
        platform = PLATFORM_NAMES.get(_text(item.get("platform")).casefold())
        if platform is None or platform not in platforms:
            raise OrderIntakeError("a credential must name one of the order platforms")
        encoded = item.get("ciphertext_b64")
        if not isinstance(encoded, str) or not encoded.strip():
            raise OrderIntakeError("ciphertext_b64 is required")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise OrderIntakeError("ciphertext_b64 is not valid base64") from exc
        if not raw:
            raise OrderIntakeError("ciphertext is empty")
        rows.append(
            CredentialInput(
                platform=platform,
                login_hint=_optional_text(item.get("login_hint"), "login_hint"),
                ciphertext=raw,
            )
        )
    return tuple(rows)


def _same_name(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def _platform_rows(checklist: Any) -> dict[str, dict[str, str]]:
    if not isinstance(checklist, Mapping):
        return {}
    raw = checklist.get("platforms")
    if not isinstance(raw, Mapping):
        return {}
    rows: dict[str, dict[str, str]] = {}
    for name, item in raw.items():
        if not isinstance(item, Mapping):
            continue
        rows[str(name)] = {
            "action": str(item.get("action") or ""),
            "owner_authorization": str(item.get("owner_authorization") or "none"),
        }
    return rows


def _item_status(items: Mapping[str, Any], name: str) -> str:
    row = items.get(name)
    if not isinstance(row, dict):
        return "missing"
    status = row.get("status")
    return status if isinstance(status, str) else "missing"


def _stored_ref(source_ref: str, file_ids: Mapping[str, int]) -> str:
    kind, _, ref = source_ref.partition(":")
    if kind == "file":
        return f"file:{file_ids[ref]}"
    return source_ref


def _source_ref(value: Any, known_refs: set[str]) -> str:
    if not isinstance(value, str):
        raise OrderIntakeError("source_ref is required")
    kind, sep, ref = value.partition(":")
    if sep != ":" or kind not in {"msg", "file"} or ref not in known_refs:
        raise OrderIntakeError("source_ref must point at a message or material in this order")
    if kind == "msg":
        return f"msg:{ref}"
    return f"file:{ref}"


def _change_key(order_id: int, platform: str, field: str, after: Any) -> str:
    raw = json.dumps(
        {"after": after, "field": field, "order_id": order_id, "platform": platform},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _translate_integrity(exc: IntegrityError, key: str) -> OrderIntakeError:
    text = str(getattr(exc, "orig", exc)).lower()
    if "syntony_deal_id" in text:
        return DuplicateOrderError(key)
    if "id_2gis" in text:
        return OrderIntakeError("company already exists; select it by company_id")
    if "uq_company_platform_ids_platform_external" in text:
        return OrderIntakeError("platform id already belongs to another company")
    return OrderIntakeError("order was not saved")


def _name_list(value: Any, allowed: frozenset[str], label: str) -> tuple[str, ...]:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OrderIntakeError(f"{label} must be a list of names")
    rows: list[str] = []
    for item in value:
        if item not in allowed:
            raise OrderIntakeError(f"unknown {label} entry {item or '(blank)'}")
        if item not in rows:
            rows.append(item)
    return tuple(rows)


def _texts(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise OrderIntakeError(f"{label} must be a list of text")
    return tuple(item.strip() for item in value)


def _token(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if not TOKEN.fullmatch(text):
        raise OrderIntakeError(f"{label} must be a short token")
    return text


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderIntakeError(f"{label} is required")
    return value.strip()


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OrderIntakeError(f"{label} must be text")
    return value.strip()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _whole(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrderIntakeError(f"{label} must be a whole number")
    return value
