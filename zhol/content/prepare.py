"""Draft listing text and abstract illustrations. Nothing here is published."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zhol.content.brief import OrderFacts, collect_facts
from zhol.content.checks import check_reply, check_text
from zhol.content.hashing import digest_json, digest_text
from zhol.content.images import (
    ILLUSTRATIVE_KINDS,
    ORIGIN,
    GeneratedImage,
    IllustrationProvider,
    IllustrationRequest,
    OfflineIllustrationProvider,
    write_illustration,
)
from zhol.content.limits import char_limit
from zhol.db.models import ClientMessage, Company, CompanyProfile, ContentItem, HumanTask, Intake, Order, Review
from zhol.guards import refuse_fabricated_scene
from zhol.llm.gateway import Gateway, LlmError
from zhol.timeutil import now

TEXT_PROMPT = "creator-text"
REVIEW_PROMPT = "executor-review-reply"


class ContentError(ValueError):
    pass


@dataclass(frozen=True)
class ContentReport:
    order_id: int
    rows: tuple[dict[str, Any], ...]
    skipped_reviews: int


def prepare_content(
    session: Session,
    order_id: int,
    *,
    gateway: Gateway,
    output_dir: str | Path,
    images: IllustrationProvider | None = None,
) -> ContentReport:
    """Build drafts from approved facts. A second call with the same facts does not generate again."""
    order, company, profile, checklist, messages = _context(session, order_id)
    facts = collect_facts(
        company_name=company.name,
        city=company.city,
        profile=profile,
        messages=messages,
        checklist=checklist,
    )
    provider = images if images is not None else OfflineIllustrationProvider()
    text_prompt = gateway.prompt(TEXT_PROMPT)
    review_prompt = gateway.prompt(REVIEW_PROMPT)
    stored: list[ContentItem] = []
    for kind, platform, subject, purpose in _text_tasks(facts):
        stored.append(
            _text_item(
                session,
                order_id=order.id,
                facts=facts,
                gateway=gateway,
                prompt_name=TEXT_PROMPT,
                prompt_version=text_prompt.version,
                kind=kind,
                platform=platform,
                subject=subject,
                purpose=purpose,
                output_dir=Path(output_dir),
            )
        )
    reviews = _reviews(session, order.id)
    skipped = 0
    if facts.reviews_authorized:
        for review in reviews:
            stored.append(
                _reply_item(
                    session,
                    order_id=order.id,
                    facts=facts,
                    review=review,
                    gateway=gateway,
                    prompt_version=review_prompt.version,
                    output_dir=Path(output_dir),
                )
            )
    else:
        skipped = len(reviews)
    stored.append(
        _illustration(
            session,
            order_id=order.id,
            facts=facts,
            provider=provider,
            output_dir=Path(output_dir),
            kind="cover",
        )
    )
    session.flush()
    return ContentReport(order.id, tuple(_public(row) for row in stored), skipped)


def content_status(session: Session, order_id: int) -> ContentReport | None:
    order = session.get(Order, order_id)
    if order is None:
        return None
    rows = session.scalars(
        select(ContentItem).where(ContentItem.order_id == order_id).order_by(ContentItem.id)
    ).all()
    return ContentReport(order_id, tuple(_public(row) for row in rows), 0)


def list_content(
    session: Session,
    *,
    order_id: int | None = None,
    status: str | None = None,
    limit: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Page stored drafts. This does not generate or approve content."""
    stmt = select(ContentItem)
    if order_id is not None:
        stmt = stmt.where(ContentItem.order_id == order_id)
    if status:
        stmt = stmt.where(ContentItem.status == status)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.scalars(stmt.order_by(ContentItem.id.desc()).limit(limit).offset(offset)).all()
    items: list[dict[str, Any]] = []
    for row in rows:
        public = _public(row)
        public["order_id"] = row.order_id
        public["approval_status"] = "approved" if public["approved"] else row.status
        public["publication"] = "not_published"
        items.append(public)
    return total, items


def approve_content(session: Session, content_id: int) -> ContentItem:
    """A person accepts a finished draft. Checked content is still not published."""
    row = session.get(ContentItem, content_id)
    if row is None or not isinstance(row.brief, dict):
        raise ContentError("content item not found")
    brief = dict(row.brief)
    if row.status == "checked" and _approval_current(brief):
        return row
    if row.status != "done" or brief.get("valid") is not True:
        raise PermissionError("only a valid draft can be approved")
    if row.kind in ILLUSTRATIVE_KINDS:
        refuse_fabricated_scene(row.kind)
        if brief.get("illustrative") is not True or brief.get("origin") != ORIGIN:
            raise PermissionError("an illustration must be labeled before approval")
    if row.kind == "review_reply" and brief.get("needs_client_approval") is True and not _reply_consent(session, row, brief):
        raise PermissionError("a reply to a rating of 3 or lower needs client consent")
    brief["approval"] = {
        "facts_hash": brief.get("input_hash"),
        "content_hash": brief.get("content_hash"),
        "at": now().isoformat(),
    }
    row.brief = brief
    row.status = "checked"
    session.flush()
    return row


def request_illustration(
    session: Session,
    order_id: int,
    kind: str,
    *,
    output_dir: str | Path,
    provider: IllustrationProvider | None = None,
) -> ContentItem:
    """Generate one abstract image, or refuse a scene that would invent a real place or person."""
    order, company, profile, checklist, messages = _context(session, order_id)
    facts = collect_facts(
        company_name=company.name,
        city=company.city,
        profile=profile,
        messages=messages,
        checklist=checklist,
    )
    try:
        refuse_fabricated_scene(kind)
        if kind not in ILLUSTRATIVE_KINDS:
            raise PermissionError("only an abstract cover or promo illustration can be generated")
    except PermissionError as exc:
        _refuse_image(session, order.id, kind, str(exc))
        session.flush()
        raise
    row = _illustration(
        session,
        order_id=order.id,
        facts=facts,
        provider=provider if provider is not None else OfflineIllustrationProvider(),
        output_dir=Path(output_dir),
        kind=kind,
    )
    session.flush()
    return row


def format_report(report: ContentReport) -> str:
    lines = [f"order {report.order_id}"]
    for row in report.rows:
        platform = row.get("platform") or "-"
        lines.append(f"{row['kind']} {platform} {row['status']} v{row['version']}")
    if report.skipped_reviews:
        lines.append(f"reviews skipped {report.skipped_reviews}")
    return "\n".join(lines)


def _text_tasks(facts: OrderFacts) -> tuple[tuple[str, str | None, str, str], ...]:
    tasks: list[tuple[str, str | None, str, str]] = []
    if not facts.client_description:
        tasks.append(("company_description", None, "company", "company description"))
        for platform in facts.platforms:
            tasks.append(("platform_description", platform, "platform", "short platform description"))
    if any(item["path"] == "services" for item in facts.approved):
        tasks.append(("service_description", None, "services", "service descriptions"))
    return tuple(tasks)


def _text_item(
    session: Session,
    *,
    order_id: int,
    facts: OrderFacts,
    gateway: Gateway,
    prompt_name: str,
    prompt_version: str,
    kind: str,
    platform: str | None,
    subject: str,
    purpose: str,
    output_dir: Path,
) -> ContentItem:
    limit = char_limit(kind, platform)
    incoming = _input_hash(facts, kind=kind, platform=platform, subject=subject, limit=limit, prompt_version=prompt_version)
    existing = _find(session, order_id, kind, platform, subject)
    if existing is not None and isinstance(existing.brief, dict) and existing.brief.get("input_hash") == incoming:
        return existing
    payload = {
        "kind": kind,
        "platform": platform or "",
        "language": facts.language,
        "purpose": purpose,
        "business_data_json": facts.model_payload(),
        "missing_json": facts.gap_payload(),
        "constraints_json": {"max_chars": limit},
    }
    try:
        result = gateway.run(prompt_name, payload)
    except LlmError as exc:
        return _store_text(
            session,
            existing,
            order_id=order_id,
            kind=kind,
            platform=platform,
            subject=subject,
            facts=facts,
            incoming=incoming,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            model=None,
            text="",
            claims=[],
            language=facts.language,
            limit=limit,
            reason=str(exc),
            output_dir=output_dir,
        )
    data = result.data if isinstance(result.data, dict) else {}
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    language = data.get("language") if isinstance(data.get("language"), str) else ""
    reason = None
    if language != facts.language:
        reason = "language does not match"
    else:
        reason = check_text(
            text=text,
            language=language,
            claims=claims,
            approved=facts.approved,
            unverified=facts.unverified,
            max_chars=limit,
        )
    return _store_text(
        session,
        existing,
        order_id=order_id,
        kind=kind,
        platform=platform,
        subject=subject,
        facts=facts,
        incoming=incoming,
        prompt_name=result.prompt_name,
        prompt_version=result.prompt_version,
        model=result.model,
        text=text,
        claims=claims,
        language=language,
        limit=limit,
        reason=reason,
        output_dir=output_dir,
    )


def _reply_item(
    session: Session,
    *,
    order_id: int,
    facts: OrderFacts,
    review: Review,
    gateway: Gateway,
    prompt_version: str,
    output_dir: Path,
) -> ContentItem:
    kind = "review_reply"
    platform = review.platform
    subject = str(review.id)
    limit = char_limit(kind)
    incoming = _input_hash(
        facts,
        kind=kind,
        platform=platform,
        subject=subject,
        limit=limit,
        prompt_version=prompt_version,
        extra={"review_id": review.id, "rating": review.rating, "text": review.text or ""},
    )
    existing = _find(session, order_id, kind, platform, subject)
    if existing is not None and isinstance(existing.brief, dict) and existing.brief.get("input_hash") == incoming:
        return existing
    payload = {
        "name": facts.company_name,
        "business_data_json": facts.model_payload(),
        "rating": review.rating if review.rating is not None else "",
        "author_label": review.author_label or "",
        "posted_at": review.posted_at.isoformat() if review.posted_at is not None else "",
        "review_text": review.text or "",
    }
    try:
        result = gateway.run(REVIEW_PROMPT, payload)
    except LlmError as exc:
        return _store_text(
            session,
            existing,
            order_id=order_id,
            kind=kind,
            platform=platform,
            subject=subject,
            facts=facts,
            incoming=incoming,
            prompt_name=REVIEW_PROMPT,
            prompt_version=prompt_version,
            model=None,
            text="",
            claims=[],
            language="",
            limit=limit,
            reason=str(exc),
            output_dir=output_dir,
            review=review,
        )
    data = result.data if isinstance(result.data, dict) else {}
    text = data.get("reply") if isinstance(data.get("reply"), str) else ""
    language = data.get("language") if isinstance(data.get("language"), str) else ""
    reason = check_reply(
        text=text,
        language=language,
        rating=review.rating,
        review_text=review.text or "",
        approved=facts.approved,
        max_chars=limit,
    )
    needs_client = bool(data.get("needs_client_approval")) or (review.rating is not None and review.rating <= 3)
    return _store_text(
        session,
        existing,
        order_id=order_id,
        kind=kind,
        platform=platform,
        subject=subject,
        facts=facts,
        incoming=incoming,
        prompt_name=result.prompt_name,
        prompt_version=result.prompt_version,
        model=result.model,
        text=text,
        claims=[],
        language=language,
        limit=limit,
        reason=reason,
        output_dir=output_dir,
        review=review,
        needs_client=needs_client,
    )


def _store_text(
    session: Session,
    existing: ContentItem | None,
    *,
    order_id: int,
    kind: str,
    platform: str | None,
    subject: str,
    facts: OrderFacts,
    incoming: str,
    prompt_name: str,
    prompt_version: str,
    model: str | None,
    text: str,
    claims: Sequence[Any],
    language: str,
    limit: int,
    reason: str | None,
    output_dir: Path,
    review: Review | None = None,
    needs_client: bool = False,
) -> ContentItem:
    version = 1
    if existing is not None and isinstance(existing.brief, dict):
        version = int(existing.brief.get("version") or 1)
        if existing.brief.get("input_hash") != incoming:
            version += 1
    path = None
    if text:
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = platform or subject
        file = output_dir / f"order-{order_id}-{kind}-{suffix}.txt"
        file.write_text(text, encoding="utf-8")
        path = str(file)
    brief = _base_brief(facts, subject=subject, incoming=incoming, version=version)
    brief.update(
        {
            "origin": "generated_text",
            "illustrative": False,
            "language": language or facts.language,
            "text": text,
            "claims": [dict(item) for item in claims if isinstance(item, Mapping)],
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "model": model,
            "max_chars": limit,
            "content_hash": digest_text(text),
            "valid": reason is None and bool(text.strip()),
            "rejection": reason,
            "approval": None,
            "needs_client_approval": needs_client,
        }
    )
    if review is not None:
        brief["review_id"] = review.id
        brief["rating"] = review.rating
    row = existing or ContentItem(order_id=order_id, kind=kind, platform=platform)
    row.kind = kind
    row.platform = platform
    row.brief = brief
    row.result_path = path
    row.status = "done" if brief["valid"] else "rejected"
    if existing is None:
        session.add(row)
    session.flush()
    return row


def _illustration(
    session: Session,
    *,
    order_id: int,
    facts: OrderFacts,
    provider: IllustrationProvider,
    output_dir: Path,
    kind: str,
) -> ContentItem:
    refuse_fabricated_scene(kind)
    subject = kind
    incoming = digest_json(
        {
            "approved": [dict(item) for item in facts.approved],
            "missing": list(facts.missing),
            "unverified": [dict(item) for item in facts.unverified],
            "kind": kind,
            "origin": ORIGIN,
            "provider": "offline",
        }
    )
    existing = _find(session, order_id, kind, None, subject)
    if existing is not None and isinstance(existing.brief, dict) and existing.brief.get("input_hash") == incoming:
        return existing
    image: GeneratedImage = provider.generate(
        IllustrationRequest(
            kind=kind,
            order_id=order_id,
            prompt=f"Abstract color field for {facts.company_name}. No people, rooms, products, or finished work.",
        )
    )
    if image.origin != ORIGIN or image.illustrative is not True:
        raise PermissionError("an illustration must be labeled and kept separate from a photograph")
    path = write_illustration(output_dir, order_id, kind, image)
    version = 1
    if existing is not None and isinstance(existing.brief, dict) and existing.brief.get("input_hash") != incoming:
        version = int(existing.brief.get("version") or 1) + 1
    brief = _base_brief(facts, subject=subject, incoming=incoming, version=version)
    brief.update(
        {
            "origin": ORIGIN,
            "illustrative": True,
            "not_a_photograph": True,
            "provider": "offline",
            "media_type": image.media_type,
            "label": image.label,
            "content_hash": digest_text(image.label + str(len(image.data))),
            "valid": True,
            "rejection": None,
            "approval": None,
            "text": None,
        }
    )
    row = existing or ContentItem(order_id=order_id, kind=kind, platform=None)
    row.kind = kind
    row.platform = None
    row.brief = brief
    row.result_path = path
    row.status = "done"
    if existing is None:
        session.add(row)
    return row


def _refuse_image(session: Session, order_id: int, kind: str, reason: str) -> ContentItem:
    existing = _find(session, order_id, kind, None, kind)
    if existing is not None and existing.status == "human":
        _open_task(session, order_id, kind)
        return existing
    row = existing or ContentItem(order_id=order_id, kind=kind, platform=None)
    row.kind = kind
    row.platform = None
    row.status = "human"
    row.result_path = None
    row.brief = {
        "subject": kind,
        "origin": ORIGIN,
        "illustrative": False,
        "not_a_photograph": True,
        "valid": False,
        "version": 1,
        "approval": None,
        "rejection": reason,
    }
    if existing is None:
        session.add(row)
    _open_task(session, order_id, kind)
    return row


def _open_task(session: Session, order_id: int, kind: str) -> None:
    rows = session.scalars(
        select(HumanTask).where(
            HumanTask.object_type == "order",
            HumanTask.object_id == order_id,
            HumanTask.reason == "content_refused",
            HumanTask.status == "open",
        )
    ).all()
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        if details.get("kind") == kind:
            return
    session.add(
        HumanTask(
            reason="content_refused",
            object_type="order",
            object_id=order_id,
            details={"kind": kind, "submitted": False},
            status="open",
            created_at=now(),
        )
    )


def _base_brief(facts: OrderFacts, *, subject: str, incoming: str, version: int) -> dict[str, Any]:
    return {
        "subject": subject,
        "language": facts.language,
        "approved": [dict(item) for item in facts.approved],
        "missing": list(facts.missing),
        "unverified": [dict(item) for item in facts.unverified],
        "input_hash": incoming,
        "version": version,
        "sources": [str(item["source_ref"]) for item in facts.approved if item.get("source_ref")],
    }


def _input_hash(
    facts: OrderFacts,
    *,
    kind: str,
    platform: str | None,
    subject: str,
    limit: int,
    prompt_version: str,
    extra: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "approved": [dict(item) for item in facts.approved],
        "missing": list(facts.missing),
        "unverified": [dict(item) for item in facts.unverified],
        "kind": kind,
        "platform": platform,
        "subject": subject,
        "language": facts.language,
        "limit": limit,
        "prompt_version": prompt_version,
    }
    if extra:
        payload["extra"] = dict(extra)
    return digest_json(payload)


def _find(session: Session, order_id: int, kind: str, platform: str | None, subject: str) -> ContentItem | None:
    rows = session.scalars(
        select(ContentItem).where(ContentItem.order_id == order_id, ContentItem.kind == kind).order_by(ContentItem.id)
    ).all()
    for row in rows:
        brief = row.brief if isinstance(row.brief, dict) else {}
        if row.platform == platform and brief.get("subject") == subject:
            return row
    return None


def _context(
    session: Session, order_id: int
) -> tuple[Order, Company, dict[str, Any], dict[str, Any], tuple[str, ...]]:
    order = session.get(Order, order_id)
    if order is None or order.company_id is None:
        raise ContentError("order not found")
    company = session.get(Company, order.company_id)
    intake = session.get(Intake, order_id)
    if company is None or intake is None or not isinstance(intake.checklist, dict):
        raise ContentError("order not found")
    profile_row = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == company.id).order_by(CompanyProfile.version.desc())
    )
    profile = profile_row.data if profile_row is not None and isinstance(profile_row.data, dict) else {}
    messages = tuple(
        row.text or ""
        for row in session.scalars(
            select(ClientMessage).where(ClientMessage.order_id == order_id).order_by(ClientMessage.id)
        ).all()
    )
    return order, company, profile, intake.checklist, messages


def _reviews(session: Session, order_id: int) -> tuple[Review, ...]:
    rows = session.scalars(select(Review).where(Review.order_id == order_id).order_by(Review.id)).all()
    return tuple(row for row in rows if isinstance(row.text, str) and row.text.strip() and row.rating is not None)


def _reply_consent(session: Session, row: ContentItem, brief: Mapping[str, Any]) -> bool:
    if row.order_id is None:
        return False
    intake = session.get(Intake, row.order_id)
    if intake is None or not isinstance(intake.checklist, dict):
        return False
    consent = intake.checklist.get("review_reply_consent")
    if not isinstance(consent, list):
        return False
    review_id = brief.get("review_id")
    return review_id in consent or str(review_id) in {str(item) for item in consent}


def _approval_current(brief: Mapping[str, Any]) -> bool:
    approval = brief.get("approval")
    if not isinstance(approval, dict):
        return False
    text = brief.get("text")
    if isinstance(text, str) and text:
        if digest_text(text) != brief.get("content_hash"):
            return False
    return approval.get("facts_hash") == brief.get("input_hash") and approval.get("content_hash") == brief.get("content_hash")


def _public(row: ContentItem) -> dict[str, Any]:
    brief = row.brief if isinstance(row.brief, dict) else {}
    return {
        "id": row.id,
        "kind": row.kind,
        "platform": row.platform,
        "status": row.status,
        "version": int(brief.get("version") or 1),
        "origin": brief.get("origin"),
        "illustrative": brief.get("illustrative") is True,
        "language": brief.get("language"),
        "rejection": brief.get("rejection"),
        "approved": isinstance(brief.get("approval"), dict),
        "source_refs": brief.get("sources") or [],
        "facts": [
            {"path": item.get("path"), "source_ref": item.get("source_ref"), "value": item.get("value")}
            for item in (brief.get("approved") or [])
            if isinstance(item, dict)
        ][:20],
        "text": brief.get("text") if isinstance(brief.get("text"), str) else None,
        "missing": brief.get("missing") or [],
        "unverified": [item.get("path") for item in brief.get("unverified") or [] if isinstance(item, dict)],
    }
