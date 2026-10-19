"""Company list and detail stay readable and do not invent missing platform data."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zhol.api.company_view import contact_label
from zhol.api.main import app
from zhol.audit.bek import PROMPT
from zhol.db.models import Audit, CardSnapshot, Company, Credential
from zhol.timeutil import ALMATY

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
SECRET_PATH = "/var/lib/zhol/secret-shot.png"
LOGIN_HINT = "login-hint-e97"
PASSWORD = "password=e97-secret"


def test_contact_label_does_not_treat_false_as_consent() -> None:
    assert contact_label(True) == "Не связываться"
    assert contact_label(False) == "Запрет не установлен"
    assert contact_label(None) == "Не определено"
    assert "согласие" not in contact_label(False)


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _snap(company_id: int, platform: str, when: datetime, **extra: object) -> CardSnapshot:
    fields = extra.pop("fields")
    sources = extra.pop("field_sources", {})
    return CardSnapshot(
        company_id=company_id,
        platform=platform,
        match_status=str(extra.pop("match_status", "found")),
        url=extra.pop("url", None),
        fields=fields,
        field_sources=sources,
        screenshot_path=extra.pop("screenshot_path", None),
        purpose="prospect",
        collected_at=when,
        collected_by=str(extra.pop("collected_by", "manual_import")),
        status=str(extra.pop("status", "verified")),
        fail_reason=extra.pop("fail_reason", None),
    )


def _fact(card: dict, label: str) -> str:
    found = next(item["text"] for item in card["facts"] if item["label"] == label)
    return str(found)


def test_company_view_labels_yandex_history_and_secrets(client) -> None:
    test_client, factory = client
    older = datetime(2026, 9, 30, 12, 0, tzinfo=ALMATY)
    newer = older + timedelta(days=11)
    with factory() as db, db.begin():
        banned = Company(id_2gis="e97-ban", name="Закрытый салон", city="Алматы", niche="салон", source="e97", dnc=True)
        open_company = Company(
            id_2gis="e97-open",
            name="Открытый салон",
            city="Алматы",
            niche="косметология",
            whatsapp="+77010000001",
            instagram="open.salon",
            source="e97",
            dnc=False,
        )
        missing = Company(id_2gis="e97-miss", name="Без Яндекса", city="Алматы", niche="косметология", source="e97")
        db.add_all([banned, open_company, missing])
        db.flush()
        db.add(
            _snap(
                open_company.id,
                "yandex",
                older,
                url="https://yandex.kz/maps/org/badi/166357199721/",
                fields={
                    "rating": 4.9,
                    "reviews_count": 24,
                    "news_count": 0,
                    "photos_count": 69,
                    "prices": "yes_outdated",
                    "replies_count": 8,
                    "reviews_shown": 51,
                    "category": "Салон красоты",
                    "address": "Самал-2, 32",
                    "phones": ["+7 701 000 00 01"],
                    "password": PASSWORD,
                },
                field_sources={"rating": {"url": "https://yandex.kz/maps/org/badi/166357199721/", "seen_at": older.isoformat()}},
                screenshot_path=SECRET_PATH,
            )
        )
        db.add(
            _snap(
                open_company.id,
                "yandex",
                newer,
                collected_by="scout",
                status="collected",
                url="https://yandex.kz/maps/org/badi/166357199721/",
                fields={"rating": 5, "reviews_count": 30, "news_count": None, "photos_count": 70},
                screenshot_path="https://shots.example/badi.png",
            )
        )
        db.add(
            _snap(
                open_company.id,
                "2gis",
                older,
                url="https://2gis.kz/almaty/firm/e97-open",
                fields={"rating": 4.8, "address": "Самал-2, 32", "phones": ["+7 701 000 00 01"], "category": "Салон красоты"},
            )
        )
        db.add(
            _snap(
                open_company.id,
                "google",
                older,
                match_status="not_found",
                fields={"rating": None, "reviews_count": 0},
                field_sources={"rating": {"formula": True, "source": "bek_audit"}},
            )
        )
        db.add(
            _snap(
                missing.id,
                "2gis",
                older,
                url="https://2gis.kz/almaty/firm/e97-miss",
                fields={"rating": 4.8, "address": "Абая, 10", "reviews_count": 3},
                status="collected",
                collected_by="bek_audit",
            )
        )
        db.add(
            Audit(
                company_id=missing.id,
                snapshot_ids=[],
                score_total=61,
                score_groups={},
                derived={},
                discrepancies=[],
                gaps=[],
                observations={
                    "source": "bek_audit",
                    "collected_on": "2026-09-30",
                    "texts": ["в Яндексе нет оценок"],
                    "password": PASSWORD,
                },
                status="draft",
                prompt_version=PROMPT,
                model=None,
            )
        )
        db.add(Credential(company_id=open_company.id, platform="yandex", login_hint=LOGIN_HINT, ciphertext=b"secret-bytes"))
        open_id = open_company.id
        missing_id = missing.id
        banned_id = banned.id

    assert test_client.get("/api/v1/admin/companies").status_code == 401
    listed = test_client.get("/api/v1/admin/companies", headers=_auth(), params={"q": "салон"})
    assert listed.status_code == 200
    body = listed.json()
    labels = {item["id"]: item["contact_label"] for item in body["items"]}
    assert labels[banned_id] == "Не связываться"
    assert labels[open_id] == "Запрет не установлен"
    assert "не согласие" in body["contact_hint"]
    assert "Do Not Contact" not in body["contact_hint"]
    open_row = next(item for item in body["items"] if item["id"] == open_id)
    assert open_row["consent_label"] == "Не записано"
    assert open_row["category"] == "косметология"
    assert open_row["address"] == "Самал-2, 32"
    assert open_row["phone"] == "+7 701 000 00 01"
    marks = {item["label"]: item["text"] for item in open_row["platforms_view"]}
    assert marks["Яндекс Карты"] == "есть, не подтверждено"
    assert marks["2ГИС"] == "есть"
    assert marks["Google Maps"] == "не найдена"

    detail = test_client.get(f"/api/v1/admin/companies/{open_id}", headers=_auth())
    assert detail.status_code == 200
    card = detail.json()
    header = card["header"]
    assert header["name"] == "Открытый салон"
    assert header["whatsapp"] == "+77010000001"
    assert header["instagram"] == "open.salon"
    assert header["address"] == "Самал-2, 32"
    assert header["category"] == "Салон красоты"
    assert header["sector"] == "Красота"
    assert header["contact_label"] == "Запрет не установлен"
    assert header["live_verification"] is False
    yandex = next(item for item in card["cards"] if item["platform"] == "yandex")
    google = next(item for item in card["cards"] if item["platform"] == "google")
    twogis = next(item for item in card["cards"] if item["platform"] == "2gis")
    phone = next(item for item in twogis["contacts"] if item["label"] == "Телефон")
    assert phone["links"][0]["href"] == "tel:+77010000001"
    assert phone["links"][0]["text"] == "+7 701 000 00 01"
    assert header["phone_links"][0]["href"] == "tel:+77010000001"
    assert header["whatsapp_href"] == "https://wa.me/77010000001"
    assert header["instagram_href"] == "https://instagram.com/open.salon"
    link = next(item for item in twogis["facts"] if item["label"] == "Ссылка на карточку")
    assert link["href"] == "https://2gis.kz/almaty/firm/e97-open"
    assert link["text"] == link["href"]
    assert yandex["live_verification"] is False
    assert _fact(yandex, "Рейтинг") == "5"
    assert _fact(yandex, "Число отзывов") == "30"
    assert _fact(yandex, "Новости") == "Нет данных"
    assert _fact(google, "Число отзывов") == "0"
    assert _fact(google, "Рейтинг") == "Данные требуют проверки"
    assert _fact(google, "Наличие карточки") == "Не найдена"
    history = next(item for item in card["history"] if item["platform"] == "yandex")
    assert [item["when"] for item in history["items"]] == ["11.10.2026, 12:00", "30.09.2026, 12:00"]
    assert history["items"][0]["changes"]
    older_card_news = _fact({"facts": history["items"][1]["facts"]}, "Новости")
    assert older_card_news == "0"
    assert "8 из 51" in _fact({"facts": history["items"][1]["facts"]}, "Ответы на отзывы")
    shot = next(item for item in yandex["facts"] if item["label"] == "Скриншот")
    assert shot["href"] == "https://shots.example/badi.png"
    assert SECRET_PATH not in detail.text
    assert PASSWORD not in detail.text
    assert LOGIN_HINT not in detail.text

    missed = test_client.get(f"/api/v1/admin/companies/{missing_id}", headers=_auth())
    assert missed.status_code == 200
    missed_body = missed.json()
    missed_yandex = next(item for item in missed_body["cards"] if item["platform"] == "yandex")
    assert _fact(missed_yandex, "Наличие карточки") == "Не проверялось"
    assert all("href" not in item for item in missed_yandex["facts"])
    assert "4.8" not in " ".join(item["text"] for item in missed_yandex["facts"])
    assert "yandex.kz" not in missed.text
    assert missed_body["historical_audit"]["texts"] == ["в Яндексе нет оценок"]
    assert missed_body["historical_audit"]["observed_on"] == "30.09.2026"
    assert missed_body["historical_audit"]["source_label"] == "Книга аудита Бека"
    assert missed_body["historical_audit"]["live_verification"] is False
    assert missed_body["header"]["address"] == "Абая, 10"
    assert missed_body["header"]["address_verified"] is False
    assert "не проверен" in missed_body["header"]["address_note"]
    assert missed_body["header"]["audit_score"] == "61"
    assert "password" not in missed.text

    page = (ROOT / "web" / "src" / "companies.tsx").read_text(encoding="utf-8")
    css = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    assert "Открыть в 2ГИС" in page
    assert "Открыть в Яндекс Картах" in page
    assert "Открыть в Google Maps" in page
    assert "minmax(0, 1fr)" in css
    assert "overflow-wrap: anywhere" in css
    assert "min-width: 0" in css
    assert page.count("JSON.stringify") == 1
    assert "Технические данные (для разработчика)" in page
    assert "<details className=\"technical\">" in page
    assert "open" not in page.split("Технические данные")[0][-80:]
    assert "История проверок карточек" in page
    assert "Статус связи" in page
