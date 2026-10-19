"""E10.17 address display, optional screenshots, and saved-page import.

Saved HTML only. No live map site is opened.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zhol.agents.scout.discover import run_discovery
from zhol.agents.scout.verify import check_snapshot
from zhol.api.admin_jobs import handle_scout_batch
from zhol.api.main import app
from zhol.browser.evidence import screenshot_state
from zhol.browser.fixtures import FixtureStore
from zhol.db.models import Audit, CardSnapshot, CollectionBatch, Company, Job, Order
from zhol.timeutil import ALMATY

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Zhol-Request": "admin"}
SALON = "Салон красоты"
WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=ALMATY)
PNG = b"\x89PNG\r\n\x1a\n\x00"


def _page(platform: str, external: str, href: str, name: str, address: str, phone: str) -> str:
    return f"""<!DOCTYPE html><html><body>
<section data-page="search" data-platform="{platform}">
<p data-field="seen_at">2026-10-17T12:00:00+05:00</p>
<a data-candidate="1" data-external-id="{external}" href="{href}">
<span data-field="name">{name}</span>
<span data-field="address">{address}</span>
<span data-field="category">{SALON}</span>
<span data-field="phones">{phone}</span>
</a>
</section></body></html>"""


def test_saved_import_page_is_labeled_and_does_not_ask_for_a_server_path() -> None:
    page = (ROOT / "web" / "src" / "collect.tsx").read_text(encoding="utf-8")
    companies = (ROOT / "web" / "src" / "companies.tsx").read_text(encoding="utf-8")
    assert "Импорт сохранённых страниц" in page
    assert "Это не живой сбор" in page
    assert "Сохранённая страница, не живой сайт" in page
    assert "/api/v1/admin/scout/saved-pages" in page
    assert "validateSavedImport" in page
    assert 'accept=".html,.htm,text/html"' in page
    assert "Скриншот необязателен" in page
    for banned in ("/Users/", "/tmp/", "catalog.yaml", "server path", "filesystem"):
        assert banned not in page
    assert "собрано, не проверено" in companies
    assert "адреса расходятся" in companies


def test_screenshot_file_is_optional_and_never_invented(tmp_path: Path) -> None:
    cards = tmp_path / "2gis"
    cards.mkdir()
    (cards / "1.html").write_text("<html></html>", encoding="utf-8")
    missing = FixtureStore(tmp_path)
    assert missing.screenshot_state == "missing"
    assert missing.screenshot is None
    assert missing.read("2gis", "1.html", "https://2gis.kz/almaty/firm/1", kind="card").screenshot_path is None
    assert not (tmp_path / "blank.png").exists()

    shot = tmp_path / "blank.png"
    shot.write_bytes(PNG)
    present = FixtureStore(tmp_path)
    loaded = present.read("2gis", "1.html", "https://2gis.kz/almaty/firm/1", kind="card")
    assert present.screenshot_state == "present"
    assert loaded.screenshot_path == str(shot)
    assert Path(str(loaded.screenshot_path)).is_file()

    shot.chmod(0)
    try:
        blocked = FixtureStore(tmp_path)
        assert blocked.screenshot_state == "unreadable"
        assert blocked.screenshot is None
        assert blocked.read("2gis", "1.html", "https://2gis.kz/almaty/firm/1", kind="card").screenshot_path is None
    finally:
        shot.chmod(0o644)


def test_verification_requires_a_readable_screenshot_before_it_can_pass(tmp_path: Path) -> None:
    gone = tmp_path / "gone.png"
    missing = check_snapshot(_snapshot_view(str(gone)))
    assert "screenshot is missing" in missing
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    shot.chmod(0)
    try:
        blocked = check_snapshot(_snapshot_view(str(shot)))
        assert "screenshot is unreadable" in blocked
    finally:
        shot.chmod(0o644)
    shot.chmod(0o644)
    assert screenshot_state(shot) == "present"


def _snapshot_view(path: str):
    return type(
        "Snap",
        (),
        {"platform": "2gis", "screenshot_path": path, "fields": {}, "field_sources": {}},
    )()


def _catalog(root: Path, platform: str, html: str) -> None:
    folder = root / platform
    folder.mkdir(parents=True)
    (folder / "page-1.html").write_text(html, encoding="utf-8")
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(
            {"city": "Алматы", "category": SALON, "starts": {platform: f"{platform}/page-1.html"}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def factory(migrated_engine):
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def test_missing_and_unreadable_screenshots_do_not_abort_saved_collection(factory, tmp_path: Path) -> None:
    html = _page("yandex", "91616017", "https://yandex.kz/maps/org/e1017/91616017", "E1017 Salon", "улица Абая, 17", "8 701 000 10 17")
    missing_root = tmp_path / "missing"
    _catalog(missing_root, "yandex", html)
    first = run_discovery(
        factory,
        city="Алматы",
        category=SALON,
        platforms=("yandex",),
        limit=1,
        out=tmp_path / "missing.xlsx",
        source_mode="manual",
        root=missing_root,
    )
    _assert_finished_without_a_dead_screenshot(factory, first.batch_id, "missing")

    unreadable_root = tmp_path / "unreadable"
    other = _page("yandex", "91616018", "https://yandex.kz/maps/org/e1017b/91616018", "E1017 Second", "улица Абая, 18", "8 701 000 10 18")
    _catalog(unreadable_root, "yandex", other)
    pages = unreadable_root / "pages"
    pages.mkdir()
    shot = pages / "blank.png"
    shot.write_bytes(PNG)
    shot.chmod(0)
    try:
        second = run_discovery(
            factory,
            city="Алматы",
            category=SALON,
            platforms=("yandex",),
            limit=1,
            out=tmp_path / "unreadable.xlsx",
            source_mode="manual",
            root=unreadable_root,
        )
        _assert_finished_without_a_dead_screenshot(factory, second.batch_id, "unreadable")
    finally:
        shot.chmod(0o644)


def _assert_finished_without_a_dead_screenshot(factory, batch_id: int, expected: str) -> None:
    with factory() as session:
        batch = session.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "completed"
        assert (batch.progress or {})["optional_screenshot"]["status"] == expected
        assert (batch.progress or {})["optional_screenshot"]["required_for_verification"] is False
        companies = session.scalars(select(Company)).all()
        assert companies
        for snapshot in session.scalars(select(CardSnapshot)).all():
            path = snapshot.screenshot_path
            if path:
                assert Path(path).is_file()
            marker = (snapshot.field_sources or {}).get("_collection") or {}
            if not path:
                assert marker.get("screenshot_status") in {"missing", "unreadable", "present"}


@pytest.fixture
def client(migrated_engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("ZHOL_ADMIN_FILES", str(tmp_path / "admin-files"))
    monkeypatch.delenv("ZHOL_SCOUT_LIVE", raising=False)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    with TestClient(app) as test_client:
        yield test_client, factory


def _snap(company_id: int, platform: str, address: str, **extra: object) -> CardSnapshot:
    return CardSnapshot(
        company_id=company_id,
        platform=platform,
        match_status=str(extra.pop("match_status", "found")),
        url=str(extra.pop("url", f"https://example.test/{platform}")),
        fields={"address": address},
        field_sources={},
        screenshot_path=None,
        purpose="prospect",
        collected_at=extra.pop("collected_at", WHEN),
        collected_by="manual_import",
        status=str(extra.pop("status", "verified")),
        fail_reason=None,
    )


def test_header_address_covers_each_platform_and_a_conflict(client) -> None:
    test_client, factory = client
    with factory() as db, db.begin():
        twogis = Company(id_2gis="e1017-2gis", name="Только 2ГИС", city="Алматы", niche="салон", source="e1017")
        yandex = Company(name="Только Яндекс", city="Алматы", niche="салон", source="e1017")
        google = Company(name="Только Google", city="Алматы", niche="салон", source="e1017")
        conflict = Company(id_2gis="e1017-conflict", name="Две площадки", city="Алматы", niche="салон", source="e1017")
        db.add_all([twogis, yandex, google, conflict])
        db.flush()
        db.add_all(
            [
                _snap(twogis.id, "2gis", "улица 2ГИС, 1", url="https://2gis.kz/almaty/firm/e1017-2gis"),
                _snap(
                    yandex.id,
                    "yandex",
                    "улица Яндекса, 2",
                    status="collected",
                    url="https://yandex.kz/maps/org/e1017/2",
                ),
                _snap(
                    google.id,
                    "google",
                    "улица Google, 3",
                    status="verified",
                    url="https://www.google.com/maps/place/e1017",
                ),
                _snap(conflict.id, "2gis", "улица А, 1", url="https://2gis.kz/almaty/firm/e1017-conflict"),
                _snap(conflict.id, "yandex", "улица Б, 2", url="https://yandex.kz/maps/org/conflict/2"),
            ]
        )
        ids = {"2gis": twogis.id, "yandex": yandex.id, "google": google.id, "conflict": conflict.id}
        stored = {row.id: (row.name, row.city, row.niche) for row in (twogis, yandex, google, conflict)}

    twogis_body = test_client.get(f"/api/v1/admin/companies/{ids['2gis']}", headers=HEADERS).json()
    assert twogis_body["header"]["address"] == "улица 2ГИС, 1"
    assert twogis_body["header"]["address_verified"] is True
    assert twogis_body["header"]["address_provenance"]["platform"] == "2gis"
    assert "проверенного снимка 2ГИС" in twogis_body["header"]["address_note"]

    yandex_body = test_client.get(f"/api/v1/admin/companies/{ids['yandex']}", headers=HEADERS).json()
    assert yandex_body["header"]["address"] == "улица Яндекса, 2"
    assert yandex_body["header"]["address_verified"] is False
    assert "не проверен" in yandex_body["header"]["address_note"]
    yandex_card = next(item for item in yandex_body["cards"] if item["platform"] == "yandex")
    assert next(item["text"] for item in yandex_card["facts"] if item["label"] == "Адрес") == "улица Яндекса, 2"
    twogis_card = next(item for item in yandex_body["cards"] if item["platform"] == "2gis")
    assert next(item["text"] for item in twogis_card["facts"] if item["label"] == "Адрес") == "Не проверялось"

    google_body = test_client.get(f"/api/v1/admin/companies/{ids['google']}", headers=HEADERS).json()
    assert google_body["header"]["address"] == "улица Google, 3"
    assert google_body["header"]["address_verified"] is True
    assert google_body["header"]["address_provenance"]["platform"] == "google"

    conflict_body = test_client.get(f"/api/v1/admin/companies/{ids['conflict']}", headers=HEADERS).json()
    assert conflict_body["header"]["address"] == "Нет данных"
    assert conflict_body["header"]["address_provenance"] is None
    assert "разных адрес" in conflict_body["header"]["address_note"]
    by_platform = {item["platform"]: item for item in conflict_body["cards"]}
    assert next(item["text"] for item in by_platform["2gis"]["facts"] if item["label"] == "Адрес") == "улица А, 1"
    assert next(item["text"] for item in by_platform["yandex"]["facts"] if item["label"] == "Адрес") == "улица Б, 2"

    listed = test_client.get("/api/v1/admin/companies", headers=HEADERS, params={"q": "Только Яндекс"}).json()
    row = next(item for item in listed["items"] if item["id"] == ids["yandex"])
    assert row["address"] == "улица Яндекса, 2"
    assert row["address_verified"] is False

    with factory() as db:
        for company_id, fields in stored.items():
            company = db.get(Company, company_id)
            assert company is not None
            assert (company.name, company.city, company.niche) == fields


def test_saved_page_import_runs_on_the_existing_path_without_a_screenshot(client, tmp_path: Path) -> None:
    test_client, factory = client
    yandex = _page("yandex", "91616017", "https://yandex.kz/maps/org/e1017/91616017", "Север Импорт", "проспект Яндекса, 4", "8 701 111 10 17")
    google = _page(
        "google",
        "ChIJe1017google",
        "https://www.google.com/maps/place/E1017/data=!1sChIJe1017google",
        "Юг Импорт",
        "проспект Гугла, 5",
        "8 702 222 10 17",
    )
    twogis = _page("2gis", "70000001000001017", "https://2gis.kz/almaty/firm/70000001000001017", "Центр Импорт", "проспект Двагиса, 6", "8 703 333 10 17")
    denied = test_client.post(
        "/api/v1/admin/scout/saved-pages",
        headers=HEADERS,
        data={"city": "Алматы", "category": SALON, "platforms": "yandex", "limit": "1", "catalog": "/etc/passwd"},
        files={"file_yandex": ("note.txt", b"not html", "text/plain")},
    )
    assert denied.status_code == 400
    assert "/etc/passwd" not in denied.text
    assert str(tmp_path) not in denied.text

    started = test_client.post(
        "/api/v1/admin/scout/saved-pages",
        headers=HEADERS,
        data={"city": "Алматы", "category": SALON, "platforms": "2gis,yandex,google", "limit": "5"},
        files={
            "file_2gis": ("page.html", twogis.encode(), "text/html"),
            "file_yandex": ("page.html", yandex.encode(), "text/html"),
            "file_google": ("page.html", google.encode(), "text/html"),
        },
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["live_collection"] is False
    assert body["source_mode"] == "manual"
    assert body["data_source"] == "saved_pages"
    assert body["import_label"] == "Импорт сохранённых страниц"
    assert body["platforms"] == ["2gis", "yandex", "google"]
    assert body["status"] == "queued"
    assert str(tmp_path) not in started.text
    assert "blank.png" not in started.text
    with factory() as db:
        before_orders = db.scalar(select(func.count()).select_from(Order))
        before_audits = db.scalar(select(func.count()).select_from(Audit))
        job = db.get(Job, body["id"])
        assert job is not None
        payload = dict(job.payload or {})
        job.status = "running"
        job.attempts = 1
        db.commit()
    handle_scout_batch(factory, payload)
    shown = test_client.get("/api/v1/admin/companies", headers=HEADERS, params={"q": "Импорт"}).json()
    row = next(item for item in shown["items"] if item["name"] == "Север Импорт")
    assert row["address"] == "проспект Яндекса, 4"
    assert row["address_verified"] is False
    google_rows = test_client.get("/api/v1/admin/companies", headers=HEADERS, params={"q": "Юг Импорт"}).json()
    google_row = next(item for item in google_rows["items"] if item["name"] == "Юг Импорт")
    assert google_row["address"] == "проспект Гугла, 5"
    with factory() as db:
        batch_id = db.scalar(select(CollectionBatch.id))
        batch = db.get(CollectionBatch, batch_id)
        assert batch is not None
        assert batch.status == "completed"
        assert batch.source_mode == "manual"
        assert (batch.progress or {})["optional_screenshot"]["status"] == "missing"
        names = {row.name for row in db.scalars(select(Company)).all()}
        assert {"Север Импорт", "Юг Импорт", "Центр Импорт"} <= names
        for snapshot in db.scalars(select(CardSnapshot)).all():
            if snapshot.screenshot_path:
                assert Path(snapshot.screenshot_path).is_file()
        assert db.scalar(select(func.count()).select_from(Order)) == before_orders
        assert db.scalar(select(func.count()).select_from(Audit)) == before_audits
        finished_id = batch.id
    detail_response = test_client.get(f"/api/v1/admin/scout/batches/{finished_id}", headers=HEADERS)
    assert str(tmp_path) not in detail_response.text
    detail = detail_response.json()
    assert detail["optional_screenshot"] == "missing"
    assert "Импорт сохранённых страниц" in detail["note"]
