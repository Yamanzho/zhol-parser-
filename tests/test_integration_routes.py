"""HTTP contract for the versioned API. The database session is replaced."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhol.api import integration as integration_api
from zhol.integration.schema import export_bytes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ZHOL_ADMIN_TOKEN", "test-token")
    app = FastAPI()
    integration_api.register(app)

    def empty() -> Iterator[object]:
        yield object()

    app.dependency_overrides[integration_api._read_session] = empty
    app.dependency_overrides[integration_api._write_session] = empty
    with TestClient(app) as test_client:
        yield test_client


def test_missing_token_is_a_stable_error(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/1")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "токен" in body["error"]["message"].casefold() or "аутентиф" in body["error"]["message"].casefold()


def test_validation_error_uses_the_same_shape(client: TestClient) -> None:
    response = client.post(
        "/api/v1/jobs",
        headers={"Authorization": "Bearer test-token"},
        json={"platform": "2gis", "city": "Алматы"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_job_routes_delegate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    document = {
        "id": 4,
        "status": "queued",
        "platform": "2gis",
        "platform_label": "2ГИС",
        "city": "Алматы",
        "query": "салон красоты",
        "limit": 2,
        "collected": 0,
        "progress": None,
        "message": "Задание в очереди.",
        "error": None,
        "duplicate": False,
        "batch_id": None,
        "controls": {"pause": True, "resume": False, "stop": True},
        "results_url": "/api/v1/jobs/4/results",
        "export_url": "/api/v1/jobs/4/export",
    }
    monkeypatch.setattr(integration_api.live, "create_job", lambda _session, _spec: (document, False))
    monkeypatch.setattr(integration_api.live, "job_view", lambda _session, job_id: {**document, "id": job_id})
    monkeypatch.setattr(integration_api.live, "control_job", lambda _session, job_id, action: {**document, "id": job_id, "status": action})
    monkeypatch.setattr(
        integration_api.live,
        "iter_job_records",
        lambda _session, _job_id: iter(
            [
                {
                    "name": "Салон",
                    "phone": "+7 701 000 00 11",
                    "address": "Абая 10",
                    "category": "салон красоты",
                    "city": "Алматы",
                    "platform_label": "2ГИС",
                    "source_label": "2ГИС",
                }
            ]
        ),
    )
    headers = {"Authorization": "Bearer test-token"}
    created = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"platform": "2gis", "city": "Алматы", "query": "салон красоты", "limit": 2, "idempotency_key": "job-1"},
    )
    assert created.status_code == 201
    assert created.json()["results_url"] == "/api/v1/jobs/4/results"
    paused = client.post("/api/v1/jobs/4/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()["status"] == "pause"
    exported = client.get("/api/v1/jobs/4/export?format=csv", headers=headers)
    assert exported.status_code == 200
    assert "Салон" in exported.text


def test_export_csv_and_json_round_trip() -> None:
    rows = [
        {
            "name": "Салон",
            "phone": None,
            "address": "Абая 10",
            "category": "салон красоты",
            "city": "Алматы",
            "platform_label": "2ГИС",
            "source_label": "2ГИС",
        }
    ]
    csv_bytes, media, name = export_bytes("csv", rows, notes=("пояснение",))
    assert media.startswith("text/csv")
    assert name.endswith(".csv")
    assert "Салон" in csv_bytes.decode("utf-8-sig")
    raw, _media, json_name = export_bytes("json", rows, notes=("пояснение",))
    assert json_name.endswith(".json")
    assert "пояснение" in raw.decode("utf-8")


def test_page_offset_is_not_capped_at_the_old_admin_limit() -> None:
    from zhol.integration.schema import parse_page

    limit, offset = parse_page(100, 20_000)
    assert limit == 100
    assert offset == 20_000
