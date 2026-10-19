"""Approved client files and E6 content. Missing generated content does not invent a file."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.content.kinds import TEXT_KINDS
from zhol.content.hashing import digest_text
from zhol.db.models import ClientFile, ContentItem
from zhol.guards import listing_media_allowed

APPROVED_CONTENT = "checked"


@dataclass(frozen=True)
class FileRef:
    id: int
    kind: str
    path: str


@dataclass(frozen=True)
class ContentPiece:
    id: int
    kind: str
    platform: str | None
    path: str
    text: str | None = None
    origin: str | None = None


@dataclass(frozen=True)
class ContentLibrary:
    availability: str
    approved: tuple[ContentPiece, ...]
    pending_kinds: tuple[str, ...]
    texts: tuple[ContentPiece, ...] = ()

    def approved_for(self, kind: str, platform: str) -> ContentPiece | None:
        matches = [
            item
            for item in self.approved
            if item.kind == kind and (item.platform is None or item.platform == platform)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.id)[0]

    def approved_text(self, kind: str, platform: str | None) -> ContentPiece | None:
        matches = [
            item
            for item in self.texts
            if item.kind == kind and item.text and item.platform == platform
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.id)[-1]


def load_files(session: Session, order_id: int) -> dict[str, tuple[FileRef, ...]]:
    rows = session.scalars(select(ClientFile).where(ClientFile.order_id == order_id).order_by(ClientFile.id)).all()
    grouped: dict[str, list[FileRef]] = {}
    for row in rows:
        grouped.setdefault(row.kind, []).append(FileRef(id=row.id, kind=row.kind, path=row.path))
    return {kind: tuple(items) for kind, items in grouped.items()}


def load_content(session: Session, order_id: int) -> ContentLibrary:
    rows = session.scalars(select(ContentItem).where(ContentItem.order_id == order_id).order_by(ContentItem.id)).all()
    if not rows:
        return ContentLibrary("not_available", (), ())
    approved: list[ContentPiece] = []
    texts: list[ContentPiece] = []
    pending: list[str] = []
    for row in rows:
        brief = row.brief if isinstance(row.brief, dict) else {}
        if row.kind in TEXT_KINDS:
            piece = _approved_text_piece(row, brief)
            if piece is None:
                if row.status not in {"rejected"}:
                    pending.append(row.kind)
                continue
            texts.append(piece)
            continue
        illustrative = brief.get("illustrative") is True
        try:
            listing_media_allowed(row.kind, status=row.status, illustrative=illustrative)
        except PermissionError:
            pending.append(row.kind)
            continue
        if row.status == APPROVED_CONTENT and illustrative and row.result_path:
            origin = brief.get("origin") if isinstance(brief.get("origin"), str) else None
            approved.append(
                ContentPiece(id=row.id, kind=row.kind, platform=row.platform, path=row.result_path, origin=origin)
            )
        else:
            pending.append(row.kind)
    availability = "available" if approved and not pending else "incomplete"
    if texts and not pending and not approved:
        availability = "incomplete"
    return ContentLibrary(availability, tuple(approved), tuple(sorted(set(pending))), tuple(texts))


def _approved_text_piece(row: ContentItem, brief: dict) -> ContentPiece | None:
    if row.status != APPROVED_CONTENT:
        return None
    text = brief.get("text")
    approval = brief.get("approval")
    if not isinstance(text, str) or not text.strip() or not isinstance(approval, dict):
        return None
    content_hash = digest_text(text)
    if approval.get("facts_hash") != brief.get("input_hash"):
        return None
    if approval.get("content_hash") != brief.get("content_hash") or brief.get("content_hash") != content_hash:
        return None
    if brief.get("valid") is not True:
        return None
    return ContentPiece(id=row.id, kind=row.kind, platform=row.platform, path=row.result_path or "", text=text)
