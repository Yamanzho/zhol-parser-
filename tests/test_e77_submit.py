"""E7.7 submits an approved draft only as an offline simulation."""

from __future__ import annotations

import json
from datetime import timedelta
from html import escape
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.test_e76_create import _confirm, _prepare, _ready, _search, _spec, _view
from zhol.browser.config import load_platforms
from zhol.browser.google.page import FixtureCabinet as GoogleCabinet
from zhol.browser.google.prepare import prepare_google_listing
from zhol.browser.twogis.page import FixtureCabinet as TwoGisCabinet
from zhol.browser.twogis.prepare import prepare_2gis_listing
from zhol.browser.yandex.page import FixtureCabinet as YandexCabinet
from zhol.browser.yandex.prepare import prepare_yandex_listing
from zhol.cli import main
from zhol.db.models import (
    ChangeLog,
    CompanyPlatformId,
    ListingApproval,
    ListingSubmission,
)
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter
from zhol.fulfillment.adapters.twogis import TwoGisAdapter
from zhol.fulfillment.adapters.yandex import YandexBusinessAdapter
from zhol.fulfillment.submission import (
    _attempt_count,
    approve_listing,
    submit_listing,
    submit_prepared_platforms,
    verify_listing,
)
from zhol.fulfillment.types import ListingContext, PreparedListing, SubmitReaction
from zhol.orders.intake import create_test_order
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "tests" / "fixtures" / "cabinet"
MARKERS = load_platforms()["markers"]
PLATFORMS = ("2gis", "yandex", "google")
ADAPTERS = {"2gis": TwoGisAdapter, "yandex": YandexBusinessAdapter, "google": GoogleBusinessAdapter}
UPDATE = {
    "2gis": (prepare_2gis_listing, TwoGisCabinet, "2gis-editor.html", "70000000000000081"),
    "yandex": (prepare_yandex_listing, YandexCabinet, "yandex.html", "yandex-1"),
    "google": (prepare_google_listing, GoogleCabinet, "google-editor.html", "google-1"),
}


@pytest.fixture
def session(migrated_engine: Engine):
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def test_publish_button_and_live_mode_are_refused(session: Session, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="publish is not a submit action"):
        _view("yandex").simulate_submit("Опубликовать")
    with pytest.raises(PermissionError, match="live listing submission is closed"):
        submit_listing(
            session,
            1,
            platform="yandex",
            operation="create",
            page=_view("yandex"),
            evidence_dir=tmp_path,
            mode="live",
        )
    order = _ready(session, "yandex", "e77-live")
    closed = submit_listing(
        session,
        order.id,
        platform="yandex",
        operation="create",
        page="https://cabinet.example/create",
        evidence_dir=tmp_path / "https",
        mode="offline",
    )
    assert closed.status == "human_required" and closed.published is False and closed.submitted is False
    ctx = ListingContext(
        order_id=1,
        company_id=1,
        company_name="Salon Test",
        platform="yandex",
        action="update",
        external_id="yandex-1",
        public_url=None,
        access_status="verified",
        claimed_authorization="verified",
        profile_status=None,
        page_html=None,
        snapshot_fields={},
        snapshot_match="found",
        screenshot_path=None,
    )
    with pytest.raises(PermissionError):
        YandexBusinessAdapter().apply_changes(ctx, PreparedListing("yandex", "update", "yandex-1", (), (), True))
    claimed = SubmitReaction("Сохранить", True, "submitted", "опубликовано", "unknown", True)
    dry = YandexBusinessAdapter().offline_submit("update", claimed)
    assert dry.result == "submitted" and dry.details["published"] is False
    google = GoogleBusinessAdapter().offline_submit(
        "create", SubmitReaction("Create", True, "moderation_pending", "на модерации", "unknown", False)
    )
    twogis = TwoGisAdapter().offline_submit(
        "update", SubmitReaction("Сохранить", True, "submitted", None, "unknown", False)
    )
    assert google.details["button"] == "Create" and google.result == "moderation_pending" and google.details["published"] is False
    assert twogis.details["button"] == "Сохранить" and twogis.details["published"] is False


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("operation", ("create", "update"))
def test_submit_clicks_only_the_operation_button(session: Session, tmp_path: Path, platform: str, operation: str) -> None:
    order, view, folder = _draft(session, platform, operation, f"e77-btn-{operation}-{platform}", tmp_path)
    approved = approve_listing(session, order.id, platform=platform, operation=operation, page=view, evidence_dir=folder)
    assert approved.status == "approved" and approved.published is False
    report = submit_listing(session, order.id, platform=platform, operation=operation, page=view, evidence_dir=folder)
    button = ADAPTERS[platform].submit_buttons[operation]
    assert report.status == "submitted" and report.submitted is True and report.published is False
    assert view.clicked() == (button,)
    assert "Опубликовать" not in view.clicked() and "Publish" not in view.clicked()
    saved = json.loads((folder / "submission.json").read_text(encoding="utf-8"))
    assert saved["published"] is False and saved["package_version"] == 1
    assert (folder / "submit-before.png").read_bytes().startswith(b"\x89PNG")
    assert (folder / "submit-after.png").read_bytes().startswith(b"\x89PNG")
    logs = list(session.scalars(select(ChangeLog).where(ChangeLog.action.in_(("apply", "create_card")))))
    assert logs and all(log.details["published"] is False and log.details["mode"] == "offline" for log in logs)


def test_missing_and_expired_approval(session: Session, tmp_path: Path) -> None:
    missing_order, missing_view, missing_folder = _draft(session, "yandex", "create", "e77-missing", tmp_path / "missing")
    missing = submit_listing(
        session, missing_order.id, platform="yandex", operation="create", page=missing_view, evidence_dir=missing_folder
    )
    assert missing.status == "awaiting_approval" and missing.detail == "approval is missing"
    assert missing.submitted is False and missing.published is False and missing_view.clicked() == ()

    order, view, folder = _draft(session, "2gis", "create", "e77-expired", tmp_path / "expired")
    approved = approve_listing(session, order.id, platform="2gis", operation="create", page=view, evidence_dir=folder)
    approval = session.get(ListingApproval, approved.approval_id)
    assert approval is not None
    approval.expires_at = now() - timedelta(hours=1)
    expired = submit_listing(session, order.id, platform="2gis", operation="create", page=view, evidence_dir=folder)
    assert expired.status == "awaiting_approval" and expired.detail == "approval expired"
    assert expired.published is False and view.clicked() == ()
    assert approval.status == "expired"


def test_changed_values_invalidate_approval(session: Session, tmp_path: Path) -> None:
    order, view, folder = _draft(session, "google", "create", "e77-changed", tmp_path)
    approved = approve_listing(session, order.id, platform="google", operation="create", page=view, evidence_dir=folder)
    approval = session.get(ListingApproval, approved.approval_id)
    assert approval is not None and approval.status == "active"
    item = session.scalar(select(ChangeLog).where(ChangeLog.action == "prepare"))
    assert item is not None and item.change_item_id is not None
    from zhol.db.models import ChangeItem

    change = session.get(ChangeItem, item.change_item_id)
    assert change is not None
    change.after = "Другое имя"
    session.flush()
    report = submit_listing(session, order.id, platform="google", operation="create", page=view, evidence_dir=folder)
    assert report.status == "awaiting_approval" and report.detail == "planned values changed"
    assert report.published is False and view.clicked() == ()
    assert approval.status == "invalidated"


def test_wrong_organization_and_lost_ownership(session: Session, tmp_path: Path) -> None:
    wrong_order, wrong_view, wrong_folder = _draft(session, "yandex", "update", "e77-org", tmp_path / "org")
    approve_listing(session, wrong_order.id, platform="yandex", operation="update", page=wrong_view, evidence_dir=wrong_folder)
    wrong_view.external_id = "other-org"
    wrong = submit_listing(
        session, wrong_order.id, platform="yandex", operation="update", page=wrong_view, evidence_dir=wrong_folder
    )
    assert wrong.status == "human_required" and wrong.detail == "organization does not match the order"
    assert wrong.published is False and wrong_view.clicked() == ()

    lost_order, lost_view, lost_folder = _draft(session, "google", "update", "e77-owner", tmp_path / "owner")
    approve_listing(session, lost_order.id, platform="google", operation="update", page=lost_view, evidence_dir=lost_folder)
    assert lost_order.company_id is not None
    stored = session.get(CompanyPlatformId, (lost_order.company_id, "google"))
    assert stored is not None
    stored.access_status = "none"
    lost = submit_listing(
        session, lost_order.id, platform="google", operation="update", page=lost_view, evidence_dir=lost_folder
    )
    assert lost.status == "human_required" and lost.detail == "missing owner permission"
    assert lost.published is False and lost_view.clicked() == ()

    role_order, role_view, role_folder = _draft(session, "2gis", "create", "e77-role", tmp_path / "role")
    approve_listing(session, role_order.id, platform="2gis", operation="create", page=role_view, evidence_dir=role_folder)
    role_view.fields["access_role"] = "Наблюдатель"
    role = submit_listing(session, role_order.id, platform="2gis", operation="create", page=role_view, evidence_dir=role_folder)
    assert role.status == "human_required" and role.detail == "missing owner permission" and role_view.clicked() == ()


def test_duplicate_and_uncertain_create_is_not_retried(session: Session, tmp_path: Path) -> None:
    order, view, folder = _draft(session, "yandex", "create", "e77-dup", tmp_path / "dup")
    approve_listing(session, order.id, platform="yandex", operation="create", page=view, evidence_dir=folder)
    first = submit_listing(session, order.id, platform="yandex", operation="create", page=view, evidence_dir=folder)
    assert first.status == "submitted" and first.duplicate is False
    second = submit_listing(session, order.id, platform="yandex", operation="create", page=view, evidence_dir=folder)
    assert second.status == "submitted" and second.duplicate is True and second.published is False
    assert view.clicked() == ("Создать",)
    assert _count(session, order.id, "yandex") == 1

    crashed_order, crashed, crashed_folder = _draft(session, "2gis", "create", "e77-crash", tmp_path / "crash")
    approve_listing(session, crashed_order.id, platform="2gis", operation="create", page=crashed, evidence_dir=crashed_folder)
    crashed.submit_outcome = "browser_crash"
    crash = submit_listing(
        session, crashed_order.id, platform="2gis", operation="create", page=crashed, evidence_dir=crashed_folder
    )
    assert crash.status == "uncertain" and crash.submitted is False and crash.published is False
    again = submit_listing(
        session, crashed_order.id, platform="2gis", operation="create", page=crashed, evidence_dir=crashed_folder
    )
    assert again.status == "uncertain" and again.published is False
    assert crashed.clicked() == ("Создать",)
    assert _count(session, crashed_order.id, "2gis") == 1

    net_order, net, net_folder = _draft(session, "google", "create", "e77-net", tmp_path / "net")
    approve_listing(session, net_order.id, platform="google", operation="create", page=net, evidence_dir=net_folder)
    net.submit_outcome = "network_failure"
    net.request_sent = "false"
    known = submit_listing(session, net_order.id, platform="google", operation="create", page=net, evidence_dir=net_folder)
    assert known.status == "approved" and known.submitted is False and known.published is False
    unknown_order, unknown, unknown_folder = _draft(session, "yandex", "update", "e77-unknown-net", tmp_path / "unet")
    approve_listing(
        session, unknown_order.id, platform="yandex", operation="update", page=unknown, evidence_dir=unknown_folder
    )
    unknown.submit_outcome = "network_failure"
    lost = submit_listing(
        session, unknown_order.id, platform="yandex", operation="update", page=unknown, evidence_dir=unknown_folder
    )
    retry = submit_listing(
        session, unknown_order.id, platform="yandex", operation="update", page=unknown, evidence_dir=unknown_folder
    )
    assert lost.status == "uncertain" and retry.status == "uncertain" and retry.published is False
    assert _count(session, unknown_order.id, "yandex") == 1


def test_layout_rejection_moderation_and_no_accidental_publish(session: Session, tmp_path: Path) -> None:
    plain_order, plain, plain_folder = _draft(session, "yandex", "create", "e77-layout", tmp_path / "layout")
    approve_listing(session, plain_order.id, platform="yandex", operation="create", page=plain, evidence_dir=plain_folder)
    plain.body = plain.body.replace("Создать", "Другая кнопка")
    layout = submit_listing(
        session, plain_order.id, platform="yandex", operation="create", page=plain, evidence_dir=plain_folder
    )
    assert layout.status == "human_required" and layout.detail == "stopped_unknown_ui" and plain.clicked() == ()

    extra_order, extra, extra_folder = _draft(session, "yandex", "create", "e77-extra", tmp_path / "extra")
    approve_listing(session, extra_order.id, platform="yandex", operation="create", page=extra, evidence_dir=extra_folder)
    extra.write_control("textbox", "WhatsApp", "+7 700 000 00 99")
    unrelated = submit_listing(
        session, extra_order.id, platform="yandex", operation="create", page=extra, evidence_dir=extra_folder
    )
    assert unrelated.status == "human_required" and unrelated.detail == "cabinet page differs from the approved draft"
    assert extra.clicked() == ()

    rejected_order, rejected_view, rejected_folder = _draft(session, "2gis", "update", "e77-reject", tmp_path / "reject")
    approve_listing(
        session, rejected_order.id, platform="2gis", operation="update", page=rejected_view, evidence_dir=rejected_folder
    )
    rejected_view.submit_outcome = "rejected"
    rejected = submit_listing(
        session, rejected_order.id, platform="2gis", operation="update", page=rejected_view, evidence_dir=rejected_folder
    )
    assert rejected.status == "rejected" and rejected.published is False and rejected.submitted is True
    assert rejected_view.clicked() == ("Сохранить",)

    pending_order, pending, pending_folder = _draft(session, "google", "create", "e77-mod", tmp_path / "mod")
    approve_listing(session, pending_order.id, platform="google", operation="create", page=pending, evidence_dir=pending_folder)
    pending.submit_outcome = "moderation_pending"
    pending.confirmation = "отправлено на модерацию"
    moderation = submit_listing(
        session, pending_order.id, platform="google", operation="create", page=pending, evidence_dir=pending_folder
    )
    assert moderation.status == "moderation_pending" and moderation.published is False
    assert moderation.confirmation == "отправлено на модерацию" and moderation.moderation_status == "pending"
    assert pending.clicked() == ("Create",)

    claim_order, claim, claim_folder = _draft(session, "yandex", "update", "e77-claim", tmp_path / "claim")
    approve_listing(session, claim_order.id, platform="yandex", operation="update", page=claim, evidence_dir=claim_folder)
    claim.submit_outcome = "published"
    claim.confirmation = "password=hunter2 опубликовано"
    clicked = submit_listing(
        session, claim_order.id, platform="yandex", operation="update", page=claim, evidence_dir=claim_folder
    )
    assert clicked.status == "submitted" and clicked.published is False and clicked.confirmation is None
    blob = (claim_folder / "submission.json").read_text(encoding="utf-8")
    assert "hunter2" not in blob and '"published": false' in blob
    checked = _clone("yandex", claim)
    checked.verification = "published"
    verified = verify_listing(
        session, claim_order.id, platform="yandex", operation="update", page=checked, evidence_dir=claim_folder
    )
    assert verified.status == "published" and verified.published is True and checked.clicked() == ()


def test_partial_platform_failure(session: Session, tmp_path: Path) -> None:
    spec = _spec("2gis", "e77-partial", company_id="70000000000077001", message_id="m-partial")
    spec["platforms"].append({"platform": "yandex", "action": "create", "owner_authorization": "verified"})
    order = create_test_order(session, spec)
    _confirm(session, order.id, "2gis")
    _confirm(session, order.id, "yandex")
    assert order.company_id is not None
    _search(session, order.company_id, "2gis")
    _search(session, order.company_id, "yandex")
    twogis = _view("2gis")
    yandex = _view("yandex")
    assert _prepare(session, "2gis", order.id, twogis, tmp_path / "2gis").status == "prepared"
    assert _prepare(session, "yandex", order.id, yandex, tmp_path / "yandex").status == "prepared"
    assert approve_listing(session, order.id, platform="2gis", operation="create", page=twogis, evidence_dir=tmp_path / "2gis").status == "approved"
    assert approve_listing(session, order.id, platform="yandex", operation="create", page=yandex, evidence_dir=tmp_path / "yandex").status == "approved"
    yandex.submit_outcome = "browser_crash"
    first = submit_prepared_platforms(session, order.id, {"2gis": twogis, "yandex": yandex}, tmp_path / "out")
    by_platform = {item.platform: item for item in first}
    assert by_platform["2gis"].status == "submitted" and by_platform["2gis"].published is False
    assert by_platform["yandex"].status == "uncertain" and by_platform["yandex"].published is False
    second = submit_prepared_platforms(session, order.id, {"2gis": twogis, "yandex": yandex}, tmp_path / "out")
    again = {item.platform: item for item in second}
    assert again["2gis"].status == "submitted" and again["2gis"].duplicate is True
    assert again["yandex"].status == "uncertain"
    assert _count(session, order.id, "2gis") == 1 and _count(session, order.id, "yandex") == 1
    assert twogis.clicked() == ("Создать",) and yandex.clicked() == ("Создать",)


def test_cli_submit_stays_offline(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with factory() as db, db.begin():
        order, view, folder = _draft(db, "yandex", "create", "e77-cli", tmp_path / "prep")
        approved = approve_listing(db, order.id, platform="yandex", operation="create", page=view, evidence_dir=folder)
        assert approved.status == "approved"
        order_id = order.id
        page = tmp_path / "page.html"
        page.write_text(_dump(view, "moderation_pending", "карточка на модерации"), encoding="utf-8")
    evidence = tmp_path / "submit"
    assert (
        main(
            [
                "submit-listing",
                "--order-id",
                str(order_id),
                "--platform",
                "yandex",
                "--operation",
                "create",
                "--page",
                str(page),
                "--evidence",
                str(evidence),
            ],
            factory=factory,
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "mode offline" in printed and "status moderation_pending" in printed and "published false" in printed
    saved = json.loads((evidence / "submission.json").read_text(encoding="utf-8"))
    assert saved["published"] is False and saved["submitted"] is True and saved["confirmation"] == "карточка на модерации"


def _draft(session: Session, platform: str, operation: str, key: str, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    if operation == "create":
        order = _ready(session, platform, key, company_id=f"77{key}", message_id=f"m-{key}")
        view = _view(platform)
        report = _prepare(session, platform, order.id, view, folder)
    else:
        order, view, report = _update(session, platform, key, folder)
    assert report.status == "prepared", report.detail
    return order, view, folder


def _update(session: Session, platform: str, key: str, folder: Path):
    prepare, cabinet, page_name, external_id = UPDATE[platform]
    spec = _spec(platform, key, company_id=f"78{key}", message_id=f"m-{key}")
    spec["platforms"] = [
        {"platform": platform, "action": "update", "external_id": external_id, "owner_authorization": "verified"}
    ]
    spec["changes"] = [
        {
            "platform": platform,
            "field": "phones",
            "after": ["+7 701 111 22 33"],
            "source_ref": f"msg:m-{key}",
            "nap_consent_ref": f"msg:m-{key}",
        },
        {
            "platform": platform,
            "field": "category",
            "after": "Парикмахерская",
            "source_ref": f"msg:m-{key}",
            "nap_consent_ref": f"msg:m-{key}",
        },
    ]
    order = create_test_order(session, spec)
    view = cabinet((PAGES / page_name).read_text(encoding="utf-8"), MARKERS)
    report = prepare(session, order.id, page=view, evidence_dir=folder)
    return order, view, report


def _clone(platform: str, source: object):
    prepare, cabinet, page_name, _external = UPDATE[platform]
    del prepare
    clone = cabinet((PAGES / page_name).read_text(encoding="utf-8"), MARKERS)
    current = {control.name: control for control in clone.controls}
    for control in source.controls:  # type: ignore[attr-defined]
        if control.name in current and not control.secret:
            current[control.name].value = control.value
    clone.fields = dict(source.fields)  # type: ignore[attr-defined]
    clone.external_id = source.external_id  # type: ignore[attr-defined]
    clone.page = source.page  # type: ignore[attr-defined]
    clone.platform = source.platform  # type: ignore[attr-defined]
    return clone


def _dump(view: object, outcome: str, confirmation: str) -> str:
    lines = [
        "<!DOCTYPE html><html><body",
        f' data-page="{escape(view.page or "")}"',  # type: ignore[attr-defined]
        f' data-platform="{escape(view.platform or "")}"',  # type: ignore[attr-defined]
        f' data-external-id="{escape(view.external_id or "")}"',  # type: ignore[attr-defined]
        f' data-submit-outcome="{escape(outcome)}"',
        f' data-confirmation="{escape(confirmation)}">',
    ]
    for field, text in view.fields.items():  # type: ignore[attr-defined]
        lines.append(f'<p data-field="{escape(field)}">{escape(text)}</p>')
    for control in view.controls:  # type: ignore[attr-defined]
        if control.secret or not control.name:
            continue
        lines.append(f'<input aria-label="{escape(control.name)}" value="{escape(control.value)}">')
    lines.append("<button>Создать</button><button>Create</button><button>Сохранить</button></body></html>")
    return "".join(lines)


def _count(session: Session, order_id: int, platform: str) -> int:
    submission_id = session.scalar(
        select(ListingSubmission.id).where(ListingSubmission.order_id == order_id, ListingSubmission.platform == platform)
    )
    if submission_id is None:
        return 0
    return _attempt_count(session, int(submission_id))
