"""Small client for the versioned collection API. Uses only the standard library."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from zhol.integration.schema import IntegrationError


def create_job(
    origin: str,
    token: str,
    *,
    platform: str,
    city: str,
    query: str,
    limit: int = 20,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"platform": platform, "city": city, "query": query, "limit": limit}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    return _send(origin, token, "POST", "/api/v1/jobs", body)


def job_status(origin: str, token: str, job_id: int) -> dict[str, Any]:
    return _send(origin, token, "GET", f"/api/v1/jobs/{job_id}")


def job_results(origin: str, token: str, job_id: int, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return _send(origin, token, "GET", f"/api/v1/jobs/{job_id}/results?limit={limit}&offset={offset}")


def export_job(origin: str, token: str, job_id: int, fmt: str, out: Path) -> None:
    payload = _raw(origin, token, "GET", f"/api/v1/jobs/{job_id}/export?format={fmt}")
    out.write_bytes(payload)


def _send(origin: str, token: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _raw(origin, token, method, path, body)
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise IntegrationError(502, "rejected", "Ответ API не является объектом JSON.")
    return parsed


def _raw(origin: str, token: str, method: str, path: str, body: dict[str, Any] | None = None) -> bytes:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(origin.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        message = detail
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                message = str(parsed["error"].get("message") or detail)
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise IntegrationError(exc.code, "rejected", str(message)) from exc
