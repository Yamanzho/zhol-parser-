from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_platforms(path: str | Path | None = None) -> dict[str, Any]:
    file = Path(path) if path is not None else ROOT / "config" / "platforms.yaml"
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{file} must be a mapping")
    markers = data.get("markers") or {}
    for key in ("page_attr", "platform_attr", "field_attr", "candidate_attr", "external_id_attr"):
        if not markers.get(key):
            raise ValueError(f"{file} is missing markers.{key}")
    platforms = data.get("platforms") or {}
    for name in ("2gis", "yandex", "google"):
        spec = platforms.get(name)
        if not isinstance(spec, dict):
            raise ValueError(f"{file} is missing platforms.{name}")
        if not isinstance(spec.get("code_fields"), list) or not isinstance(spec.get("model_fields"), list):
            raise ValueError(f"{file} platforms.{name} needs code_fields and model_fields")
    if float(data.get("rate_limit_seconds") or 0) <= 0:
        raise ValueError(f"{file} rate_limit_seconds must be positive")
    return data
