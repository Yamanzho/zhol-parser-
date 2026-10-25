"""E9.8 company header, consent label, and audit preview. Display does not write rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.api.company_view import consent_label, contact_label
from zhol.api.main import app
from zhol.db.models import Audit, CardSnapshot, Company
from zhol.timeutil import ALMATY

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}


def test_false_dnc_is_not_recorded_as_consent() -> None:
    assert contact_label(False) == "Запрет не установлен"
    assert consent_label(False) == "Не записано"
    assert consent_label(True) == "Не записано"
    assert consent_label(None) == "Не записано"
    assert consent_label(False) != "да"


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with TestClient(app) as test_client:
        yield test_client, factory


def _snap(company_id: int, when: datetime, **extra: object) -> CardSnapshot:
    return CardSnapshot(
        company_id=company_id,
        platform=str(extra.pop("platform", "2gis")),
        match_status=str(extra.pop("match_status", "found")),
        url=extra.pop("url", "https://2gis.kz/almaty/firm/e98"),
        fields=extra.pop("fields"),
        field_sources=extra.pop("field_sources", {}),
        screenshot_path=None,
        purpose="prospect",
        collected_at=when,
        collected_by=str(extra.pop("collected_by", "bek_audit")),
        status=str(extra.pop("status", "verified")),
        fail_reason=None,
    )


def test_cosmetology_header_uses_only_an_unambiguous_verified_2gis_address(client) -> None:
    test_client, factory = client
    older = datetime(2026, 9, 30, 12, 0, tzinfo=ALMATY)
    newer = older + timedelta(days=4)
    with factory() as db, db.begin():
        verified = Company(
            id_2gis="e98-verified",
            name="Begim sulu",
            city="Алматы",
            niche="косметология",
            whatsapp="+77010000015",
            source="e98",
            dnc=False,
        )
        collected = Company(
            id_2gis="e98-collected",
            name="ProFace",
            city="Алматы",
            niche="косметология",
            source="e98",
            dnc=False,
        )
        ambiguous = Company(id_2gis="e98-other", name="Другой адрес", city="Алматы", niche="косметология", source="e98", dnc=False)
        conflict = Company(id_2gis="e98-two", name="Два адреса", city="Алматы", niche="косметология", source="e98", dnc=False)
        formula = Company(id_2gis="e98-formula", name="Формула", city="Алматы", niche="косметология", source="e98", dnc=False)
        yandex_only = Company(id_2gis="e98-yandex", name="Только Яндекс", city="Алматы", niche="косметология", source="e98", dnc=False)
        db.add_all([verified, collected, ambiguous, conflict, formula, yandex_only])
        db.flush()
        verified_snap = _snap(
            verified.id,
            older,
            collected_by="manual_import",
            fields={"address": "Улица Курмангазы, 98", "phones": ["+7 777 137 47 13"]},
        )
        newer_collected = _snap(
            verified.id,
            newer,
            status="collected",
            fields={"address": "Другая улица, 1"},
        )
        collected_snap = _snap(
            collected.id,
            older,
            status="collected",
            fields={"address": "Проспект Абылай хана, 92", "phones": ["+7 701 555 78 93"]},
        )
        db.add_all(
            [
                verified_snap,
                newer_collected,
                collected_snap,
                _snap(ambiguous.id, older, match_status="other_address", fields={"address": "Улица Абая, 10"}),
                _snap(conflict.id, older, fields={"address": "Улица А, 1"}),
                _snap(conflict.id, newer, fields={"address": "Улица Б, 2"}),
                _snap(
                    formula.id,
                    older,
                    fields={"address": "Улица В, 3"},
                    field_sources={"address": {"formula": True, "source": "bek_audit"}},
                ),
                _snap(
                    yandex_only.id,
                    older,
                    platform="yandex",
                    url="https://yandex.kz/maps/org/e98/1/",
                    fields={"address": "Улица Яндекса, 4"},
                ),
            ]
        )
        db.flush()
        verified_id = verified.id
        collected_id = collected.id
        ambiguous_id = ambiguous.id
        conflict_id = conflict.id
        formula_id = formula.id
        yandex_id = yandex_only.id
        verified_snap_id = verified_snap.id
        collected_fields = dict(collected_snap.fields)

    shown = test_client.get(f"/api/v1/admin/companies/{verified_id}", headers=_auth()).json()
    header = shown["header"]
    assert header["address"] == "Улица Курмангазы, 98"
    assert header["address_rule"] == "newest_verified_else_collected_unambiguous"
    assert header["address_provenance"]["snapshot_id"] == verified_snap_id
    assert header["address_provenance"]["status"] == "verified"
    assert header["address_provenance"]["platform"] == "2gis"
    assert header["address_provenance"]["freshness"] == "newest_verified"
    assert header["address_verified"] is True
    assert header["consent_label"] == "Не записано"
    assert header["contact_label"] == "Запрет не установлен"
    assert "не подставляется" in header["address_note"] or "не изменялась" in header["address_note"]
    assert shown["workspace"]["report"].keys() >= {"completed", "prepared", "awaiting_approval", "moderation_pending", "missing"}
    assert "Что нужно улучшить" in (ROOT / "web" / "src" / "companies.tsx").read_text(encoding="utf-8")

    hidden = test_client.get(f"/api/v1/admin/companies/{collected_id}", headers=_auth()).json()
    assert hidden["header"]["address"] == "Проспект Абылай хана, 92"
    assert hidden["header"]["address_verified"] is False
    assert "не проверен" in hidden["header"]["address_note"]
    twogis = next(item for item in hidden["cards"] if item["platform"] == "2gis")
    assert next(item["text"] for item in twogis["facts"] if item["label"] == "Адрес") == "Проспект Абылай хана, 92"
    listed = test_client.get("/api/v1/admin/companies", headers=_auth(), params={"q": "ProFace"}).json()
    row = next(item for item in listed["items"] if item["id"] == collected_id)
    assert row["address"] == "Проспект Абылай хана, 92"
    assert row["address_verified"] is False
    assert row["consent_label"] == "Не записано"
    assert row["niche"] == "косметология"

    yandex_body = test_client.get(f"/api/v1/admin/companies/{yandex_id}", headers=_auth()).json()
    assert yandex_body["header"]["address"] == "Улица Яндекса, 4"
    assert yandex_body["header"]["address_verified"] is True
    assert yandex_body["header"]["address_provenance"]["platform"] == "yandex"
    yandex_card = next(item for item in yandex_body["cards"] if item["platform"] == "yandex")
    assert next(item["text"] for item in yandex_card["facts"] if item["label"] == "Адрес") == "Улица Яндекса, 4"

    for company_id in (ambiguous_id, conflict_id, formula_id):
        body = test_client.get(f"/api/v1/admin/companies/{company_id}", headers=_auth()).json()
        assert body["header"]["address"] == "Нет данных"
        assert body["header"]["address_provenance"] is None

    with factory() as db:
        stored = db.get(Company, collected_id)
        assert stored is not None
        assert stored.name == "ProFace"
        assert stored.niche == "косметология"
        assert stored.dnc is False
        snap = db.scalar(select(CardSnapshot).where(CardSnapshot.company_id == collected_id, CardSnapshot.platform == "2gis"))
        assert snap is not None
        assert snap.fields == collected_fields
        assert snap.status == "collected"


def test_audit_preview_does_not_import(client) -> None:
    test_client, factory = client
    book = Workbook()
    sheet = book.active
    sheet.title = "карточки"
    sheet.append(["id_2gis", "название", "слой", "url_2gis", "дата_просмотра", "балл_итого", "город", "адрес_2гис"])
    row = ["70000000000000921", "Salon Test", "середина", "https://2gis.kz/firm/70000000000000921", "2026-09-30", "40", "Алматы", "ул. Абая 10"]
    sheet.append(row)
    sheet.append(["70000000000000921", "Salon Test copy", "середина", "https://2gis.kz/firm/70000000000000921", "2026-09-30", "41", "Алматы", "ул. Абая 10"])
    payload = BytesIO()
    book.save(payload)
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Audit))
    checked = test_client.post(
        "/api/v1/admin/audits/validate",
        headers=HEADERS,
        files={"file": ("audit.xlsx", payload.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["imported"] is False
    assert body["preview"]["companies"] == 2
    assert body["preview"]["duplicates"] == 1
    assert body["preview"]["issues"] >= 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Audit)) == before
    page = (ROOT / "web" / "src" / "pages.tsx").read_text(encoding="utf-8")
    before_confirm = page.split("Проверить файл")[0]
    assert "/api/v1/admin/audits/validate" in before_confirm
    assert "/api/v1/admin/audits/import" not in before_confirm
    nav = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    for removed in ('["orders"', '["content"', '["listings"', '["tasks"', '["reports"'):
        assert removed not in nav
    for label in ("Сбор", "Компании", "Выгрузка", "Дополнительно"):
        assert label in nav
    assert "Главная" in page
    assert "Загрузка и анализ аудита" in page
    assert "#/audits" in (ROOT / "web" / "src" / "home.tsx").read_text(encoding="utf-8")


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}
