"""E7.3 and E7.4 prepare 2GIS and Google cabinets and do not submit them."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.browser.config import load_platforms
from zhol.browser.google.page import FixtureCabinet as GoogleCabinet
from zhol.browser.google.prepare import prepare_google_listing
from zhol.browser.google.session import launch_blank as google_launch
from zhol.browser.google.session import open_interactive as google_open
from zhol.browser.google.spec import load_editor as load_google_editor
from zhol.browser.twogis.page import FixtureCabinet as TwoGisCabinet
from zhol.browser.twogis.prepare import prepare_2gis_listing
from zhol.browser.twogis.session import launch_blank as twogis_launch
from zhol.browser.twogis.session import open_interactive as twogis_open
from zhol.browser.twogis.spec import load_editor as load_twogis_editor
from zhol.cli import main
from zhol.db.models import BrowserProfile, ChangeItem, ChangeLog, ContentItem, Credential, HumanTask, Order
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter
from zhol.fulfillment.adapters.twogis import TwoGisAdapter
from zhol.fulfillment.capabilities import load_cabinet
from zhol.fulfillment.types import ListingContext, PreparedField
from zhol.orders.intake import create_test_order
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "tests" / "fixtures" / "cabinet"
MARKERS = load_platforms()["markers"]
HOURS = '<label>Часы работы <input aria-label="Часы работы" data-field="hours" value="10:00-19:00"></label>'
SUBMIT = {"Сохранить", "Опубликовать", "Отправить", "Save", "Publish", "Submit"}


class Case:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


CASES = {
    "2gis": Case(
        prepare=prepare_2gis_listing,
        cabinet=TwoGisCabinet,
        page_name="2gis-editor.html",
        external_id="70000000000000081",
        company_id="70000000000000081",
        command="prepare-2gis-listing",
        env="ZHOL_2GIS_CABINET",
        review="2gis_listing_review",
        closed="live 2GIS cabinet is closed",
        open_interactive=twogis_open,
        launch_blank=twogis_launch,
        url="https://account.2gis.com/org/1",
        prepared=("phones", "category", "hours", "link", "services"),
        category_label="Рубрика",
        create_detail="2GIS prepare edits an existing organization",
    ),
    "google": Case(
        prepare=prepare_google_listing,
        cabinet=GoogleCabinet,
        page_name="google-editor.html",
        external_id="google-1",
        company_id="70000000000000091",
        command="prepare-google-listing",
        env="ZHOL_GOOGLE_CABINET",
        review="google_listing_review",
        closed="live Google Business Profile cabinet is closed",
        open_interactive=google_open,
        launch_blank=google_launch,
        url="https://business.google.com/locations/1",
        prepared=("phones", "category", "hours", "website", "description"),
        category_label="Категория",
        create_detail="Google prepare edits an existing organization",
    ),
}


@pytest.fixture
def session(migrated_engine: Engine):
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _spec(platform: str, key: str, **changes: object) -> dict:
    case = CASES[platform]
    company_id = str(changes.pop("company_id", case.company_id))
    message_id = str(changes.pop("message_id", "m1"))
    action = str(changes.pop("action", "update"))
    authorization = str(changes.pop("authorization", "verified"))
    external_id = str(changes.pop("external_id", case.external_id))
    spec: dict = {
        "key": key,
        "company": {"id_2gis": company_id, "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": platform,
                "action": action,
                "owner_authorization": authorization,
                **({} if action == "create" else {"external_id": external_id}),
            }
        ],
        "messages": [{"id": message_id, "text": "Салон на Абая. Сайт, телефон, часы, рубрика и стрижка."}],
        "facts": [
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
            {"field": "entrance_hint", "value": "2 этаж", "source_ref": f"msg:{message_id}", "approved": True},
        ],
        "changes": [
            {
                "platform": platform,
                "field": "phones",
                "after": ["+7 701 111 22 33"],
                "source_ref": f"msg:{message_id}",
                "nap_consent_ref": f"msg:{message_id}",
            },
            {
                "platform": platform,
                "field": "category",
                "after": "Парикмахерская",
                "source_ref": f"msg:{message_id}",
                "nap_consent_ref": f"msg:{message_id}",
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


def _view(platform: str, html: str | None = None):
    case = CASES[platform]
    text = html if html is not None else (PAGES / str(case.page_name)).read_text(encoding="utf-8")
    return case.cabinet(text, MARKERS)


def _prepare(session: Session, platform: str, order_id: int, page: object, folder: Path, **kwargs: object):
    return CASES[platform].prepare(session, order_id, page=page, evidence_dir=folder, **kwargs)


def _logs(session: Session, platform: str) -> list[ChangeLog]:
    return list(
        session.scalars(
            select(ChangeLog)
            .join(ChangeItem, ChangeLog.change_item_id == ChangeItem.id)
            .where(ChangeItem.platform == platform, ChangeLog.action == "prepare")
        )
    )


def test_editor_fields_follow_the_dictionary() -> None:
    twogis = load_twogis_editor()
    google = load_google_editor()
    assert load_cabinet("2gis").submit is False and load_cabinet("google").submit is False
    assert {"name", "address", "category", "phones", "link", "hours", "services", "whatsapp", "instagram"} <= set(twogis.fields)
    assert "description" not in twogis.fields and "prices" not in twogis.fields
    assert twogis.fields["link"].name == "Сайт"
    assert {"name", "address", "category", "phones", "website", "hours", "description"} <= set(google.fields)
    assert "services" not in google.fields and "whatsapp" not in google.fields and "instagram" not in google.fields
    assert "Сохранить" in twogis.submit_names and "Publish" in google.submit_names
    assert "description" in TwoGisAdapter.forbidden and "prices" in TwoGisAdapter.forbidden
    assert "services" in GoogleBusinessAdapter.forbidden and "description" not in GoogleBusinessAdapter.forbidden
    ctx = ListingContext(
        order_id=1,
        company_id=1,
        company_name="Salon Test",
        platform="google",
        action="update",
        external_id="google-1",
        public_url=None,
        access_status="verified",
        claimed_authorization="verified",
        profile_status=None,
        page_html=None,
        snapshot_fields={},
        snapshot_match="found",
        screenshot_path=None,
    )
    prepared = GoogleBusinessAdapter().prepare_update(
        ctx,
        (
            PreparedField("description", "text", "old", "new", "msg:m1"),
            PreparedField("services", "services", None, [{"name": "Стрижка", "price": 8000}], "msg:m1"),
            PreparedField("whatsapp", "text", None, "7700", "msg:m1"),
        ),
    )
    assert [field.field for field in prepared.fields] == ["description"]
    assert prepared.dropped == ("services", "whatsapp") and prepared.submit is False
    applied = GoogleBusinessAdapter().apply_changes(ctx, prepared)
    assert applied.submitted is False and applied.result == "dry_run" and applied.screenshot_after is None


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_prepare_fills_approved_fields_and_stops_before_submit(session: Session, tmp_path: Path, platform: str) -> None:
    case = CASES[platform]
    order = create_test_order(session, _spec(platform, f"e73-{platform}"))
    assert order.company_id is not None
    session.add(
        ContentItem(
            order_id=order.id,
            kind="cover",
            platform=platform,
            brief={"illustrative": True},
            result_path="generated/cover.png",
            status="checked",
        )
    )
    session.add(
        ContentItem(
            order_id=order.id,
            kind="photo_work",
            platform=platform,
            brief={"illustrative": True},
            result_path="generated/work.png",
            status="checked",
        )
    )
    session.add(
        Credential(
            company_id=order.company_id,
            platform=platform,
            login_hint="o***",
            ciphertext=b"sealed-secret-e73",
            created_at=now(),
        )
    )
    session.add(BrowserProfile(platform=platform, account_label="other", path="/profiles/unrelated", status="ok"))
    session.flush()
    view = _view(platform)
    folder = tmp_path / "evidence"
    report = _prepare(session, platform, order.id, view, folder)
    assert report.status == "prepared" and report.platform == platform
    assert report.submitted is False and report.published is False and report.approval_required is True
    by_field = {item.field: item for item in report.changes}
    for name in case.prepared:
        assert by_field[name].action == "prepared"
    assert by_field["name"].action == "already" and by_field["address"].action == "already"
    assert by_field["entrance_hint"].action == "not_filled"
    for name in ("cover", "photos", "logo"):
        assert by_field[name].action == "not_filled"
    assert view.value("Телефон") == "+7 701 111 22 33"
    assert view.value("Часы работы") == "10:00-20:00"
    assert view.value(case.category_label) == "Парикмахерская"
    assert view.value("Сайт") == "https://salon.example"
    assert view.value("Название") == "Salon Test"
    assert view.value("Адрес") == "ул. Абая 10"
    assert view.value("Почта") == "keep@example.test"
    assert view.value("WhatsApp") == "77000000000"
    assert view.value("Instagram") == "oldsalon"
    assert view.clicked() == ()
    assert {name for name, _value in view.writes}.isdisjoint(SUBMIT)
    blob = "\n".join(value for _name, value in view.writes)
    assert "generated/cover.png" not in blob and "generated/work.png" not in blob
    assert "materials/hall.png" not in blob and "2 этаж" not in blob
    if platform == "2gis":
        assert by_field["description"].action == "not_filled"
        assert by_field["link"].action == "prepared"
        assert view.value("Услуги и цены") == "Стрижка: 8000"
        assert view.value("Описание") == "Описание только для чтения"
        assert view.field_text("prices") == "пусто"
    else:
        assert by_field["services"].action == "not_filled"
        assert by_field["description"].action == "prepared"
        assert view.value("Описание") == "Салон на Абая"
        assert view.value("Услуги и цены") == "Стрижка: 7000"
    assert session.get(Order, order.id).stage == "created"  # type: ignore[union-attr]
    logs = _logs(session, platform)
    assert len(logs) == len(case.prepared)
    assert all(
        row.result == "prepared"
        and row.screenshot_before
        and row.screenshot_after
        and row.details["submitted"] is False
        and row.details["published"] is False
        for row in logs
    )
    assert all(row.status == "draft" for row in session.scalars(select(ChangeItem)))
    task = session.scalars(select(HumanTask).where(HumanTask.reason == case.review)).one()
    assert task.status == "open" and task.details is not None
    assert task.details["approval"] == "required" and task.details["published"] is False
    review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    assert review["submitted"] is False and review["published"] is False and review["platform"] == platform
    assert (folder / "before.png").is_file() and (folder / "prepared.png").is_file()
    stored = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in folder.rglob("*") if path.is_file())
    assert "sealed-secret-e73" not in stored and "/profiles/unrelated" not in stored

    again = _view(platform)
    second = _prepare(session, platform, order.id, again, folder)
    assert again.writes == []
    assert second.status == "already" and second.submitted is False
    assert len(_logs(session, platform)) == len(case.prepared)
    assert session.scalar(select(func.count()).select_from(HumanTask).where(HumanTask.reason == case.review)) == 1


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_wrong_organization_is_rejected(session: Session, tmp_path: Path, platform: str) -> None:
    case = CASES[platform]
    order = create_test_order(session, _spec(platform, f"e73-wrong-{platform}"))
    html = (PAGES / str(case.page_name)).read_text(encoding="utf-8")
    variants = (
        html.replace(str(case.external_id), "other-9"),
        html.replace("Salon Test", "Other Salon"),
    )
    for index, text in enumerate(variants):
        view = _view(platform, text)
        report = _prepare(session, platform, order.id, view, tmp_path / str(index))
        assert report.status == "stopped" and report.detail == "organization does not match the order"
        assert view.writes == [] and view.clicked() == ()
        assert not (tmp_path / str(index) / "prepared.png").exists()
    assert _logs(session, platform) == []


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_login_captcha_sms_and_reauth_pause(session: Session, tmp_path: Path, platform: str) -> None:
    order = create_test_order(session, _spec(platform, f"e73-pause-{platform}"))
    login = _view(
        platform,
        f'<html><body data-page="login" data-platform="{platform}"><p>Войдите в аккаунт</p><input type="password" aria-label="Пароль" value="secret-value"></body></html>',
    )
    login_report = _prepare(session, platform, order.id, login, tmp_path / "login")
    assert login_report.status == "paused" and login_report.detail == "login"
    assert login.writes == [] and login.clicked() == ()
    assert {path.name for path in (tmp_path / "login").iterdir()} == {"review.json"}
    assert "secret-value" not in (tmp_path / "login" / "review.json").read_text(encoding="utf-8")

    captcha = _view(platform, (PAGES / str(CASES[platform].page_name)).read_text(encoding="utf-8").replace("</h1>", "</h1><p>Подтвердите, что вы не робот</p>", 1))
    captcha_report = _prepare(session, platform, order.id, captcha, tmp_path / "captcha")
    assert captcha_report.status == "paused" and captcha_report.detail == "captcha"
    assert captcha.writes == [] and captcha.value("Часы работы") == "10:00-19:00"
    assert (tmp_path / "captcha" / "before.png").is_file()
    assert not (tmp_path / "captcha" / "prepared.png").exists()

    sms = _view(
        platform,
        f'<html><body data-page="cabinet" data-platform="{platform}"><p>Введите код из СМС</p><input aria-label="Код из СМС" value="123456"></body></html>',
    )
    sms_report = _prepare(session, platform, order.id, sms, tmp_path / "sms")
    assert sms_report.status == "paused" and sms_report.detail == "sms" and sms.writes == []
    sms_blob = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in (tmp_path / "sms").rglob("*"))
    assert "123456" not in sms_blob

    reauth = _view(
        platform,
        f'<html><body data-page="cabinet" data-platform="{platform}" data-external-id="{CASES[platform].external_id}"><p>Подтвердите, что это вы</p></body></html>',
    )
    reauth_report = _prepare(session, platform, order.id, reauth, tmp_path / "reauth")
    assert reauth_report.status == "paused" and reauth_report.detail == "login" and reauth.writes == []
    pauses = list(session.scalars(select(HumanTask).where(HumanTask.reason == "listing_paused")))
    assert {row.details["pause"] for row in pauses if row.details} == {"login", "captcha", "sms"}
    assert all(row.details["platform"] == platform and row.details["submitted"] is False for row in pauses if row.details)
    assert _logs(session, platform) == []


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_missing_permission_is_refused(session: Session, tmp_path: Path, platform: str) -> None:
    denied_order = create_test_order(session, _spec(platform, f"e73-denied-{platform}"))
    html = (PAGES / str(CASES[platform].page_name)).read_text(encoding="utf-8").replace("Владелец", "Наблюдатель")
    denied = _view(platform, html)
    denied_report = _prepare(session, platform, denied_order.id, denied, tmp_path / "denied")
    assert denied_report.status == "refused" and denied_report.detail == "missing owner permission"
    assert denied.writes == [] and denied.clicked() == ()

    invited = create_test_order(
        session,
        _spec(
            platform,
            f"e73-invited-{platform}",
            company_id=f"70000000000000{platform[:1]}2",
            message_id="m2",
            authorization="invited",
            external_id="70000000000000082" if platform == "2gis" else "google-invited",
        ),
    )

    class Closed:
        def body_text(self) -> str:
            raise AssertionError("the cabinet was opened")

    invited_report = _prepare(session, platform, invited.id, Closed(), tmp_path / "invited")
    assert invited_report.status == "refused" and invited_report.detail == "missing owner permission"


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_representative_may_prepare_a_matching_cabinet(session: Session, tmp_path: Path, platform: str) -> None:
    order = create_test_order(session, _spec(platform, f"e73-rep-{platform}"))
    html = (PAGES / str(CASES[platform].page_name)).read_text(encoding="utf-8")
    html = html.replace("Владелец", "Представитель")
    html = html.replace("Салон красоты", "Парикмахерская")
    html = html.replace("+7 700 000 00 00", "+7 701 111 22 33")
    html = html.replace("https://old.example", "https://salon.example")
    html = html.replace("10:00-19:00", "10:00-20:00")
    html = html.replace("Стрижка: 7000", "Стрижка: 8000")
    html = html.replace("Коротко о салоне", "Салон на Абая")
    view = _view(platform, html)
    report = _prepare(session, platform, order.id, view, tmp_path)
    assert report.status == "already"
    assert view.writes == [] and view.clicked() == ()


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_unapproved_and_unconsented_fields_stay(session: Session, tmp_path: Path, platform: str) -> None:
    spec = _spec(platform, f"e73-held-{platform}", changes=[])
    for fact in spec["facts"]:
        if fact["field"] == "description":
            fact["approved"] = False
    order = create_test_order(session, spec)
    view = _view(platform)
    report = _prepare(session, platform, order.id, view, tmp_path)
    by_field = {item.field: item for item in report.changes}
    assert report.status == "prepared" and report.submitted is False
    assert by_field["hours"].action == "prepared" and by_field["phones"].action == "held"
    assert view.value("Часы работы") == "10:00-20:00"
    assert view.value("Телефон") == "+7 700 000 00 00"
    assert view.clicked() == ()
    if platform == "google":
        assert by_field["description"].action == "held"
        assert view.value("Описание") == "Коротко о салоне"
    else:
        assert "description" not in by_field
        assert view.value("Описание") == "Описание только для чтения"


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_missing_control_stops_without_a_partial_write(session: Session, tmp_path: Path, platform: str) -> None:
    order = create_test_order(session, _spec(platform, f"e73-missing-{platform}"))
    html = (PAGES / str(CASES[platform].page_name)).read_text(encoding="utf-8").replace(HOURS, "")
    view = _view(platform, html)
    report = _prepare(session, platform, order.id, view, tmp_path)
    assert report.status == "stopped" and report.detail == "stopped_unknown_ui"
    assert view.writes == [] and view.clicked() == ()
    assert view.value("Сайт") == "https://old.example"
    assert not (tmp_path / "prepared.png").exists()


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_create_and_unknown_layout_stop(session: Session, tmp_path: Path, platform: str) -> None:
    case = CASES[platform]
    created = create_test_order(session, _spec(platform, f"e73-create-{platform}", action="create"))

    class Closed:
        def body_text(self) -> str:
            raise AssertionError("the cabinet was opened")

    created_report = _prepare(session, platform, created.id, Closed(), tmp_path / "create")
    assert created_report.status == "stopped" and created_report.detail == case.create_detail
    assert _logs(session, platform) == []

    unknown_order = create_test_order(
        session,
        _spec(platform, f"e73-unknown-{platform}", company_id=f"700000000000009{platform[:1]}", message_id="m9"),
    )
    html = (PAGES / str(case.page_name)).read_text(encoding="utf-8").replace('data-page="cabinet"', 'data-page="wizard"', 1)
    unknown = _view(platform, html)
    unknown_report = _prepare(session, platform, unknown_order.id, unknown, tmp_path / "unknown")
    assert unknown_report.status == "stopped" and unknown_report.detail == "stopped_unknown_ui"
    assert unknown.writes == [] and unknown.clicked() == ()


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_approval_flag_does_not_submit(session: Session, tmp_path: Path, platform: str) -> None:
    order = create_test_order(session, _spec(platform, f"e73-approve-{platform}"))

    class Closed:
        def body_text(self) -> str:
            raise AssertionError("the cabinet was opened")

    with pytest.raises(PermissionError):
        _prepare(session, platform, order.id, Closed(), tmp_path, approve=True)
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_live_session_is_headed_and_has_no_saved_profile(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    case = CASES[platform]
    monkeypatch.delenv(str(case.env), raising=False)
    with pytest.raises(PermissionError, match=str(case.closed)):
        case.open_interactive(case.url)
    monkeypatch.setenv(str(case.env), "1")
    with pytest.raises(PermissionError, match="https"):
        case.open_interactive("http://example.test/cabinet")
    calls: dict[str, object] = {}

    class Browser:
        def new_context(self, **kwargs: object) -> "Browser":
            calls["context"] = kwargs
            return self

        def new_page(self) -> "Browser":
            return self

        def goto(self, url: str, **kwargs: object) -> None:
            calls["goto"] = (url, kwargs)

    class Playwright:
        def __init__(self) -> None:
            self.chromium = self

        def launch(self, **kwargs: object) -> Browser:
            calls["launch"] = kwargs
            return Browser()

    case.open_interactive(case.url, playwright=Playwright())
    assert calls["launch"] == {"headless": False}
    assert calls["context"] == {}
    assert calls["goto"] == (case.url, {"wait_until": "domcontentloaded"})
    case.launch_blank(Playwright(), "https://example.test/next")
    assert calls["goto"] == ("https://example.test/next", {"wait_until": "domcontentloaded"})


def test_prepare_modules_do_not_store_secrets() -> None:
    paths = [
        ROOT / "zhol" / "browser" / "cabinet" / "form.py",
        ROOT / "zhol" / "browser" / "cabinet" / "prepare.py",
        ROOT / "zhol" / "browser" / "cabinet" / "editor.py",
    ]
    for folder in (ROOT / "zhol" / "browser" / "twogis", ROOT / "zhol" / "browser" / "google"):
        for name in ("prepare.py", "session.py", "page.py", "spec.py"):
            paths.append(folder / name)
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    live = (ROOT / "zhol" / "browser" / "headed.py").read_text(encoding="utf-8")
    for banned in ("storage_state", "user_data_dir", "ciphertext", "add_init_script", ".click(", "page.fill"):
        assert banned not in text
        assert banned not in live
    assert "headless=False" in live
    assert "sync_playwright" not in text


def test_partial_platform_failure_keeps_the_other_log(session: Session, tmp_path: Path) -> None:
    spec = _spec("2gis", "e73-partial", company_id="70000000000000081", message_id="m1")
    spec["platforms"].append(
        {"platform": "google", "action": "update", "external_id": "google-1", "owner_authorization": "verified"}
    )
    spec["changes"].extend(
        [
            {
                "platform": "google",
                "field": "phones",
                "after": ["+7 701 111 22 33"],
                "source_ref": "msg:m1",
                "nap_consent_ref": "msg:m1",
            },
            {
                "platform": "google",
                "field": "category",
                "after": "Парикмахерская",
                "source_ref": "msg:m1",
                "nap_consent_ref": "msg:m1",
            },
        ]
    )
    order = create_test_order(session, spec)
    twogis = _prepare(session, "2gis", order.id, _view("2gis"), tmp_path / "2gis")
    assert twogis.status == "prepared" and len(_logs(session, "2gis")) == 5
    paused = _prepare(
        session,
        "google",
        order.id,
        _view("google", '<html><body data-page="login" data-platform="google"><p>Войдите в аккаунт</p></body></html>'),
        tmp_path / "google-pause",
    )
    assert paused.status == "paused" and paused.detail == "login"
    assert len(_logs(session, "2gis")) == 5 and _logs(session, "google") == []

    other = _spec(
        "google",
        "e73-partial-google",
        company_id="70000000000000092",
        message_id="m3",
        external_id="google-partial-2",
    )
    other["platforms"].append(
        {
            "platform": "2gis",
            "action": "update",
            "external_id": "70000000000000082",
            "owner_authorization": "verified",
        }
    )
    other["changes"].extend(
        [
            {
                "platform": "2gis",
                "field": "phones",
                "after": ["+7 701 111 22 33"],
                "source_ref": "msg:m3",
                "nap_consent_ref": "msg:m3",
            },
            {
                "platform": "2gis",
                "field": "category",
                "after": "Парикмахерская",
                "source_ref": "msg:m3",
                "nap_consent_ref": "msg:m3",
            },
        ]
    )
    second = create_test_order(session, other)
    google_html = (PAGES / "google-editor.html").read_text(encoding="utf-8").replace("google-1", "google-partial-2")
    google = _prepare(session, "google", second.id, _view("google", google_html), tmp_path / "google")
    assert google.status == "prepared"
    wrong_html = (PAGES / "2gis-editor.html").read_text(encoding="utf-8").replace("70000000000000081", "other-9")
    stopped = _prepare(session, "2gis", second.id, _view("2gis", wrong_html), tmp_path / "2gis-wrong")
    assert stopped.status == "stopped"
    assert len(_logs(session, "google")) == 5
    assert len(_logs(session, "2gis")) == 5


@pytest.mark.parametrize("platform", ["2gis", "google"])
def test_cli_prepare_listing(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str], platform: str) -> None:
    case = CASES[platform]
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    path = tmp_path / "order.json"
    path.write_text(json.dumps(_spec(platform, f"e73-cli-{platform}")), encoding="utf-8")
    assert main(["create-test-order", "--spec", str(path)], factory=factory) == 0
    order_id = capsys.readouterr().out.strip().split()[1]
    evidence = tmp_path / "out"
    assert (
        main(
            [str(case.command), "--order-id", order_id, "--page", str(PAGES / str(case.page_name)), "--evidence", str(evidence)],
            factory=factory,
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "mode cabinet_prepare" in printed
    assert f"platform {platform}" in printed
    assert "submitted false" in printed and "published false" in printed
    assert "approval required" in printed and "hours prepared" in printed
    review = json.loads((evidence / "review.json").read_text(encoding="utf-8"))
    assert review["submitted"] is False and review["published"] is False and review["platform"] == platform


def test_callables_are_the_platform_commands() -> None:
    prepare_2gis: Callable[..., object] = CASES["2gis"].prepare
    prepare_google: Callable[..., object] = CASES["google"].prepare
    assert prepare_2gis is prepare_2gis_listing
    assert prepare_google is prepare_google_listing
