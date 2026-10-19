"""E8 checks stored evidence and writes a completion report. It does not open a listing."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.cli import main
from zhol.db.models import (
    Audit,
    CardSnapshot,
    ChangeItem,
    ChangeLog,
    ChangePackage,
    ContentItem,
    Credential,
    HumanTask,
    ListingApproval,
    ListingImprovementPlan,
    ListingSubmission,
    Report,
)
from zhol.orders.intake import create_test_order
from zhol.reports.completion import generate_completion_report, verify_order
from zhol.timeutil import now

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
HOURS = "10:00-20:00"
BEFORE = "09:00-18:00"


@pytest.fixture
def session(migrated_engine: Engine):
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def test_draft_only_is_not_completed_work(session: Session) -> None:
    order = _order(session, "e8-draft", ("yandex",))
    package = _package(session, order.id)
    item = _change(session, order.id, package, "yandex")
    session.add(
        ChangeLog(
            change_item_id=item.id,
            action="prepare",
            result="dry_run",
            details={"submitted": False, "published": False, "mode": "dry_run"},
            at=now(),
        )
    )
    _audit(session, order.company_id)
    session.add(
        HumanTask(
            reason="yandex_listing_review",
            object_type="order",
            object_id=order.id,
            details={"platform": "yandex", "approval": "required"},
            status="open",
            created_at=now(),
        )
    )
    session.add(
        ContentItem(
            order_id=order.id,
            kind="cover",
            platform=None,
            brief={"illustrative": False, "origin": "upload", "label": "реальное фото салона", "version": 1},
            status="done",
        )
    )
    session.flush()
    document = verify_order(session, order.id)
    html = _html(document)
    assert document["completed"] is False
    assert document["outcome"] == "not_submitted"
    assert document["live_verified"] is False
    assert document["platforms"][0]["status"] == "not_submitted"
    assert document["platforms"][0]["changes"][0]["draft"] is True
    assert document["verified_changes"] == []
    assert document["deficiencies"]
    assert any(row["kind"] == "human_task" for row in document["outstanding"])
    assert document["content"]["illustrations"][0]["label"] == "Иллюстрация, не фотография компании."
    assert document["content"]["illustrations"][0]["real_photo"] is False
    assert "реальное фото салона" not in html
    assert "published_verified" not in html
    assert "completed" not in html
    assert "live verified" not in html.casefold()
    assert "Черновик, форма не отправлена." in html
    assert "Живая проверка карточек не выполнялась." in html


def test_approved_but_not_submitted(session: Session) -> None:
    order = _order(session, "e8-approved", ("google",))
    package = _package(session, order.id, status="approved")
    _change(session, order.id, package, "google")
    _submission(session, order.id, package, "google", status="approved", submitted_at=None)
    document = verify_order(session, order.id)
    platform = document["platforms"][0]
    assert platform["status"] == "not_submitted"
    assert platform["submission_status"] == "approved"
    assert platform["changes"][0]["approved"] == HOURS
    assert platform["evidence"]["submit"] is None
    assert document["verified_changes"] == []
    assert document["completed"] is False


def test_submitted_click_is_not_publication(session: Session) -> None:
    order = _order(session, "e8-submitted", ("2gis",))
    package = _package(session, order.id)
    item = _change(session, order.id, package, "2gis")
    submission = _submission(session, order.id, package, "2gis", status="submitted", moderation="unconfirmed")
    session.add(
        ChangeLog(
            change_item_id=item.id,
            action="apply",
            result="ok",
            details={"submitted": True, "published": True, "mode": "offline", "status": "submitted"},
            at=now(),
        )
    )
    session.flush()
    before = submission.status
    logs = session.scalar(select(func.count()).select_from(ChangeLog))
    document = verify_order(session, order.id)
    assert document["platforms"][0]["status"] == "submitted"
    assert document["platforms"][0]["evidence"]["submit"]["counts_as_publication"] is False
    assert document["platforms"][0]["evidence"]["after"] is None
    assert "unsupported_claim" in document["issues"]
    assert document["completed"] is False
    assert document["verified_changes"] == []
    assert submission.status == before
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == logs


def test_moderation_pending_stays_pending(session: Session) -> None:
    order = _order(session, "e8-pending", ("yandex",))
    package = _package(session, order.id)
    _change(session, order.id, package, "yandex")
    _submission(session, order.id, package, "yandex", status="moderation_pending", moderation="pending")
    document = verify_order(session, order.id)
    platform = document["platforms"][0]
    assert platform["status"] == "moderation_pending"
    assert platform["moderation_status"] == "pending"
    assert document["outcome"] == "moderation_pending"
    assert document["completed"] is False
    assert any("модерации" in row["text"] for row in document["client_actions"])


def test_independent_snapshot_verifies_without_a_live_check(session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    order = _order(session, "e8-verified", ("yandex",))
    package = _package(session, order.id)
    item = _change(session, order.id, package, "yandex")
    shot = _shot(tmp_path / "after.png")
    before = _shot(tmp_path / "before.png")
    session.add(
        ChangeLog(
            change_item_id=item.id,
            action="prepare",
            result="prepared",
            screenshot_before=str(before),
            details={"submitted": False, "published": False, "mode": "prepared"},
            at=now() - timedelta(hours=2),
        )
    )
    submission = _submission(
        session,
        order.id,
        package,
        "yandex",
        status="submitted",
        moderation="unconfirmed",
        screenshot_before=str(before),
        screenshot_after=str(_shot(tmp_path / "submit-after.png")),
    )
    _public_snapshot(session, order.company_id, "yandex", {"hours": HOURS}, shot)
    session.add(
        ContentItem(
            order_id=order.id,
            kind="company_description",
            platform=None,
            brief={
                "text": "Салон на Абая",
                "illustrative": False,
                "version": 1,
                "input_hash": "facts",
                "content_hash": "body",
                "approval": {"facts_hash": "facts", "content_hash": "body", "at": now().isoformat()},
            },
            status="checked",
        )
    )
    session.add(
        ContentItem(
            order_id=order.id,
            kind="cover",
            platform=None,
            brief={"illustrative": True, "origin": "ai_illustration", "version": 1, "label": "реальное фото салона"},
            result_path=str(shot),
            status="checked",
        )
    )
    session.flush()
    document = verify_order(session, order.id)
    html = _html(document)
    platform = document["platforms"][0]
    assert platform["status"] == "published_verified"
    assert platform["live_verified"] is False
    assert document["completed"] is True
    assert document["outcome"] == "stored_verified"
    assert document["verification_mode"] == "stored_offline"
    assert document["verified_changes"][0]["field"] == "hours"
    assert document["verified_changes"][0]["observed"] == HOURS
    assert document["content"]["descriptions"][0]["text"] == "Салон на Абая"
    assert document["content"]["services"][0]["name"] == "Стрижка"
    assert document["content"]["illustrations"][0]["label"] == "Иллюстрация, не фотография компании."
    assert "реальное фото салона" not in html
    assert platform["evidence"]["after"]["independent"] is True
    assert platform["evidence"]["after"]["live"] is False
    assert platform["evidence"]["submit"]["counts_as_publication"] is False
    assert platform["evidence"]["before"]["screenshot_exists"] is True
    assert document["verified_at"] and document["generated_at"]
    assert platform["package_version"] == 1
    assert submission.status == "submitted"
    sources = dict(_snapshot_row(session).field_sources)
    sources["_collection"] = {"live": True}
    _snapshot_row(session).field_sources = sources
    session.flush()
    claimed = verify_order(session, order.id)
    assert claimed["platforms"][0]["status"] == "uncertain"
    assert claimed["live_verified"] is False
    assert "unsupported_claim" in claimed["issues"]
    assert claimed["completed"] is False


def test_rejected_edit_is_not_verified(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-rejected", ("google",))
    package = _package(session, order.id)
    _change(session, order.id, package, "google")
    _submission(session, order.id, package, "google", status="rejected", moderation="rejected")
    refused = verify_order(session, order.id)
    assert refused["platforms"][0]["status"] == "rejected"
    assert refused["outcome"] == "rejected"
    assert refused["verified_changes"] == []
    _public_snapshot(session, order.company_id, "google", {"hours": HOURS}, _shot(tmp_path / "after.png"))
    conflict = verify_order(session, order.id)
    assert conflict["platforms"][0]["status"] == "uncertain"
    assert conflict["verified_changes"] == []
    assert "rejected_but_snapshot_matches" in conflict["issues"]


def test_partial_success_across_platforms(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-partial", ("2gis", "yandex", "google"))
    package = _package(session, order.id)
    _change(session, order.id, package, "yandex", n=1)
    _change(session, order.id, package, "google", n=2)
    shot = _shot(tmp_path / "yandex.png")
    _submission(
        session,
        order.id,
        package,
        "yandex",
        status="submitted",
        moderation="unconfirmed",
        screenshot_before=str(shot),
        screenshot_after=str(shot),
    )
    _submission(session, order.id, package, "google", status="moderation_pending", moderation="pending")
    _public_snapshot(session, order.company_id, "yandex", {"hours": HOURS}, shot)
    _audit(session, order.company_id)
    document = verify_order(session, order.id)
    by_name = {row["platform"]: row["status"] for row in document["platforms"]}
    assert by_name == {"2gis": "not_submitted", "yandex": "published_verified", "google": "moderation_pending"}
    assert document["outcome"] == "partial"
    assert document["completed"] is False
    assert document["verified_changes"][0]["platform"] == "yandex"
    assert any(row["source"] == "audit" for row in document["deficiencies"])
    assert any("2gis" in row["text"] for row in document["client_actions"])


def test_partial_fields_are_not_fully_verified(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-fields", ("yandex",))
    package = _package(session, order.id)
    _change(session, order.id, package, "yandex", field="hours", n=1)
    _change(session, order.id, package, "yandex", field="description", before=None, after="Салон на Абая", n=2)
    _submission(
        session,
        order.id,
        package,
        "yandex",
        status="submitted",
        moderation="unconfirmed",
        fields=_fields(package, description="Салон на Абая"),
    )
    _public_snapshot(session, order.company_id, "yandex", {"hours": HOURS}, _shot(tmp_path / "after.png"))
    document = verify_order(session, order.id)
    platform = document["platforms"][0]
    assert platform["status"] == "uncertain"
    assert platform["partial"] is True
    assert "partial_fields" in platform["issues"]
    assert any(item["kind"] == "not_seen" for item in platform["discrepancies"])
    assert document["verified_changes"] == []
    assert document["completed"] is False


def test_stale_evidence_is_not_verification(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-stale", ("yandex",))
    package = _package(session, order.id)
    _change(session, order.id, package, "yandex")
    submitted = now() - timedelta(days=20)
    _submission(session, order.id, package, "yandex", status="submitted", moderation="unconfirmed", submitted_at=submitted)
    _public_snapshot(
        session,
        order.company_id,
        "yandex",
        {"hours": HOURS},
        _shot(tmp_path / "old.png"),
        collected_at=now() - timedelta(days=15),
    )
    document = verify_order(session, order.id)
    assert document["platforms"][0]["status"] == "uncertain"
    assert "stale_evidence" in document["issues"]
    assert document["verified_changes"] == []
    assert document["completed"] is False


def test_missing_screenshot_blocks_verification(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-shot", ("google",))
    package = _package(session, order.id)
    _change(session, order.id, package, "google")
    _submission(session, order.id, package, "google", status="submitted", moderation="unconfirmed")
    _public_snapshot(session, order.company_id, "google", {"hours": HOURS}, tmp_path / "missing.png")
    document = verify_order(session, order.id)
    assert document["platforms"][0]["status"] == "uncertain"
    assert "missing_screenshot" in document["issues"]
    assert document["platforms"][0]["evidence"]["after"]["screenshot_exists"] is False
    assert document["verified_changes"] == []


def test_changed_package_version_needs_a_person(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-version", ("2gis",))
    package = _package(session, order.id)
    item = _change(session, order.id, package, "2gis")
    _submission(session, order.id, package, "2gis", status="submitted", moderation="unconfirmed")
    _public_snapshot(session, order.company_id, "2gis", {"hours": HOURS}, _shot(tmp_path / "after.png"))
    item.after = "11:00-19:00"
    session.flush()
    document = verify_order(session, order.id)
    assert document["platforms"][0]["status"] == "human_required"
    assert "changed_package_version" in document["issues"]
    assert document["verified_changes"] == []
    assert document["outcome"] == "blocked"
    assert any(row["kind"] == "package" for row in document["client_actions"])


def test_changed_content_version_is_not_current(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-content", ("yandex",))
    package = _package(session, order.id)
    text = "Салон на Абая"
    content = ContentItem(
        order_id=order.id,
        kind="platform_description",
        platform="yandex",
        brief={
            "text": text,
            "illustrative": False,
            "version": 2,
            "input_hash": "new",
            "content_hash": "new",
            "approval": {"facts_hash": "old", "content_hash": "old", "at": now().isoformat()},
        },
        status="checked",
    )
    session.add(content)
    session.flush()
    _change(session, order.id, package, "yandex", field="description", before=None, after=text, source_ref=f"content:{content.id}")
    _submission(
        session,
        order.id,
        package,
        "yandex",
        status="submitted",
        moderation="unconfirmed",
        fields=_fields(package, hours=False, description=text),
    )
    _public_snapshot(session, order.company_id, "yandex", {"description": text}, _shot(tmp_path / "after.png"))
    document = verify_order(session, order.id)
    assert document["platforms"][0]["status"] == "human_required"
    assert "changed_content_version" in document["issues"]
    assert document["content"]["descriptions"] == []
    assert document["verified_changes"] == []
    assert document["completed"] is False


def test_report_generation_is_idempotent(session: Session, tmp_path: Path) -> None:
    order = _order(session, "e8-idem", ("yandex",))
    package = _package(session, order.id)
    _change(session, order.id, package, "yandex")
    _submission(session, order.id, package, "yandex", status="approved", submitted_at=None)
    folder = tmp_path / "out"
    first = generate_completion_report(session, order.id, folder)
    json_bytes = (folder / "completion.json").read_bytes()
    html_bytes = (folder / "completion.html").read_bytes()
    second = generate_completion_report(session, order.id, folder)
    assert (folder / "completion.json").read_bytes() == json_bytes
    assert (folder / "completion.html").read_bytes() == html_bytes
    assert first["json"] == second["json"]
    assert session.scalar(select(func.count()).select_from(Report).where(Report.order_id == order.id)) == 1
    stored = session.scalar(select(Report).where(Report.order_id == order.id))
    assert stored is not None
    assert stored.pdf_path is None
    assert stored.xlsx_path is None
    assert stored.data["kind"] == "completion"
    assert not (folder / "completion.pdf").exists()


def test_sensitive_values_are_redacted(session: Session) -> None:
    order = _order(session, "e8-secret", ("google",))
    package = _package(session, order.id)
    _change(session, order.id, package, "google")
    _submission(
        session,
        order.id,
        package,
        "google",
        status="submitted",
        moderation="unconfirmed",
        detail="Bearer SuperSecret99",
        confirmation="password=SuperSecret99",
    )
    session.add(
        HumanTask(
            reason="google_listing_review",
            object_type="order",
            object_id=order.id,
            details={"platform": "google", "detail": "token=SuperSecret99"},
            status="open",
            created_at=now(),
        )
    )
    session.add(
        Credential(
            company_id=order.company_id,
            platform="google",
            login_hint="SuperSecret99",
            ciphertext=b"ciphertext-secret-value",
        )
    )
    session.flush()
    document = verify_order(session, order.id)
    raw = json.dumps(document, ensure_ascii=False) + _html(document)
    assert "SuperSecret99" not in raw
    assert "ciphertext-secret-value" not in raw
    assert "[redacted]" in raw
    assert document["platforms"][0]["confirmation"] == "password=[redacted]"


def test_cli_verify_and_report(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with factory() as db, db.begin():
        order = _order(db, "e8-cli", ("yandex",))
        package = _package(db, order.id)
        _change(db, order.id, package, "yandex")
        _submission(db, order.id, package, "yandex", status="approved", submitted_at=None)
        order_id = order.id
    assert main(["verify-order", "--order-id", str(order_id)], factory=factory) == 0
    printed = capsys.readouterr().out
    assert "live_verified false" in printed
    assert "yandex not_submitted" in printed
    assert main(["verify-order", "--order-id", "999999"], factory=factory) == 2
    folder = tmp_path / "report"
    assert main(["generate-completion-report", "--order-id", str(order_id), "--out", str(folder)], factory=factory) == 0
    assert (folder / "completion.json").is_file()
    assert (folder / "completion.html").is_file()
    saved = json.loads((folder / "completion.json").read_text(encoding="utf-8"))
    assert saved["live_verified"] is False
    assert saved["completed"] is False


def _html(document: dict) -> str:
    from zhol.reports.completion import render_html

    return render_html(document)


def _order(session: Session, key: str, platforms: tuple[str, ...]):
    return create_test_order(
        session,
        {
            "key": key,
            "company": {"id_2gis": key, "name": "Salon Test", "city": "Алматы"},
            "platforms": [
                {"platform": name, "action": "update", "external_id": f"{name}-1", "owner_authorization": "verified"}
                for name in platforms
            ],
            "messages": [{"id": "m1", "text": "Салон на Абая. Часы и стрижка."}],
            "facts": [
                {"field": "address", "value": "ул. Абая 10", "source_ref": "msg:m1", "approved": True},
                {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "approved": True},
                {"field": "hours", "value": HOURS, "source_ref": "msg:m1", "approved": True},
                {"field": "description", "value": "Салон на Абая", "source_ref": "msg:m1", "approved": True},
                {
                    "field": "services",
                    "value": [{"name": "Стрижка", "price": 8000}],
                    "source_ref": "msg:m1",
                    "approved": True,
                },
            ],
            "materials": [
                {"kind": "logo", "path": "materials/logo.png", "ref": "logo"},
                {"kind": "photo_real", "path": "materials/hall.png", "ref": "hall"},
            ],
            "not_applicable": ["videos"],
        },
    )


def _package(session: Session, order_id: int, status: str = "draft") -> ChangePackage:
    row = session.scalar(select(ChangePackage).where(ChangePackage.order_id == order_id))
    assert row is not None
    row.status = status
    session.flush()
    return row


def _change(
    session: Session,
    order_id: int,
    package: ChangePackage,
    platform: str,
    *,
    field: str = "hours",
    before: str | None = BEFORE,
    after: str = HOURS,
    n: int = 1,
    source_ref: str = "msg:m1",
) -> ChangeItem:
    raw = json.dumps({"after": after, "field": field, "n": n, "order_id": order_id, "platform": platform}, sort_keys=True)
    item = ChangeItem(
        package_id=package.id,
        n=n,
        platform=platform,
        field=field,
        before=before,
        after=after,
        source_ref={"ref": source_ref},
        is_nap=False,
        counts_to_limit=False,
        status="draft",
        idempotency_key=hashlib.sha256(raw.encode()).hexdigest(),
    )
    session.add(item)
    session.flush()
    return item


def _submission(
    session: Session,
    order_id: int,
    package: ChangePackage,
    platform: str,
    *,
    status: str,
    moderation: str | None = None,
    submitted_at=None,
    screenshot_before: str | None = None,
    screenshot_after: str | None = None,
    detail: str | None = None,
    confirmation: str | None = None,
    fields: dict | None = None,
) -> ListingSubmission:
    payload = fields if fields is not None else _fields(package)
    if submitted_at is None and status not in {"approved", "prepared", "awaiting_approval"}:
        submitted_at = now() - timedelta(hours=1)
    row = ListingSubmission(
        order_id=order_id,
        package_id=package.id,
        platform=platform,
        operation="update",
        status=status,
        idempotency_key=hashlib.sha256(f"{order_id}:{package.id}:{platform}:{status}".encode()).hexdigest(),
        fields=payload,
        evidence={"package_version": package.version},
        confirmation=None if confirmation is None else {"text": confirmation},
        screenshot_before=screenshot_before,
        screenshot_after=screenshot_after,
        submitted_at=submitted_at,
        moderation_status=moderation,
        detail=detail,
        created_at=now(),
        updated_at=now(),
    )
    session.add(row)
    session.flush()
    if status in {"approved", "submitted", "moderation_pending", "published", "rejected"}:
        session.add(
            ListingApproval(
                order_id=order_id,
                package_id=package.id,
                platform=platform,
                operation="update",
                digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                fields=payload,
                evidence={"package_version": package.version},
                status="active",
                approved_at=now() - timedelta(hours=2),
                expires_at=now() + timedelta(hours=20),
            )
        )
        session.flush()
    return row


def _fields(package: ChangePackage, *, hours: bool = True, description: str | None = None) -> dict:
    changes = []
    if hours:
        changes.append({"after": HOURS, "before": BEFORE, "field": "hours", "source_ref": "msg:m1"})
    if description is not None:
        changes.append({"after": description, "before": None, "field": "description", "source_ref": "msg:m1"})
    return {"changes": changes, "package_version": package.version, "page": {}}


def _public_snapshot(session: Session, company_id: int, platform: str, fields: dict, shot: Path, collected_at=None) -> None:
    if shot.parent:
        shot.parent.mkdir(parents=True, exist_ok=True)
    when = now() if collected_at is None else collected_at
    sources = {
        key: {
            "url": f"https://example.test/{platform}",
            "seen_at": when.isoformat(),
            "screenshot": str(shot),
            "read_by": "code",
        }
        for key in fields
    }
    session.add(
        CardSnapshot(
            company_id=company_id,
            platform=platform,
            match_status="found",
            url=f"https://example.test/{platform}",
            fields=fields,
            field_sources=sources,
            screenshot_path=str(shot),
            purpose="report",
            collected_at=when,
            collected_by="stored_fixture",
            status="verified",
        )
    )
    session.flush()


def _snapshot_row(session: Session) -> CardSnapshot:
    row = session.scalar(select(CardSnapshot).order_by(CardSnapshot.id.desc()))
    assert row is not None
    return row


def _shot(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return path


def _audit(session: Session, company_id: int) -> None:
    audit = Audit(
        company_id=company_id,
        snapshot_ids=[],
        score_total=40,
        score_groups={},
        derived={},
        discrepancies={},
        gaps={"items": [{"platform": "google", "text": "описание пустое"}]},
        observations={"texts": ["В Google карточка не найдена по названию и адресу"]},
        status="draft",
        created_at=now(),
    )
    session.add(audit)
    session.flush()
    session.add(
        ListingImprovementPlan(
            company_id=company_id,
            audit_id=audit.id,
            source_key=f"bek:{company_id}",
            plan={
                "tasks": [
                    {
                        "type": "add_description",
                        "platform": "google",
                        "status": "manual",
                        "completed": False,
                        "current": "пусто",
                        "desired": "описание салона",
                    }
                ]
            },
            created_at=now(),
        )
    )
    session.flush()
