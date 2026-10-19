"""E6 content drafts. Generation stays offline and does not publish a listing."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.cli import main
from zhol.content.checks import check_text
from zhol.content.images import IllustrationRequest, OfflineIllustrationProvider
from zhol.content.limits import char_limit
from zhol.content.prepare import (
    ContentError,
    approve_content,
    prepare_content,
    request_illustration,
)
from zhol.db.models import (
    CardSnapshot,
    ChangeItem,
    ChangeLog,
    CompanyProfile,
    ContentItem,
    HumanTask,
    Intake,
    ListingSubmission,
    Order,
    Review,
)
from zhol.fulfillment.capabilities import load_cabinet
from zhol.fulfillment.compare import build_field_plans
from zhol.fulfillment.content import ContentLibrary, ContentPiece
from zhol.fulfillment.engine import FulfillmentRefused, run_listing
from zhol.guards import refuse_fabricated_scene
from zhol.llm import FakeProvider, Gateway
from zhol.llm.config import load_config
from zhol.orders.intake import create_test_order
from zhol.timeutil import now

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TEXT_KEY = "creator-text:stub-claude-sonnet"
REPLY_KEY = "executor-review-reply:stub-claude-sonnet"
APPROVED = (
    {"path": "name", "source_ref": "field:company.name", "value": "Salon Test"},
    {"path": "city", "source_ref": "field:company.city", "value": "Алматы"},
    {"path": "services", "source_ref": "msg:m1", "value": [{"name": "Стрижка", "price": 8000}]},
)
CLAIMS = (
    {"fact": "name", "source_ref": "field:company.name", "text": "Salon Test"},
    {"fact": "city", "source_ref": "field:company.city", "text": "Алматы"},
    {"fact": "services", "source_ref": "msg:m1", "text": "Стрижка"},
)
GOOD = {"language": "ru", "text": "Salon Test в Алматы. Стрижка.", "claims": list(CLAIMS)}
KAZAKH = {"language": "kk", "text": "Salon Test Алматыда шаш қияды.", "claims": list(CLAIMS)}
LONG = {"language": "ru", "text": "Салон " * 126, "claims": [CLAIMS[0]]}
REPLY = {"reply": "Спасибо за отзыв.", "language": "ru", "needs_client_approval": False, "flags": []}


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    db = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _gateway(script: dict) -> tuple[Gateway, FakeProvider]:
    provider = FakeProvider(script)
    providers = {name: provider for name in ("anthropic", "openai", "google")}
    return Gateway(load_config(STUB, ROOT), providers=providers), provider


def _spec(**changes: object) -> dict:
    spec: dict = {
        "key": "content-1",
        "company": {"id_2gis": "70000000000000021", "name": "Salon Test", "city": "Алматы"},
        "platforms": [
            {
                "platform": "2gis",
                "action": "update",
                "external_id": "70000000000000021",
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
    }
    spec.update(changes)
    return spec


def _texts(count: int, reply: dict | None = None) -> dict:
    return {TEXT_KEY: [reply or GOOD] * count}


def _snapshot(session: Session, company_id: int, platform: str) -> None:
    session.add(
        CardSnapshot(
            company_id=company_id,
            platform=platform,
            match_status="found",
            url=f"https://example.test/{platform}",
            fields={
                "name": "Salon Test",
                "address": "ул. Абая 10",
                "phones": ["+7 701 111 22 33"],
                "hours": "10:00-20:00",
                "services": [{"name": "Стрижка", "price": 8000}],
                "photos_count": 4,
            },
            field_sources={},
            screenshot_path=f"shots/{platform}.png",
            purpose="order_fresh",
            collected_at=now(),
            collected_by="test",
            status="verified",
        )
    )
    session.flush()


def _items(session: Session) -> list[ContentItem]:
    return list(session.scalars(select(ContentItem).order_by(ContentItem.id)))


def _check(text: str, claims=CLAIMS, language: str = "ru", unverified=(), max_chars: int = 750) -> str | None:
    return check_text(
        text=text,
        language=language,
        claims=claims,
        approved=APPROVED,
        unverified=unverified,
        max_chars=max_chars,
    )


def test_text_checks_reject_unsupported_claims_and_keep_both_languages() -> None:
    assert _check(GOOD["text"]) is None
    assert _check(KAZAKH["text"], language="kk") is None
    assert _check("Salon Test в Алматы.", language="kk") == "language does not match"
    assert _check("Salon Test в Алматы. Стрижка стоит 99999.") == "unsupported number"
    assert _check(GOOD["text"], claims=({"fact": "award", "source_ref": "field:company.name", "text": "Salon Test"},)) == (
        "unsupported claim"
    )
    leaked = "Salon Test в Алматы. ЧЕРНОВИК ЧАСОВ."
    assert _check(leaked, unverified=({"path": "hours", "value": "ЧЕРНОВИК ЧАСОВ"},)) == "uses unverified information"
    assert _check("Salon Test в Алматы, мы лучший салон.") == "forbidden phrase"
    assert _check("Salon Test в Алматы. Работаем с 10:00.") == "unsupported hours"
    assert char_limit("platform_description", "google") == 750
    assert char_limit("platform_description", "yandex") == 1000
    assert char_limit("platform_description", "google") < char_limit("platform_description", "yandex")
    with pytest.raises(PermissionError):
        refuse_fabricated_scene("premises")
    with pytest.raises(PermissionError):
        OfflineIllustrationProvider().generate(IllustrationRequest(kind="product", order_id=1, prompt="product photo"))


def test_approved_facts_are_drafted_and_unapproved_values_stay_out(session: Session, tmp_path: Path) -> None:
    spec = _spec(key="facts")
    spec["facts"].append(
        {"field": "description", "value": "СЕКРЕТНОЕ ОПИСАНИЕ", "source_ref": "msg:m1", "approved": False}
    )
    order = create_test_order(session, spec)
    gateway, provider = _gateway(_texts(3))
    report = prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    by_kind = {row["kind"]: row for row in report.rows}
    assert by_kind["company_description"]["status"] == "done"
    assert by_kind["company_description"]["language"] == "ru"
    assert "website" in by_kind["company_description"]["missing"]
    assert "description" in by_kind["company_description"]["unverified"]
    assert "msg:m1" in by_kind["company_description"]["source_refs"]
    joined = "\n".join(call["user"] for call in provider.calls)
    assert "ул. Абая 10" in joined
    assert "СЕКРЕТНОЕ ОПИСАНИЕ" not in joined
    assert all(call["key"] == TEXT_KEY for call in provider.calls)


def test_client_description_is_not_rewritten(session: Session, tmp_path: Path) -> None:
    spec = _spec(key="has-description")
    spec["facts"].append(
        {"field": "description", "value": "Салон на Абая.", "source_ref": "msg:m1", "approved": True}
    )
    order = create_test_order(session, spec)
    gateway, provider = _gateway(_texts(1))
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    kinds = {row.kind for row in _items(session)}
    assert "company_description" not in kinds
    assert "platform_description" not in kinds
    assert "service_description" in kinds
    assert len(provider.calls) == 1


def test_missing_services_are_not_invented(session: Session, tmp_path: Path) -> None:
    spec = _spec(key="no-services")
    spec["facts"] = [fact for fact in spec["facts"] if fact["field"] != "services"]
    order = create_test_order(session, spec)
    gateway, _provider = _gateway(_texts(2))
    report = prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    company = next(row for row in report.rows if row["kind"] == "company_description")
    assert "services" in company["missing"]
    assert "service_description" not in {row["kind"] for row in report.rows}


def test_unsupported_claim_is_stored_rejected(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="bad-claim"))
    bad = {
        "language": "ru",
        "text": "Salon Test в Алматы. Стрижка стоит 99999.",
        "claims": list(CLAIMS),
    }
    gateway, provider = _gateway({TEXT_KEY: [bad, GOOD, GOOD]})
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    company = next(row for row in _items(session) if row.kind == "company_description")
    assert company.status == "rejected"
    assert company.brief["rejection"] == "unsupported number"
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    assert len(provider.calls) == 3
    assert len(_items(session)) == 4


def test_kazakh_draft_uses_kazakh_letters(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="kazakh"))
    intake = session.get(Intake, order.id)
    assert intake is not None
    checklist = dict(intake.checklist)
    checklist["content_language"] = "kk"
    intake.checklist = checklist
    session.flush()
    gateway, _provider = _gateway(_texts(3, KAZAKH))
    report = prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    descriptions = [row for row in report.rows if row["kind"] in {"company_description", "platform_description"}]
    assert descriptions
    assert all(row["language"] == "kk" and row["status"] == "done" for row in descriptions)
    stored = next(row for row in _items(session) if row.kind == "company_description")
    assert "қ" in stored.brief["text"]


def test_platform_limits_follow_the_dictionary(session: Session, tmp_path: Path) -> None:
    spec = _spec(
        key="limits",
        platforms=[
            {"platform": "google", "action": "update", "external_id": "g-1", "owner_authorization": "verified"},
            {"platform": "yandex", "action": "update", "external_id": "y-1", "owner_authorization": "verified"},
        ],
    )
    order = create_test_order(session, spec)
    assert len(LONG["text"]) == 756
    gateway, _provider = _gateway(_texts(4, LONG))
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    rows = {(row.kind, row.platform): row.status for row in _items(session)}
    assert rows[("company_description", None)] == "rejected"
    assert rows[("platform_description", "google")] == "rejected"
    assert rows[("platform_description", "yandex")] == "done"
    assert rows[("service_description", None)] == "rejected"


def test_image_restrictions_and_label(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="images"))
    pictures = OfflineIllustrationProvider()
    gateway, _provider = _gateway(_texts(3))
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path, images=pictures)
    cover = next(row for row in _items(session) if row.kind == "cover")
    assert cover.status == "done"
    assert cover.brief["origin"] == "ai_illustration"
    assert cover.brief["illustrative"] is True
    assert cover.brief["not_a_photograph"] is True
    assert Path(cover.result_path or "").read_bytes().startswith(b"\x89PNG")
    with pytest.raises(PermissionError):
        request_illustration(session, order.id, "premises", output_dir=tmp_path, provider=pictures)
    with pytest.raises(PermissionError):
        request_illustration(session, order.id, "premises", output_dir=tmp_path, provider=pictures)
    refused = next(row for row in _items(session) if row.kind == "premises")
    assert refused.status == "human" and refused.result_path is None
    assert session.scalar(select(func.count()).select_from(HumanTask)) == 1
    task = session.scalars(select(HumanTask)).one()
    assert task.reason == "content_refused" and task.details["submitted"] is False
    assert len(pictures.calls) == 1


def test_same_facts_do_not_generate_twice(session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    order = create_test_order(session, _spec(key="idem"))
    pictures = OfflineIllustrationProvider()
    gateway, provider = _gateway(_texts(3))
    first = prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path, images=pictures)
    second = prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path, images=pictures)
    assert [row["id"] for row in first.rows] == [row["id"] for row in second.rows]
    assert len(provider.calls) == 3
    assert len(pictures.calls) == 1
    assert session.scalar(select(func.count()).select_from(ContentItem)) == 4


def test_review_replies_require_authorization(session: Session, tmp_path: Path) -> None:
    order = create_test_order(session, _spec(key="reviews"))
    assert order.company_id is not None
    positive = Review(
        company_id=order.company_id,
        order_id=order.id,
        platform="yandex",
        external_id="rev-5",
        rating=5,
        text="Спасибо, всё понравилось",
        author_label="Айгуль",
        posted_at=now(),
        reply_status="new",
    )
    low = Review(
        company_id=order.company_id,
        order_id=order.id,
        platform="yandex",
        external_id="rev-2",
        rating=2,
        text="Долго ждали",
        author_label="Асан",
        posted_at=now(),
        reply_status="new",
    )
    session.add_all([positive, low])
    session.flush()
    gateway, provider = _gateway({**_texts(3), REPLY_KEY: [REPLY, REPLY]})
    skipped = prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    assert skipped.skipped_reviews == 2
    assert not any(row.kind == "review_reply" for row in _items(session))
    assert all(call["key"] == TEXT_KEY for call in provider.calls)
    intake = session.get(Intake, order.id)
    assert intake is not None
    checklist = dict(intake.checklist)
    checklist["review_replies"] = "authorized"
    intake.checklist = checklist
    session.flush()
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path)
    replies = [row for row in _items(session) if row.kind == "review_reply"]
    assert len(replies) == 2
    low_row = next(row for row in replies if row.brief["rating"] == 2)
    high_row = next(row for row in replies if row.brief["rating"] == 5)
    assert low_row.brief["needs_client_approval"] is True
    assert high_row.status == "done"
    assert positive.reply_status == "new" and positive.reply_text is None
    with pytest.raises(PermissionError):
        approve_content(session, low_row.id)
    intake.checklist = {**checklist, "review_reply_consent": [low.id]}
    session.flush()
    approve_content(session, low_row.id)
    assert low_row.status == "checked"
    assert low.reply_status == "new"


def test_approval_is_required_and_cleared_when_facts_change(session: Session, tmp_path: Path) -> None:
    spec = _spec(
        key="invalidate",
        platforms=[
            {"platform": "2gis", "action": "update", "external_id": "70000000000000021", "owner_authorization": "verified"},
            {"platform": "yandex", "action": "update", "external_id": "yandex-1", "owner_authorization": "verified"},
        ],
    )
    order = create_test_order(session, spec)
    assert order.company_id is not None
    _snapshot(session, order.company_id, "2gis")
    _snapshot(session, order.company_id, "yandex")
    gateway, provider = _gateway(_texts(8))
    pictures = OfflineIllustrationProvider()
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path, images=pictures)
    before = run_listing(session, order.id)
    assert not any(plan.field == "description" and plan.operation == "set" for item in before.outcomes for plan in item.plans)
    yandex = next(row for row in _items(session) if row.kind == "platform_description" and row.platform == "yandex")
    cover = next(row for row in _items(session) if row.kind == "cover")
    approve_content(session, yandex.id)
    approve_content(session, cover.id)
    ready = run_listing(session, order.id)
    description = next(plan for item in ready.outcomes if item.platform == "yandex" for plan in item.plans if plan.field == "description")
    assert description.operation == "set" and description.source_ref == f"content:{yandex.id}"
    assert description.after == yandex.brief["text"]
    twogis = [plan.field for item in ready.outcomes if item.platform == "2gis" for plan in item.plans]
    assert "description" not in twogis
    cover_plan = next(plan for item in ready.outcomes if item.platform == "yandex" for plan in item.plans if plan.field == "cover")
    assert cover_plan.after["illustrative"] is True and cover_plan.after["origin"] == "ai_illustration"
    assert ready.outcomes[0].status == "dry_run" or any(item.status == "dry_run" for item in ready.outcomes)
    assert ready.as_dict()["submitted"] is False
    assert session.get(Order, order.id).stage == "created"  # type: ignore[union-attr]
    assert session.scalar(select(func.count()).select_from(ListingSubmission)) == 0
    assert set(session.scalars(select(ChangeLog.result))) == {"dry_run"}
    profile = session.scalar(
        select(CompanyProfile).where(CompanyProfile.company_id == order.company_id).order_by(CompanyProfile.version.desc())
    )
    assert profile is not None
    data = json.loads(json.dumps(profile.data))
    data["hours"] = {"value": "11:00-19:00", "source_ref": "msg:m1", "needs_confirmation": False}
    session.add(CompanyProfile(company_id=order.company_id, version=profile.version + 1, data=data, created_at=now()))
    session.flush()
    prepare_content(session, order.id, gateway=gateway, output_dir=tmp_path, images=pictures)
    assert yandex.status == "done" and yandex.brief["approval"] is None and yandex.brief["version"] == 2
    again = run_listing(session, order.id)
    assert not any(plan.field == "description" and plan.operation == "set" for item in again.outcomes for plan in item.plans)
    assert len(provider.calls) == 8


def test_unapproved_description_blocks_a_generated_substitute() -> None:
    library = ContentLibrary(
        "incomplete",
        (),
        (),
        texts=(
            ContentPiece(
                id=5,
                kind="platform_description",
                platform="yandex",
                path="draft.txt",
                text="Салон на Абая.",
            ),
        ),
    )
    profile = {"description": {"value": "секрет", "source_ref": "msg:m1", "needs_confirmation": True}}
    plans = build_field_plans(
        platform="yandex",
        action="update",
        spec=load_cabinet("yandex"),
        profile=profile,
        company_name="Salon Test",
        files={},
        library=library,
        current={},
        consents=(),
    )
    description = next(plan for plan in plans if plan.field == "description")
    assert description.operation == "waiting_approval"
    twogis = build_field_plans(
        platform="2gis",
        action="update",
        spec=load_cabinet("2gis"),
        profile={},
        company_name="Salon Test",
        files={},
        library=library,
        current={},
        consents=(),
    )
    assert all(plan.field != "description" for plan in twogis)


def test_generated_text_does_not_replace_an_existing_listing_description() -> None:
    library = ContentLibrary(
        "incomplete",
        (),
        (),
        texts=(
            ContentPiece(id=6, kind="company_description", platform=None, path="draft.txt", text="Салон на Абая."),
        ),
    )
    plans = build_field_plans(
        platform="yandex",
        action="update",
        spec=load_cabinet("yandex"),
        profile={},
        company_name="Salon Test",
        files={},
        library=library,
        current={"description": "Уже заполнено"},
        consents=(),
    )
    assert all(plan.field != "description" for plan in plans)


def test_owner_gate_stays_closed(session: Session) -> None:
    spec = _spec(key="owner")
    spec["platforms"][0]["owner_authorization"] = "none"
    order = create_test_order(session, spec)
    session.add(
        ContentItem(
            order_id=order.id,
            kind="cover",
            platform="2gis",
            brief={"illustrative": True, "origin": "ai_illustration"},
            result_path="generated/cover.png",
            status="checked",
        )
    )
    session.flush()
    with pytest.raises(FulfillmentRefused, match="owner permission"):
        run_listing(session, order.id)


def test_cli_prepare_and_status(migrated_engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    path = tmp_path / "order.json"
    path.write_text(json.dumps(_spec(key="cli")), encoding="utf-8")
    assert main(["create-test-order", "--spec", str(path)], factory=factory) == 0
    order_id = capsys.readouterr().out.strip().split()[1]
    gateway, _provider = _gateway(_texts(3))
    out = tmp_path / "content"
    assert main(["prepare-content", "--order-id", order_id, "--out", str(out)], factory=factory, gateway=gateway) == 0
    prepared = capsys.readouterr().out
    assert "company_description - done v1" in prepared
    assert "cover - done v1" in prepared
    assert main(["content-status", "--order-id", order_id], factory=factory) == 0
    assert "company_description - done v1" in capsys.readouterr().out
    with pytest.raises(ContentError):
        with factory() as session:
            prepare_content(session, 0, gateway=gateway, output_dir=out)
