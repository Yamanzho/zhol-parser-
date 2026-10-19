"""E7.1 listing plans. Forms are not submitted, and one platform does not cancel another."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.browser.cabinet.session import open_owner_cabinet, read_local_html
from zhol.cli import main
from zhol.db.models import (
    CardSnapshot,
    ChangeItem,
    ChangeLog,
    ChangePackage,
    ContentItem,
    Credential,
    HumanTask,
    Invoice,
    Job,
    Order,
)
from zhol.fulfillment.adapters import GoogleBusinessAdapter, TwoGisAdapter, YandexBusinessAdapter
from zhol.fulfillment.capabilities import cabinet_from_mapping, load_cabinet
from zhol.fulfillment.compare import build_field_plans, same_value
from zhol.fulfillment.content import ContentLibrary, FileRef
from zhol.fulfillment.engine import FulfillmentRefused, run_listing
from zhol.fulfillment.store import change_key
from zhol.fulfillment.types import ListingContext, PreparedField, PreparedListing
from zhol.orders.intake import _change_key, create_test_order
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "tests" / "fixtures" / "cabinet"


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _spec(**changes: object) -> dict:
    spec: dict = {
        "key": "listing-1",
        "company": {"id_2gis": "70000000000000001", "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": "2GIS",
                "action": "update",
                "external_id": "70000000000000001",
                "owner_authorization": "verified",
            }
        ],
        "messages": [{"id": "m1", "text": "Адрес: ул. Абая 10. Телефон +7 701 111 22 33. Часы 10:00-20:00."}],
        "facts": [
            {"field": "address", "value": "ул. Абая 10", "source_ref": "msg:m1", "approved": True},
            {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "approved": True},
            {"field": "hours", "value": "10:00-20:00", "source_ref": "msg:m1", "approved": True},
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
    }
    spec.update(changes)
    return spec


def _context(**changes: object) -> ListingContext:
    values: dict = {
        "order_id": 1,
        "company_id": 1,
        "company_name": "Salon Test",
        "platform": "2gis",
        "action": "update",
        "external_id": "70000000000000001",
        "public_url": None,
        "access_status": "verified",
        "claimed_authorization": "verified",
        "profile_status": None,
        "page_html": None,
        "snapshot_fields": {},
        "snapshot_match": "found",
        "screenshot_path": "shots/before.png",
    }
    values.update(changes)
    return ListingContext(**values)


def _snapshot(session: Session, company_id: int, platform: str = "2gis", **fields: object) -> None:
    body = {
        "name": "Other Name",
        "address": "другой адрес",
        "phones": ["+7 700 000 00 00"],
        "hours": "09:00-12:00",
        "services": [{"name": "Стрижка", "price": 8000}],
        "photos_count": 4,
    }
    body.update(fields)
    session.add(
        CardSnapshot(
            company_id=company_id,
            platform=platform,
            match_status="found",
            url=f"https://example.test/{platform}",
            fields=body,
            field_sources={},
            screenshot_path=f"shots/{platform}.png",
            purpose="order_fresh",
            collected_at=now(),
            collected_by="test",
            status="verified",
        )
    )
    session.flush()


def _items(session: Session) -> list[ChangeItem]:
    return list(session.scalars(select(ChangeItem).order_by(ChangeItem.id)))


def test_platform_capabilities_follow_the_dictionary() -> None:
    twogis = load_cabinet("2gis")
    yandex = load_cabinet("yandex")
    google = load_cabinet("google")
    assert twogis.submit is False and yandex.submit is False and google.submit is False
    assert "link" in twogis.fields and twogis.fields["link"].profile == "website"
    assert "services" in twogis.fields and "services" in yandex.fields
    assert "website" in google.fields
    for name in ("services", "whatsapp", "instagram", "link", "prices"):
        assert name not in google.fields
    assert "description" in google.fields and google.fields["description"].profile == "description"
    assert "description" not in google.read_only
    assert "description" in twogis.read_only and "prices" in twogis.read_only
    assert "owner_maintains" in yandex.read_only
    assert "owner_claimed" in google.read_only
    assert "prices" in TwoGisAdapter.forbidden
    assert "services" in GoogleBusinessAdapter.forbidden
    with pytest.raises(ValueError):
        cabinet_from_mapping("2gis", {"cabinet": {"submit": True}, "editable_fields": {}})


def test_saved_page_pauses_and_dry_run_does_not_submit() -> None:
    adapter = TwoGisAdapter()
    paused = adapter.validate_access(
        _context(page_html="<body data-page='cabinet' data-platform='2gis'>Подтвердите, что вы не робот</body>")
    )
    assert paused.status == "paused" and paused.reason == "captcha"
    sms = YandexBusinessAdapter().validate_access(
        _context(
            platform="yandex",
            external_id="yandex-1",
            page_html=PAGES.joinpath("yandex-sms.html").read_text(encoding="utf-8"),
        )
    )
    assert sms.status == "paused" and sms.reason == "sms"
    stopped = adapter.validate_access(_context(page_html="<html><body>Welcome</body></html>"))
    assert stopped.status == "stopped" and stopped.reason == "unexpected page"
    other = adapter.validate_access(
        _context(page_html="<body data-page='cabinet' data-platform='2gis' data-external-id='999'>Salon</body>")
    )
    assert other.status == "stopped"
    refused = adapter.validate_access(_context(access_status="invited", claimed_authorization="invited", page_html=None))
    assert refused.status == "refused"

    prepared = adapter.prepare_update(
        _context(),
        (PreparedField("hours", "text", "10:00-19:00", "10:00-20:00", "msg:m1"),),
    )
    assert prepared.submit is False
    applied = adapter.apply_changes(_context(), prepared)
    assert applied.result == "dry_run" and applied.submitted is False and applied.screenshot_after is None
    assert adapter.verify_changes(_context(), prepared, applied).ok is True
    blocked = PreparedListing("2gis", "update", "70000000000000001", (), (), True)
    with pytest.raises(PermissionError):
        adapter.apply_changes(_context(), blocked)
    with pytest.raises(PermissionError):
        open_owner_cabinet("https://2gis.kz/almaty/firm/1")
    with pytest.raises(PermissionError):
        read_local_html("https://business.google.com")


def test_google_drops_unsupported_fields_and_plans_do_not_copy_platforms() -> None:
    google = GoogleBusinessAdapter()
    prepared = google.prepare_update(
        _context(platform="google", external_id="g1"),
        (
            PreparedField("services", "services", None, [{"name": "Стрижка", "price": 8000}], "msg:m1"),
            PreparedField("hours", "text", None, "10:00-20:00", "msg:m1"),
            PreparedField("whatsapp", "text", None, "+77011112233", "msg:m1"),
        ),
    )
    assert [field.field for field in prepared.fields] == ["hours"]
    assert prepared.dropped == ("services", "whatsapp")
    assert prepared.submit is False

    profile = {
        "address": {"value": "ул. Абая 10", "source_ref": "msg:m1", "needs_confirmation": False},
        "phones": {"value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "needs_confirmation": False},
        "hours": {"value": "10:00-20:00", "source_ref": "msg:m1", "needs_confirmation": False},
        "services": [{"name": "Стрижка", "price": 8000, "source_ref": "msg:m1", "needs_confirmation": False}],
        "website": {"value": "https://salon.test", "source_ref": "msg:m1", "needs_confirmation": True},
    }
    plans = build_field_plans(
        platform="2gis",
        action="update",
        spec=load_cabinet("2gis"),
        profile=profile,
        company_name="Salon Test",
        files={"photo_real": (FileRef(1, "photo_real", "materials/hall.png"),)},
        library=ContentLibrary("not_available", (), ()),
        current={"hours": "10:00-19:00", "website": "https://copied.test", "photos_count": 0},
        consents=(),
    )
    by_field = {plan.field: plan for plan in plans}
    assert by_field["hours"].operation == "set"
    assert by_field["hours"].before == "10:00-19:00"
    assert "website" not in by_field
    assert by_field["link"].operation == "waiting_approval"
    assert by_field["services"].operation == "set"
    assert by_field["address"].operation == "waiting_nap_consent"
    assert by_field["photos"].operation == "set"
    assert by_field["photos"].before == 0
    google_plans = build_field_plans(
        platform="google",
        action="update",
        spec=load_cabinet("google"),
        profile=profile,
        company_name="Salon Test",
        files={},
        library=ContentLibrary("not_available", (), ()),
        current={"name": "Salon Test"},
        consents=(("google", "address", "ул. Абая 10", "msg:yes"),),
    )
    google_fields = {plan.field: plan for plan in google_plans}
    assert google_fields["services"].operation == "unsupported"
    assert google_fields["address"].operation == "set"
    assert google_fields["address"].nap_consent_ref == "msg:yes"
    assert same_value("phones", "+7 701 111 22 33", ["+77011112233"]) is True
    assert change_key(4, "2gis", "hours", "10:00-20:00") == _change_key(4, "2gis", "hours", "10:00-20:00")


def test_listing_modules_do_not_automate_secrets() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in (ROOT / "zhol" / "fulfillment", ROOT / "zhol" / "browser" / "cabinet")
        for path in folder.rglob("*.py")
    )
    for banned in ("sync_playwright", "page.fill", "ciphertext", "add_init_script"):
        assert banned not in text


def test_dry_run_uses_the_saved_page_and_rerun_is_idempotent(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec())
    assert order.company_id is not None
    _snapshot(session, order.company_id)
    session.add(
        CardSnapshot(
            company_id=order.company_id,
            platform="google",
            match_status="found",
            url="https://maps.example/copied",
            fields={"website": "https://copied.example", "name": "Copied"},
            field_sources={},
            purpose="order_fresh",
            collected_at=now(),
            collected_by="test",
            status="verified",
        )
    )
    session.add(
        Credential(
            company_id=order.company_id,
            platform="2gis",
            login_hint="o***",
            ciphertext=b"sealed-secret-e71",
            created_at=now(),
        )
    )
    session.flush()
    before_fields = dict(session.scalars(select(CardSnapshot).where(CardSnapshot.platform == "2gis")).one().fields)
    first = run_listing(
        session,
        order.id,
        pages={"2gis": str(PAGES / "2gis.html")},
        evidence_dir=tmp_path,
    )
    assert first.content == "not_available"
    assert first.ok is True
    outcome = first.outcomes[0]
    assert outcome.platform == "2gis" and outcome.status == "dry_run"
    hours = next(plan for plan in outcome.plans if plan.field == "hours")
    assert hours.operation == "set" and hours.before == "10:00-19:00" and hours.after == "10:00-20:00"
    assert next(plan for plan in outcome.plans if plan.field == "name").operation == "already"
    assert all(plan.field != "link" for plan in outcome.plans if plan.operation == "set")
    assert session.get(Order, order.id).stage == "created"  # type: ignore[union-attr]
    assert session.scalars(select(CardSnapshot).where(CardSnapshot.platform == "2gis")).one().fields == before_fields
    rows = _items(session)
    assert [row.field for row in rows] == ["hours"]
    assert rows[0].status == "draft"
    log = session.scalars(select(ChangeLog)).one()
    assert log.result == "dry_run" and log.screenshot_after is None and log.screenshot_before == "shots/2gis.png"
    assert log.details is not None and log.details["submitted"] is False
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["submitted"] is False
    assert b"sealed-secret-e71" not in json.dumps(plan).encode()
    assert session.scalars(select(Credential)).one().ciphertext == b"sealed-secret-e71"
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert session.scalar(select(func.count()).select_from(Invoice)) == 0
    assert session.scalar(select(func.count()).select_from(HumanTask)) == 0

    second = run_listing(session, order.id, pages={"2gis": str(PAGES / "2gis.html")})
    assert second.outcomes[0].status == "already"
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 1
    assert session.scalar(select(func.count()).select_from(ChangeItem)) == 1
    assert session.scalar(select(func.count()).select_from(ChangePackage)) == 1


def test_missing_facts_refuse_without_a_log(session: Session) -> None:
    thin = _spec(key="thin")
    thin["facts"] = [fact for fact in thin["facts"] if fact["field"] != "phones"]
    thin["messages"] = [{"id": "m-thin", "text": "Адрес: ул. Абая 10."}]
    for fact in thin["facts"]:
        fact["source_ref"] = "msg:m-thin"
    order = create_test_order(session, thin)
    with pytest.raises(FulfillmentRefused, match="missing phones"):
        run_listing(session, order.id)
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0


def test_one_platform_can_fail_while_another_is_planned(session: Session) -> None:
    spec = _spec(key="partial")
    spec["platforms"] = [
        {
            "platform": "2GIS",
            "action": "update",
            "external_id": "70000000000000001",
            "owner_authorization": "verified",
        },
        {
            "platform": "Google Maps",
            "action": "update",
            "external_id": "google-1",
            "owner_authorization": "verified",
        },
        {
            "platform": "Yandex Maps",
            "action": "update",
            "external_id": "yandex-1",
            "owner_authorization": "verified",
        },
    ]
    order = create_test_order(session, spec)
    assert order.company_id is not None
    _snapshot(session, order.company_id)
    _snapshot(session, order.company_id, "yandex", hours="09:00-12:00", name="Salon Test", address="ул. Абая 10")
    pages = {"google": str(PAGES / "google-other.html"), "yandex": str(PAGES / "yandex-sms.html")}
    report = run_listing(session, order.id, pages=pages)
    by_platform = {item.platform: item for item in report.outcomes}
    assert by_platform["2gis"].status == "dry_run"
    assert by_platform["google"].status == "stopped"
    assert by_platform["google"].detail == "organization does not match the order"
    assert by_platform["yandex"].status == "paused"
    assert report.ok is False
    assert [row.platform for row in _items(session)] == ["2gis"]
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 1
    task = session.scalars(select(HumanTask)).one()
    assert task.details is not None and task.details["pause"] == "sms" and task.details["submitted"] is False
    run_listing(session, order.id, pages=pages)
    assert session.scalar(select(func.count()).select_from(HumanTask)) == 1


def test_adapter_exception_does_not_erase_another_platform(session: Session) -> None:
    spec = _spec(key="boom")
    spec["platforms"] = [
        spec["platforms"][0],
        {
            "platform": "Yandex Maps",
            "action": "update",
            "external_id": "yandex-1",
            "owner_authorization": "verified",
        },
    ]
    order = create_test_order(session, spec)
    assert order.company_id is not None
    _snapshot(session, order.company_id)
    _snapshot(
        session,
        order.company_id,
        "yandex",
        name="Salon Test",
        address="ул. Абая 10",
        phones=["+7 701 111 22 33"],
        hours="09:00-12:00",
    )

    class Boom(YandexBusinessAdapter):
        def apply_changes(self, ctx, prepared):  # type: ignore[no-untyped-def]
            raise RuntimeError("cabinet broke")

    report = run_listing(session, order.id, adapters={"yandex": Boom()})
    by_platform = {item.platform: item for item in report.outcomes}
    assert by_platform["2gis"].status == "dry_run"
    assert by_platform["yandex"].status == "failed"
    assert [row.platform for row in _items(session)] == ["2gis"]
    assert session.get(Order, order.id).stage == "created"  # type: ignore[union-attr]


def test_approved_content_is_used_and_unfinished_content_is_not(session: Session) -> None:
    order = create_test_order(session, _spec(key="content"))
    assert order.company_id is not None
    _snapshot(session, order.company_id, name="Salon Test", address="ул. Абая 10", phones=["+7 701 111 22 33"])
    session.add_all(
        [
            ContentItem(
                order_id=order.id,
                kind="cover",
                platform="2gis",
                brief={"illustrative": True},
                result_path="generated/cover.png",
                status="checked",
            ),
            ContentItem(
                order_id=order.id,
                kind="video",
                brief={"illustrative": True},
                result_path="generated/video.mp4",
                status="done",
            ),
            ContentItem(
                order_id=order.id,
                kind="interior",
                brief={"illustrative": True},
                result_path="generated/room.png",
                status="checked",
            ),
        ]
    )
    session.flush()
    report = run_listing(session, order.id)
    assert report.content == "incomplete"
    assert report.ok is True
    fields = {row.field for row in _items(session)}
    assert fields == {"hours", "cover"}
    cover = next(row for row in _items(session) if row.field == "cover")
    assert cover.after["path"] == "generated/cover.png"
    assert cover.after["illustrative"] is True
    dumped = json.dumps([row.after for row in _items(session)], ensure_ascii=False)
    assert "room.png" not in dumped and "video.mp4" not in dumped
    video = next(plan for plan in report.outcomes[0].plans if plan.field == "video")
    assert video.operation == "content_unavailable"
    run_listing(session, order.id)
    assert session.scalar(select(func.count()).select_from(ChangeItem)) == 2
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 2


def test_unapproved_fact_is_not_written(session: Session) -> None:
    spec = _spec(key="draft-hours")
    for fact in spec["facts"]:
        if fact["field"] == "hours":
            fact["approved"] = False
    order = create_test_order(session, spec)
    assert order.company_id is not None
    _snapshot(session, order.company_id, name="Salon Test", address="ул. Абая 10", phones=["+7 701 111 22 33"])
    report = run_listing(session, order.id)
    assert report.ok is False
    assert report.outcomes[0].status == "held"
    assert next(plan.operation for plan in report.outcomes[0].plans if plan.field == "hours") == "waiting_approval"
    assert _items(session) == []


def test_cli_dry_run_listing(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    path = tmp_path / "order.json"
    path.write_text(json.dumps(_spec(key="cli-listing")), encoding="utf-8")
    assert main(["create-test-order", "--spec", str(path)], factory=factory) == 0
    order_id = capsys.readouterr().out.strip().split()[1]
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "2gis.html").write_text((PAGES / "2gis.html").read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["dry-run-listing", "--order-id", order_id, "--pages", str(pages), "--evidence", str(tmp_path / "out")], factory=factory) == 0
    printed = capsys.readouterr().out
    assert "mode dry_run" in printed
    assert "content not_available" in printed
    assert "platform 2gis update dry_run" in printed
    assert "hours set" in printed
    assert (tmp_path / "out" / "plan.json").is_file()


def test_all_permissions_missing_raises(session: Session) -> None:
    order = create_test_order(session, _spec(key="no-owner"))
    from zhol.db.models import CompanyPlatformId

    assert order.company_id is not None
    stored = session.get(CompanyPlatformId, (order.company_id, "2gis"))
    assert stored is not None
    stored.access_status = "invited"
    session.flush()
    with pytest.raises(FulfillmentRefused, match="owner permission"):
        run_listing(session, order.id)
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0
