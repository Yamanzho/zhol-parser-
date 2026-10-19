"""E7.6 prepares a new listing draft and does not submit it."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from zhol.browser.cabinet.editor import CREATE_SUBMIT, load_create_form
from zhol.browser.google.create import prepare_google_create
from zhol.browser.google.page import FixtureCabinet as GoogleCabinet
from zhol.browser.twogis.create import prepare_2gis_create
from zhol.browser.twogis.page import FixtureCabinet as TwoGisCabinet
from zhol.browser.twogis.prepare import prepare_2gis_listing
from zhol.browser.yandex.create import prepare_yandex_create
from zhol.browser.yandex.page import FixtureCabinet as YandexCabinet
from zhol.cli import main
from zhol.db.models import CardSnapshot, ChangeItem, ChangeLog, CompanyPlatformId, HumanTask, Intake
from zhol.fulfillment.engine import FulfillmentRefused
from zhol.fulfillment.existence import AbsenceConfirmation, SnapshotEvidence, classify_existence
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "tests" / "fixtures" / "cabinet"
PLATFORMS = ("2gis", "yandex", "google")
PREPARED = {
    "2gis": ("name", "address", "phones", "category", "hours", "link", "services"),
    "yandex": ("name", "address", "phones", "category", "hours", "website", "services", "description"),
    "google": ("name", "address", "phones", "category", "hours", "website", "description"),
}
COMMANDS = {
    "2gis": prepare_2gis_create,
    "yandex": prepare_yandex_create,
    "google": prepare_google_create,
}
CLI = {
    "2gis": "prepare-2gis-create",
    "yandex": "prepare-yandex-create",
    "google": "prepare-google-create",
}
CABINETS = {"2gis": TwoGisCabinet, "yandex": YandexCabinet, "google": GoogleCabinet}
CLOSED = {
    "2gis": "live 2GIS cabinet is closed",
    "yandex": "live Yandex Business cabinet is closed",
    "google": "live Google Business Profile cabinet is closed",
}


@pytest.fixture
def session(migrated_engine: Engine):
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def test_create_forms_follow_each_platform() -> None:
    twogis = load_create_form("2gis")
    yandex = load_create_form("yandex")
    google = load_create_form("google")
    assert "description" not in twogis.editor.fields
    assert "services" not in google.editor.fields
    assert {"name", "address", "phones", "category", "hours", "services"} <= set(yandex.editor.fields)
    for form in (twogis, yandex, google):
        assert set(CREATE_SUBMIT) <= set(form.editor.submit_names)
        assert set(form.editor.submit_names).isdisjoint({item.name for item in form.editor.fields.values()})


def test_absence_rules() -> None:
    current = now()
    fresh = _evidence("not_found", current, status="verified", purpose="order_fresh", collected_by="scout")
    audit = _evidence("not_found", current, status="collected", purpose="prospect", collected_by="bek_audit")
    stale = _evidence("not_found", current - timedelta(days=40), status="verified", purpose="order_fresh", collected_by="scout")
    found = _evidence("found", current, status="verified", purpose="order_fresh", collected_by="scout", url="https://maps.example/1")
    other = _evidence("other_address", current, status="verified", purpose="order_fresh", collected_by="scout")
    yes = AbsenceConfirmation("human:absence", current)
    assert classify_existence(external_id="1", public_url=None, snapshots=(), confirmation=yes, now=current).status == "existing_listing"
    assert classify_existence(external_id=None, public_url="https://maps.example/1", snapshots=(fresh,), confirmation=yes, now=current).status == "existing_listing"
    assert classify_existence(external_id=None, public_url=None, snapshots=(found, fresh), confirmation=yes, now=current).reason == "a stored listing was found"
    assert classify_existence(external_id=None, public_url=None, snapshots=(other,), confirmation=yes, now=current).reason == "the stored address does not match"
    assert classify_existence(external_id=None, public_url=None, snapshots=(audit,), confirmation=yes, now=current).reason == "audit not_found is unverified"
    assert classify_existence(external_id=None, public_url=None, snapshots=(stale,), confirmation=yes, now=current).reason == "listing absence is not fresh"
    assert classify_existence(external_id=None, public_url=None, snapshots=(fresh,), confirmation=None, now=current).reason == "missing human confirmation"
    assert classify_existence(external_id=None, public_url=None, snapshots=(fresh,), confirmation=yes, now=current).status == "missing_confirmed"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_confirmed_absence_prepares_a_draft(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-ok-{platform}")
    view = _view(platform)
    report = _prepare(session, platform, order.id, view, tmp_path)
    assert report.status == "prepared" and report.mode == "create_prepare"
    assert report.submitted is False and report.published is False and report.approval_required is True
    assert view.clicked() == ()
    assert {item.field for item in report.changes if item.action == "prepared"} == set(PREPARED[platform])
    assert view.value("Название") == "Salon Test"
    assert view.value("Адрес") == "ул. Абая 10"
    assert view.value("Телефон") == "+7 701 111 22 33"
    assert view.value("Часы работы") == "10:00-20:00"
    assert "Создать" not in {name for name, _value in view.writes}
    assert "Create" not in {name for name, _value in view.writes}
    if platform != "google":
        assert view.value("Услуги и цены") == "Стрижка: 8000"
        assert view.value("Instagram") == ""
    if platform != "2gis":
        assert view.value("Описание") == "Салон на Абая"
    items = _items(session, platform)
    assert {item.field for item in items} == set(PREPARED[platform])
    assert all(item.source_ref["ref"] and item.status == "draft" for item in items)
    logs = _logs(session, platform)
    assert len(logs) == len(PREPARED[platform])
    assert {log.result for log in logs} == {"prepared"}
    assert all(log.details["submitted"] is False and log.details["published"] is False for log in logs)
    assert (tmp_path / "before.png").read_bytes().startswith(b"\x89PNG")
    assert (tmp_path / "prepared.png").read_bytes().startswith(b"\x89PNG")
    saved = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "create_prepare" and saved["submitted"] is False and saved["published"] is False
    task = session.scalar(select(HumanTask).where(HumanTask.reason == f"{platform}_create_review"))
    assert task is not None and task.status == "open"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_existing_listing_is_not_prepared(session: Session, tmp_path: Path, platform: str) -> None:
    stored = _ready(session, platform, f"e76-id-{platform}", confirm=False)
    assert stored.company_id is not None
    row = session.get(CompanyPlatformId, (stored.company_id, platform))
    assert row is not None
    row.external_id = "already-1"
    refused = _prepare(session, platform, stored.id, _closed(), tmp_path / "stored")
    assert refused.status == "existing_listing" and refused.detail == "known platform id or listing url"
    assert _logs(session, platform) == []

    page = _ready(session, platform, f"e76-page-{platform}", message_id=f"m-page-{platform}")
    html = _html(platform).replace('data-external-id=""', 'data-external-id="page-1"', 1)
    opened = _view(platform, html)
    report = _prepare(session, platform, page.id, opened, tmp_path / "page")
    assert report.status == "existing_listing" and report.detail == "the open page already has a listing id"
    assert opened.writes == [] and opened.clicked() == ()
    assert _logs(session, platform) == []
    assert session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.reason == "listing_duplicate")) == 2


@pytest.mark.parametrize("platform", PLATFORMS)
def test_uncertain_match_is_escalated(session: Session, tmp_path: Path, platform: str) -> None:
    audit = _ready(session, platform, f"e76-audit-{platform}", purpose="prospect", collected_by="bek_audit", status="collected")
    audit_report = _prepare(session, platform, audit.id, _closed(), tmp_path / "audit")
    assert audit_report.status == "uncertain" and audit_report.detail == "audit not_found is unverified"

    other = _ready(
        session,
        platform,
        f"e76-other-{platform}",
        message_id=f"m-other-{platform}",
        company_id=f"700000000000002{platform[:1]}",
        match_status="other_address",
    )
    other_report = _prepare(session, platform, other.id, _closed(), tmp_path / "other")
    assert other_report.status == "uncertain" and other_report.detail == "the stored address does not match"

    vague = _ready(
        session,
        platform,
        f"e76-vague-{platform}",
        message_id=f"m-vague-{platform}",
        company_id=f"700000000000003{platform[:1]}",
    )
    html = _html(platform).replace("карточка не найдена", "проверьте вручную", 1)
    opened = _view(platform, html)
    vague_report = _prepare(session, platform, vague.id, opened, tmp_path / "vague")
    assert vague_report.status == "uncertain" and vague_report.detail == "the page does not confirm that the listing is absent"
    assert opened.writes == [] and _logs(session, platform) == []
    assert session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.reason == "listing_absence_uncertain")) == 3


@pytest.mark.parametrize("platform", PLATFORMS)
def test_missing_human_confirmation(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-confirm-{platform}", confirm=False)
    report = _prepare(session, platform, order.id, _closed(), tmp_path)
    assert report.status == "uncertain" and report.detail == "missing human confirmation"
    assert _logs(session, platform) == []
    task = session.scalar(select(HumanTask).where(HumanTask.reason == "listing_absence_uncertain"))
    assert task is not None and task.details["submitted"] is False


@pytest.mark.parametrize("platform", PLATFORMS)
def test_missing_owner_authorization(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-owner-{platform}", authorization="invited", confirm=False)
    report = _prepare(session, platform, order.id, _closed(), tmp_path / "stored")
    assert report.status == "ownership_required" and report.detail == "missing owner permission"
    assert _logs(session, platform) == []

    page = _ready(session, platform, f"e76-role-{platform}", message_id=f"m-role-{platform}", company_id=f"700000000000004{platform[:1]}")
    html = _html(platform).replace("Владелец", "Наблюдатель", 1)
    opened = _view(platform, html)
    role = _prepare(session, platform, page.id, opened, tmp_path / "role")
    assert role.status == "ownership_required" and role.detail == "missing owner permission"
    assert opened.writes == [] and opened.clicked() == ()
    assert session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.reason == "listing_ownership_required")) == 2


@pytest.mark.parametrize("platform", PLATFORMS)
def test_unsupported_fields_are_not_written(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-skip-{platform}")
    view = _view(platform)
    report = _prepare(session, platform, order.id, view, tmp_path)
    skipped = {item.field for item in report.changes if item.action == "not_filled"}
    written = {item.field for item in _items(session, platform)}
    assert "logo" in skipped and "photos" in skipped
    assert "logo" not in written and "photos" not in written
    if platform == "2gis":
        assert "description" in skipped and "description" not in written
    if platform == "google":
        assert "services" in skipped and "services" not in written
        assert view.value("Описание") == "Салон на Абая"
    assert view.clicked() == ()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_required_information_missing(session: Session, tmp_path: Path, platform: str) -> None:
    category = _ready(session, platform, f"e76-category-{platform}", drop=("category",))
    view = _view(platform)
    report = _prepare(session, platform, category.id, view, tmp_path / "category")
    assert report.status == "refused" and report.detail == "missing category"
    assert view.writes == [] and _logs(session, platform) == []

    hours = _ready(
        session,
        platform,
        f"e76-hours-{platform}",
        message_id=f"m-hours-{platform}",
        company_id=f"700000000000005{platform[:1]}",
        drop=("hours",),
    )
    hours_view = _view(platform)
    hours_report = _prepare(session, platform, hours.id, hours_view, tmp_path / "hours")
    assert hours_report.status == "refused" and hours_report.detail == "missing hours"
    assert hours_view.writes == []

    if platform != "google":
        services = _ready(
            session,
            platform,
            f"e76-services-{platform}",
            message_id=f"m-services-{platform}",
            company_id=f"700000000000006{platform[:1]}",
            drop=("services",),
        )
        services_view = _view(platform)
        services_report = _prepare(session, platform, services.id, services_view, tmp_path / "services")
        assert services_report.status == "refused" and services_report.detail == "missing services"
        assert services_view.writes == []

    phones = _spec(platform, f"e76-phones-{platform}", company_id=f"700000000000007{platform[:1]}", message_id=f"m-phones-{platform}")
    phones["facts"] = [fact for fact in phones["facts"] if fact["field"] != "phones"]
    from zhol.orders.intake import create_test_order

    phone_order = create_test_order(session, phones)
    with pytest.raises(FulfillmentRefused, match="missing phones"):
        _prepare(session, platform, phone_order.id, _closed(), tmp_path / "phones")


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    ("kind", "phrase", "store_page"),
    [("login", "Войдите в аккаунт", False), ("sms", "код из СМС", False), ("captcha", "не робот", True)],
)
def test_login_sms_and_captcha_pause(
    session: Session, tmp_path: Path, platform: str, kind: str, phrase: str, store_page: bool
) -> None:
    order = _ready(session, platform, f"e76-{kind}-{platform}", message_id=f"m-{kind}-{platform}")
    html = (
        f'<html><body data-page="login" data-platform="{platform}"><p>{phrase}</p>'
        '<input type="password" aria-label="Пароль" value="secret-value"></body></html>'
    )
    view = _view(platform, html)
    report = _prepare(session, platform, order.id, view, tmp_path)
    assert report.status == "paused" and report.detail == kind
    assert view.writes == [] and view.clicked() == () and _logs(session, platform) == []
    assert (tmp_path / "page.html").exists() is store_page
    blob = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in tmp_path.rglob("*") if path.is_file())
    assert "secret-value" not in blob
    task = session.scalar(select(HumanTask).where(HumanTask.reason == "listing_paused"))
    assert task is not None and task.details["pause"] == kind and task.details["submitted"] is False


@pytest.mark.parametrize("platform", PLATFORMS)
def test_prepare_does_not_submit(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-submit-{platform}")
    view = _view(platform)
    with pytest.raises(PermissionError):
        view.write_control("textbox", "Создать", "Salon Test")
    report = _prepare(session, platform, order.id, view, tmp_path)
    assert report.submitted is False and report.published is False and view.clicked() == ()
    assert all(log.details["submitted"] is False and log.screenshot_after.endswith("prepared.png") for log in _logs(session, platform))
    with pytest.raises(PermissionError):
        _prepare(session, platform, order.id, _closed(), tmp_path / "approve", approve=True)
    text = "\n".join(
        (ROOT / "zhol" / "browser" / name).read_text(encoding="utf-8")
        for name in ("cabinet/create.py", "twogis/create.py", "yandex/create.py", "google/create.py")
    )
    assert ".click(" not in text and "page.fill" not in text


@pytest.mark.parametrize("platform", PLATFORMS)
def test_rerun_is_idempotent(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-again-{platform}")
    first = _prepare(session, platform, order.id, _view(platform), tmp_path)
    assert first.status == "prepared"
    count = len(_logs(session, platform))
    tasks = session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.reason == f"{platform}_create_review"))
    second_view = _view(platform)
    second = _prepare(session, platform, order.id, second_view, tmp_path / "again")
    assert second.status == "already" and second.approval_required is False
    assert second_view.writes == [] and second_view.clicked() == ()
    assert len(_logs(session, platform)) == count
    assert session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.reason == f"{platform}_create_review")) == tasks


def test_evidence_and_platforms_stay_isolated(session: Session, tmp_path: Path) -> None:
    spec = _spec("2gis", "e76-iso", company_id="70000000000000801", message_id="m-iso")
    spec["platforms"].extend(
        [
            {"platform": "yandex", "action": "create", "owner_authorization": "verified"},
            {"platform": "google", "action": "create", "owner_authorization": "verified"},
        ]
    )
    from zhol.orders.intake import create_test_order

    order = create_test_order(session, spec)
    _confirm(session, order.id, "2gis")
    _confirm(session, order.id, "yandex")
    _confirm(session, order.id, "google")
    assert order.company_id is not None
    _search(session, order.company_id, "2gis")
    _search(session, order.company_id, "yandex")
    _search(session, order.company_id, "google", match_status="found", url="https://maps.example/google")
    twogis = _prepare(session, "2gis", order.id, _view("2gis"), tmp_path / "2gis")
    assert twogis.status == "prepared"
    paused = _prepare(
        session,
        "yandex",
        order.id,
        _view("yandex", '<html><body data-page="login" data-platform="yandex"><p>Войдите в аккаунт</p></body></html>'),
        tmp_path / "yandex",
    )
    google = _prepare(session, "google", order.id, _closed(), tmp_path / "google")
    wrong = _view("2gis", _html("google"))
    stopped = _prepare(session, "2gis", order.id, wrong, tmp_path / "wrong")
    assert paused.status == "paused" and google.status == "existing_listing" and stopped.status == "stopped"
    assert {item.platform for item in session.scalars(select(ChangeItem))} == {"2gis"}
    assert len(_logs(session, "2gis")) == len(PREPARED["2gis"])
    assert _logs(session, "yandex") == [] and _logs(session, "google") == []
    assert (tmp_path / "2gis" / "review.json").is_file()
    assert (tmp_path / "2gis" / "before.png").is_file() and (tmp_path / "2gis" / "prepared.png").is_file()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_cli_prepare_create(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with factory() as db, db.begin():
        order = _ready(db, platform, f"e76-cli-{platform}")
        order_id = order.id
    evidence = tmp_path / "out"
    assert main([CLI[platform], "--order-id", str(order_id), "--page", str(PAGES / f"{platform}-create.html"), "--evidence", str(evidence)], factory=factory) == 0
    printed = capsys.readouterr().out
    assert "mode create_prepare" in printed and f"platform {platform}" in printed
    assert "submitted false" in printed and "published false" in printed and "approval required" in printed
    review = json.loads((evidence / "review.json").read_text(encoding="utf-8"))
    assert review["submitted"] is False and review["published"] is False


@pytest.mark.parametrize("platform", PLATFORMS)
def test_live_create_stays_closed(session: Session, tmp_path: Path, platform: str) -> None:
    order = _ready(session, platform, f"e76-live-{platform}")
    with pytest.raises(PermissionError, match=CLOSED[platform]):
        _prepare(session, platform, order.id, "https://cabinet.example/create", tmp_path)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_unapproved_required_fact_is_held(session: Session, tmp_path: Path, platform: str) -> None:
    spec = _spec(platform, f"e76-held-{platform}")
    for fact in spec["facts"]:
        if fact["field"] == "category":
            fact["approved"] = False
    from zhol.orders.intake import create_test_order

    order = create_test_order(session, spec)
    _confirm(session, order.id, platform)
    assert order.company_id is not None
    _search(session, order.company_id, platform)
    view = _view(platform)
    report = _prepare(session, platform, order.id, view, tmp_path)
    assert report.status == "held" and report.detail == "the client fact is not approved"
    assert view.writes == [] and view.clicked() == () and _logs(session, platform) == []


def test_update_command_still_does_not_create(session: Session, tmp_path: Path) -> None:
    from zhol.orders.intake import create_test_order

    order = create_test_order(session, _spec("2gis", "e76-old-create"))
    report = prepare_2gis_listing(session, order.id, page=_closed(), evidence_dir=tmp_path)
    assert report.status == "stopped" and report.detail == "2GIS prepare edits an existing organization"
    assert report.mode == "cabinet_prepare"
    assert _logs(session, "2gis") == []


def test_unknown_control_and_wrong_organization_stop(session: Session, tmp_path: Path) -> None:
    unknown_order = _ready(session, "yandex", "e76-unknown")
    html = _html("yandex").replace(
        "<button",
        '<label>Филиал <input aria-label="Филиал" value=""></label><button',
        1,
    )
    unknown = _view("yandex", html)
    unknown_report = _prepare(session, "yandex", unknown_order.id, unknown, tmp_path / "unknown")
    assert unknown_report.status == "stopped" and unknown_report.detail == "stopped_unknown_ui"
    assert unknown.writes == [] and unknown.clicked() == ()

    wrong_order = _ready(session, "google", "e76-wrong", message_id="m-wrong", company_id="70000000000000901")
    wrong_html = _html("google").replace('data-field="organization"></h1>', 'data-field="organization">Чужой салон</h1>', 1)
    wrong = _view("google", wrong_html)
    wrong_report = _prepare(session, "google", wrong_order.id, wrong, tmp_path / "wrong")
    assert wrong_report.status == "stopped" and wrong_report.detail == "organization does not match the order"
    assert wrong.writes == [] and _logs(session, "google") == [] and _logs(session, "yandex") == []


def _evidence(
    match_status: str,
    collected_at,
    *,
    status: str,
    purpose: str,
    collected_by: str,
    url: str | None = None,
) -> SnapshotEvidence:
    return SnapshotEvidence(match_status, url, collected_at, status, purpose, collected_by)


def _spec(platform: str, key: str, **changes: object) -> dict:
    company_id = str(changes.pop("company_id", key))
    message_id = str(changes.pop("message_id", "m1"))
    authorization = str(changes.pop("authorization", "verified"))
    drop = set(changes.pop("drop", ()))  # type: ignore[arg-type]
    facts = [
        {"field": "address", "value": "ул. Абая 10", "source_ref": f"msg:{message_id}", "approved": True},
        {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": f"msg:{message_id}", "approved": True},
        {"field": "hours", "value": "10:00-20:00", "source_ref": f"msg:{message_id}", "approved": True},
        {"field": "website", "value": "https://salon.example", "source_ref": f"msg:{message_id}", "approved": True},
        {"field": "description", "value": "Салон на Абая", "source_ref": f"msg:{message_id}", "approved": True},
        {"field": "category", "value": "Парикмахерская", "source_ref": f"msg:{message_id}", "approved": True},
        {
            "field": "services",
            "value": [{"name": "Стрижка", "price": 8000}],
            "source_ref": f"msg:{message_id}",
            "approved": True,
        },
    ]
    facts = [fact for fact in facts if fact["field"] not in drop]
    return {
        "key": key,
        "company": {"id_2gis": company_id, "name": "Salon Test", "city": "Алматы"},
        "platforms": [{"platform": platform, "action": "create", "owner_authorization": authorization}],
        "messages": [{"id": message_id, "text": "Салон на Абая. Сайт, телефон, часы, рубрика и стрижка."}],
        "facts": facts,
        "materials": [
            {"kind": "logo", "path": "materials/logo.png", "ref": "logo"},
            {"kind": "photo_real", "path": "materials/hall.png", "ref": "hall"},
        ],
        "not_applicable": ["videos"],
        **changes,
    }


def _ready(session: Session, platform: str, key: str, **changes: object):
    from zhol.orders.intake import create_test_order

    confirm = bool(changes.pop("confirm", True))
    match_status = str(changes.pop("match_status", "not_found"))
    purpose = str(changes.pop("purpose", "order_fresh"))
    collected_by = str(changes.pop("collected_by", "scout"))
    status = str(changes.pop("status", "verified"))
    url = changes.pop("url", None)
    order = create_test_order(session, _spec(platform, key, **changes))
    if confirm:
        _confirm(session, order.id, platform)
    assert order.company_id is not None
    _search(
        session,
        order.company_id,
        platform,
        match_status=match_status,
        purpose=purpose,
        collected_by=collected_by,
        status=status,
        url=url if isinstance(url, str) or url is None else None,
    )
    return order


def _confirm(session: Session, order_id: int, platform: str) -> None:
    intake = session.get(Intake, order_id)
    assert intake is not None
    intake.checklist["platforms"][platform]["absence_confirmation"] = {"ref": "human:absence", "at": now().isoformat()}
    flag_modified(intake, "checklist")


def _search(
    session: Session,
    company_id: int,
    platform: str,
    *,
    match_status: str = "not_found",
    purpose: str = "order_fresh",
    collected_by: str = "scout",
    status: str = "verified",
    url: str | None = None,
) -> None:
    session.add(
        CardSnapshot(
            company_id=company_id,
            platform=platform,
            match_status=match_status,
            url=url,
            fields={},
            field_sources={},
            purpose=purpose,
            collected_at=now(),
            collected_by=collected_by,
            status=status,
        )
    )
    session.flush()


def _html(platform: str) -> str:
    return (PAGES / f"{platform}-create.html").read_text(encoding="utf-8")


def _view(platform: str, html: str | None = None):
    from zhol.browser.config import load_platforms

    return CABINETS[platform](html if html is not None else _html(platform), load_platforms()["markers"])


def _prepare(session: Session, platform: str, order_id: int, page: object, folder: Path, **kwargs: object):
    return COMMANDS[platform](session, order_id, page=page, evidence_dir=folder, **kwargs)


def _items(session: Session, platform: str) -> list[ChangeItem]:
    return list(session.scalars(select(ChangeItem).where(ChangeItem.platform == platform)))


def _logs(session: Session, platform: str) -> list[ChangeLog]:
    return list(
        session.scalars(
            select(ChangeLog)
            .join(ChangeItem, ChangeLog.change_item_id == ChangeItem.id)
            .where(ChangeItem.platform == platform, ChangeLog.action == "prepare")
        )
    )


class _Closed:
    def body_text(self) -> str:
        raise AssertionError("the create form was opened")

    def write_control(self, *_args: object) -> None:
        raise AssertionError("the create form was opened")


def _closed() -> _Closed:
    return _Closed()
