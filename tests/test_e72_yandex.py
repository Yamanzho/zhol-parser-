"""E7.2 prepares a Yandex Business cabinet and does not submit it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.browser.config import load_platforms
from zhol.browser.yandex.page import FixtureCabinet, PlaywrightCabinet
from zhol.browser.yandex.prepare import prepare_yandex_listing, wait_for_person
from zhol.browser.yandex.session import launch_blank, open_interactive
from zhol.browser.yandex.spec import load_editor
from zhol.cli import main
from zhol.db.models import (
    BrowserProfile,
    ChangeItem,
    ChangeLog,
    ContentItem,
    Credential,
    HumanTask,
    Order,
)
from zhol.fulfillment.adapters.yandex import YandexBusinessAdapter
from zhol.fulfillment.capabilities import load_cabinet
from zhol.fulfillment.types import ListingContext, PreparedField
from zhol.orders.intake import create_test_order
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "tests" / "fixtures" / "cabinet"
MARKERS = load_platforms()["markers"]
HOURS = '<label>Часы работы <input aria-label="Часы работы" data-field="hours" value="10:00-19:00"></label>'


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
        "key": "e72-yandex",
        "company": {"id_2gis": "70000000000000072", "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": "yandex",
                "action": "update",
                "external_id": "yandex-1",
                "owner_authorization": "verified",
            }
        ],
        "messages": [{"id": "m1", "text": "Салон на Абая. Сайт, телефон, часы, рубрика и стрижка."}],
        "facts": [
            {"field": "address", "value": "ул. Абая 10", "source_ref": "msg:m1", "approved": True},
            {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "approved": True},
            {"field": "hours", "value": "10:00-20:00", "source_ref": "msg:m1", "approved": True},
            {"field": "website", "value": "https://salon.example", "source_ref": "msg:m1", "approved": True},
            {"field": "description", "value": "Салон на Абая", "source_ref": "msg:m1", "approved": True},
            {"field": "category", "value": "Парикмахерская", "source_ref": "msg:m1", "approved": True},
            {
                "field": "services",
                "value": [{"name": "Стрижка", "price": 8000}],
                "source_ref": "msg:m1",
                "approved": True,
            },
            {"field": "entrance_hint", "value": "2 этаж", "source_ref": "msg:m1", "approved": True},
        ],
        "changes": [
            {
                "platform": "yandex",
                "field": "phones",
                "after": ["+7 701 111 22 33"],
                "source_ref": "msg:m1",
                "nap_consent_ref": "msg:m1",
            },
            {
                "platform": "yandex",
                "field": "category",
                "after": "Парикмахерская",
                "source_ref": "msg:m1",
                "nap_consent_ref": "msg:m1",
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


def _view(name: str = "yandex.html") -> FixtureCabinet:
    return FixtureCabinet((PAGES / name).read_text(encoding="utf-8"), MARKERS)


def _prepare(session: Session, order_id: int, page: object, folder: Path, **kwargs: object):
    return prepare_yandex_listing(session, order_id, page=page, evidence_dir=folder, **kwargs)


def test_yandex_editor_fields_stay_unsubmitted() -> None:
    yandex = load_cabinet("yandex")
    editor = load_editor()
    assert yandex.submit is False
    assert "description" in yandex.fields and "description" not in yandex.read_only
    assert "description" in load_cabinet("2gis").read_only
    assert "description" in load_cabinet("google").fields and "description" not in load_cabinet("google").read_only
    assert {"description", "website", "phones", "hours", "category", "services"} <= set(editor.fields)
    assert "Сохранить" in editor.submit_names and "Опубликовать" in editor.submit_names
    assert "prices" in YandexBusinessAdapter.forbidden
    assert "description" not in YandexBusinessAdapter.forbidden
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
    prepared = YandexBusinessAdapter().prepare_update(
        ctx,
        (
            PreparedField("description", "text", "old", "new", "msg:m1"),
            PreparedField("prices", "text", None, "x", "msg:m1"),
        ),
    )
    assert [field.field for field in prepared.fields] == ["description"]
    assert prepared.dropped == ("prices",)
    assert prepared.submit is False
    applied = YandexBusinessAdapter().apply_changes(ctx, prepared)
    assert applied.submitted is False and applied.result == "dry_run" and applied.screenshot_after is None


def test_prepare_fills_approved_fields_and_stops_before_submit(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec())
    assert order.company_id is not None
    session.add(
        ContentItem(
            order_id=order.id,
            kind="cover",
            platform="yandex",
            brief={"illustrative": True},
            result_path="generated/cover.png",
            status="checked",
        )
    )
    session.add(
        ContentItem(
            order_id=order.id,
            kind="photo_work",
            platform="yandex",
            brief={"illustrative": True},
            result_path="generated/work.png",
            status="checked",
        )
    )
    session.add(
        Credential(
            company_id=order.company_id,
            platform="yandex",
            login_hint="o***",
            ciphertext=b"sealed-secret-e72",
            created_at=now(),
        )
    )
    session.add(BrowserProfile(platform="yandex", account_label="other", path="/profiles/unrelated", status="ok"))
    session.flush()
    view = _view()
    folder = tmp_path / "evidence"
    report = _prepare(session, order.id, view, folder)
    assert report.status == "prepared"
    assert report.submitted is False and report.published is False and report.approval_required is True
    by_field = {item.field: item for item in report.changes}
    for name in ("description", "website", "phones", "hours", "category", "services"):
        assert by_field[name].action == "prepared"
    assert by_field["name"].action == "already"
    assert by_field["address"].action == "already"
    for name in ("cover", "photos", "logo", "entrance_hint"):
        assert by_field[name].action == "not_filled"
    assert view.value("Описание") == "Салон на Абая"
    assert view.value("Сайт") == "https://salon.example"
    assert view.value("Телефон") == "+7 701 111 22 33"
    assert view.value("Часы работы") == "10:00-20:00"
    assert view.value("Рубрика") == "Парикмахерская"
    assert view.value("Услуги и цены") == "Стрижка: 8000"
    assert view.value("Почта") == "keep@example.test"
    assert view.value("Instagram") == "oldsalon"
    assert view.value("WhatsApp") == ""
    assert view.value("Название") == "Salon Test"
    assert view.value("Адрес") == "ул. Абая 10"
    assert view.field_text("prices") == "пусто"
    assert view.clicked() == ()
    written = {name for name, _value in view.writes}
    assert written.isdisjoint({"Сохранить", "Опубликовать", "Отправить", "Save", "Publish", "Submit"})
    blob = "\n".join(value for _name, value in view.writes)
    assert "generated/cover.png" not in blob
    assert "generated/work.png" not in blob
    assert "materials/hall.png" not in blob
    assert "2 этаж" not in blob
    assert session.get(Order, order.id).stage == "created"  # type: ignore[union-attr]
    logs = list(session.scalars(select(ChangeLog).where(ChangeLog.action == "prepare")))
    assert len(logs) == 6
    assert all(
        row.result == "prepared" and row.screenshot_before and row.screenshot_after and row.details["submitted"] is False and row.details["published"] is False
        for row in logs
    )
    assert all(row.status == "draft" for row in session.scalars(select(ChangeItem)))
    task = session.scalars(select(HumanTask)).one()
    assert task.reason == "yandex_listing_review" and task.status == "open"
    assert task.details is not None and task.details["approval"] == "required" and task.details["published"] is False
    review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
    assert review["submitted"] is False and review["published"] is False and review["approval_required"] is True
    assert (folder / "before.png").is_file() and (folder / "prepared.png").is_file()
    page_html = (folder / "page.html").read_text(encoding="utf-8")
    assert "10:00-20:00" in page_html and "keep@example.test" in page_html
    assert "generated/cover.png" not in page_html
    stored = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in folder.rglob("*") if path.is_file())
    assert "sealed-secret-e72" not in stored and "/profiles/unrelated" not in stored

    again = _view()
    second = _prepare(session, order.id, again, folder)
    assert again.writes == []
    assert second.status == "already" and second.submitted is False
    assert session.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.action == "prepare")) == 6
    assert session.scalar(select(func.count()).select_from(HumanTask)) == 1


def test_wrong_organization_is_rejected(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="e72-wrong"))
    for name in ("yandex-wrong-org.html", "yandex-wrong-name.html"):
        view = _view(name)
        report = _prepare(session, order.id, view, tmp_path / name)
        assert report.status == "stopped"
        assert report.detail == "organization does not match the order"
        assert view.writes == [] and view.clicked() == ()
        assert not (tmp_path / name / "prepared.png").exists()
    assert session.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.action == "prepare")) == 0


def test_login_captcha_and_sms_pause(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="e72-pause"))
    login = _view("yandex-login.html")
    login_report = _prepare(session, order.id, login, tmp_path / "login")
    assert login_report.status == "paused" and login_report.detail == "login"
    assert login.writes == [] and login.clicked() == ()
    login_files = {path.name for path in (tmp_path / "login").iterdir()}
    assert login_files == {"review.json"}
    assert "secret-value" not in (tmp_path / "login" / "review.json").read_text(encoding="utf-8")

    captcha = _view("yandex-captcha.html")
    captcha_report = _prepare(session, order.id, captcha, tmp_path / "captcha")
    assert captcha_report.status == "paused" and captcha_report.detail == "captcha"
    assert captcha.writes == [] and captcha.value("Часы работы") == "10:00-19:00"
    assert (tmp_path / "captcha" / "before.png").is_file()
    assert not (tmp_path / "captcha" / "prepared.png").exists()

    sms = _view("yandex-sms.html")
    sms_report = _prepare(session, order.id, sms, tmp_path / "sms")
    assert sms_report.status == "paused" and sms_report.detail == "sms"
    assert sms.writes == []
    sms_blob = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in (tmp_path / "sms").rglob("*"))
    assert "123456" not in sms_blob
    pauses = list(session.scalars(select(HumanTask).where(HumanTask.reason == "listing_paused")))
    assert {row.details["pause"] for row in pauses if row.details} == {"login", "captcha", "sms"}
    assert all(row.details["submitted"] is False for row in pauses if row.details)
    assert session.scalar(select(func.count()).select_from(ChangeLog).where(ChangeLog.action == "prepare")) == 0


def test_missing_permission_is_refused(session: Session, tmp_path: Path) -> None:
    denied_order = create_test_order(session, _spec(key="e72-denied"))
    denied = _view("yandex-denied.html")
    denied_report = _prepare(session, denied_order.id, denied, tmp_path / "denied")
    assert denied_report.status == "refused" and denied_report.detail == "missing owner permission"
    assert denied.writes == [] and denied.clicked() == ()

    invited_spec = _spec(
        key="e72-invited",
        company={"id_2gis": "70000000000000073", "name": "Salon Test", "city": "Алматы"},
        platforms=[
            {
                "platform": "yandex",
                "action": "update",
                "external_id": "yandex-invited",
                "owner_authorization": "invited",
            }
        ],
    )
    invited_spec["messages"] = [{"id": "m2", "text": "Телефон и адрес для проверки доступа."}]
    for fact in invited_spec["facts"]:
        fact["source_ref"] = "msg:m2"
    for change in invited_spec["changes"]:
        change["source_ref"] = "msg:m2"
        if change.get("nap_consent_ref"):
            change["nap_consent_ref"] = "msg:m2"
    invited = create_test_order(session, invited_spec)

    class Closed:
        def body_text(self) -> str:
            raise AssertionError("the cabinet was opened")

    invited_report = _prepare(session, invited.id, Closed(), tmp_path / "invited")
    assert invited_report.status == "refused" and invited_report.detail == "missing owner permission"


def test_representative_may_prepare_a_matching_cabinet(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="e72-representative"))
    html = (PAGES / "yandex.html").read_text(encoding="utf-8")
    html = html.replace("Владелец", "Представитель")
    html = html.replace("Коротко о салоне", "Салон на Абая")
    html = html.replace("https://old.example", "https://salon.example")
    html = html.replace("+7 700 000 00 00", "+7 701 111 22 33")
    html = html.replace("10:00-19:00", "10:00-20:00")
    html = html.replace("Салон красоты", "Парикмахерская")
    html = html.replace("Стрижка: 7000", "Стрижка: 8000")
    view = FixtureCabinet(html, MARKERS)
    report = _prepare(session, order.id, view, tmp_path)
    assert report.status == "already"
    assert view.writes == [] and view.clicked() == ()


def test_unapproved_and_unconsented_fields_stay(session: Session, tmp_path: Path) -> None:
    spec = _spec(key="e72-held", changes=[])
    spec["facts"] = [
        {"field": "address", "value": "ул. Абая 10", "source_ref": "msg:m1", "approved": True},
        {"field": "phones", "value": ["+7 701 111 22 33"], "source_ref": "msg:m1", "approved": True},
        {"field": "hours", "value": "10:00-20:00", "source_ref": "msg:m1", "approved": True},
        {"field": "description", "value": "Салон на Абая", "source_ref": "msg:m1", "approved": False},
    ]
    order = create_test_order(session, spec)
    view = _view()
    report = _prepare(session, order.id, view, tmp_path)
    by_field = {item.field: item for item in report.changes}
    assert report.status == "prepared" and report.submitted is False
    assert by_field["hours"].action == "prepared"
    assert by_field["phones"].action == "held"
    assert by_field["description"].action == "held"
    assert view.value("Часы работы") == "10:00-20:00"
    assert view.value("Телефон") == "+7 700 000 00 00"
    assert view.value("Описание") == "Коротко о салоне"
    assert view.clicked() == ()


def test_missing_control_stops_without_a_partial_write(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="e72-missing"))
    html = (PAGES / "yandex.html").read_text(encoding="utf-8").replace(HOURS, "")
    view = FixtureCabinet(html, MARKERS)
    report = _prepare(session, order.id, view, tmp_path)
    assert report.status == "stopped" and report.detail == "stopped_unknown_ui"
    assert view.writes == [] and view.clicked() == ()
    assert view.value("Описание") == "Коротко о салоне"
    assert not (tmp_path / "prepared.png").exists()


def test_approval_flag_does_not_submit(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="e72-approve"))

    class Closed:
        def body_text(self) -> str:
            raise AssertionError("the cabinet was opened")

    with pytest.raises(PermissionError):
        _prepare(session, order.id, Closed(), tmp_path, approve=True)
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0


def test_live_session_is_headed_and_has_no_saved_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHOL_YANDEX_CABINET", raising=False)
    with pytest.raises(PermissionError):
        open_interactive("https://yandex.kz/sprav/1")
    monkeypatch.setenv("ZHOL_YANDEX_CABINET", "1")
    with pytest.raises(PermissionError, match="https"):
        open_interactive("http://yandex.kz/sprav/1")
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

    open_interactive("https://yandex.kz/sprav/1", playwright=Playwright())
    assert calls["launch"] == {"headless": False}
    assert calls["context"] == {}
    assert calls["goto"] == ("https://yandex.kz/sprav/1", {"wait_until": "domcontentloaded"})
    launch_blank(Playwright(), "https://yandex.kz/sprav/2")
    assert calls["goto"] == ("https://yandex.kz/sprav/2", {"wait_until": "domcontentloaded"})


def test_playwright_wrapper_fills_by_role_and_does_not_click() -> None:
    store: dict[str, object] = {"filled": [], "clicked": [], "values": {"Часы работы": "10:00-19:00"}, "types": {}}

    class Locator:
        def __init__(self, name: str | None = None) -> None:
            self.name = name

        def count(self) -> int:
            values = store["values"]
            assert isinstance(values, dict)
            if self.name is None:
                return 1
            return 1 if self.name in values else 0

        def get_attribute(self, key: str) -> str | None:
            types = store["types"]
            assert isinstance(types, dict)
            if key == "type":
                return types.get(self.name)
            return None

        def input_value(self) -> str:
            values = store["values"]
            assert isinstance(values, dict)
            return str(values[self.name])

        def fill(self, value: str) -> None:
            filled = store["filled"]
            values = store["values"]
            assert isinstance(filled, list) and isinstance(values, dict)
            filled.append((self.name, value))
            values[self.name] = value

        def click(self) -> None:
            clicked = store["clicked"]
            assert isinstance(clicked, list)
            clicked.append(self.name)

        @property
        def first(self) -> "Locator":
            return self

        def inner_text(self) -> str:
            return "Salon Test"

    class Page:
        def get_by_role(self, role: str, name: str | None = None, exact: bool = False) -> Locator:
            assert role == "textbox" and exact is True
            return Locator(name)

        def locator(self, selector: str) -> Locator:
            assert selector
            return Locator()

        def inner_text(self, selector: str) -> str:
            assert selector == "body"
            return "cabinet"

        def screenshot(self, path: str, full_page: bool = False) -> None:
            Path(path).write_bytes(b"png")

    view = PlaywrightCabinet(Page(), MARKERS)
    assert view.control_value("textbox", "Часы работы") == "10:00-19:00"
    view.write_control("textbox", "Часы работы", "10:00-20:00")
    assert store["filled"] == [("Часы работы", "10:00-20:00")]
    assert store["clicked"] == []
    values = store["values"]
    types = store["types"]
    assert isinstance(values, dict) and isinstance(types, dict)
    values["Пароль"] = "secret-value"
    types["Пароль"] = "password"
    with pytest.raises(PermissionError):
        view.control_value("textbox", "Пароль")
    with pytest.raises(PermissionError):
        view.write_control("textbox", "Сохранить", "x")
    assert store["clicked"] == []
    assert "secret-value" not in str(store["filled"])


def test_manual_login_wait_reads_only_visible_text(monkeypatch: pytest.MonkeyPatch) -> None:
    pause = load_cabinet("yandex").pause

    class View:
        def __init__(self) -> None:
            self.text = "Сессия истекла"

        def body_text(self) -> str:
            return self.text

    view = View()
    monkeypatch.setattr("zhol.browser.cabinet.prepare.time.monotonic", lambda: 0)
    text, kind = wait_for_person(view, pause, wait_seconds=0, sleeper=lambda _seconds: pytest.fail("slept"))
    assert kind == "login" and "Сессия истекла" in text

    def sleeper(_seconds: float) -> None:
        view.text = "cabinet is open"

    text, kind = wait_for_person(view, pause, wait_seconds=30, sleeper=sleeper)
    assert kind is None and text == "cabinet is open"
    clock = iter((0, 10))
    monkeypatch.setattr("zhol.browser.cabinet.prepare.time.monotonic", lambda: next(clock))
    view.text = "Подтвердите, что это вы"
    text, kind = wait_for_person(view, pause, wait_seconds=5, sleeper=lambda _seconds: pytest.fail("slept"))
    assert kind == "login"


def test_yandex_prepare_modules_do_not_store_secrets() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "zhol" / "browser" / "yandex").rglob("*.py"))
    live = (ROOT / "zhol" / "browser" / "headed.py").read_text(encoding="utf-8")
    for banned in ("storage_state", "user_data_dir", "ciphertext", "add_init_script", ".click(", "page.fill"):
        assert banned not in text
        assert banned not in live
    assert "headless=False" in live


def test_cli_prepare_yandex_listing(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    path = tmp_path / "order.json"
    path.write_text(json.dumps(_spec(key="e72-cli")), encoding="utf-8")
    assert main(["create-test-order", "--spec", str(path)], factory=factory) == 0
    order_id = capsys.readouterr().out.strip().split()[1]
    evidence = tmp_path / "out"
    assert (
        main(
            ["prepare-yandex-listing", "--order-id", order_id, "--page", str(PAGES / "yandex.html"), "--evidence", str(evidence)],
            factory=factory,
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "mode cabinet_prepare" in printed
    assert "submitted false" in printed
    assert "published false" in printed
    assert "approval required" in printed
    assert "hours prepared" in printed
    review = json.loads((evidence / "review.json").read_text(encoding="utf-8"))
    assert review["submitted"] is False and review["published"] is False
