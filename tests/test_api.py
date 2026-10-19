from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhol.api.main import app
from zhol.llm.config import ConfigError

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"


def test_health_with_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.setenv("ZHOL_CONFIG_DIR", str(STUB))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "placeholder")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://zhol:secret@127.0.0.1:1/zhol_test")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_fails_closed_on_production_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    monkeypatch.delenv("ZHOL_CONFIG_DIR", raising=False)
    with pytest.raises(ConfigError, match="not usable"):
        with TestClient(app):
            pass
