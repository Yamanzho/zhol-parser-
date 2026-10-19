"""Editable listing fields from config/platforms.yaml.

A field that is not listed for a platform is not written. Form submission stays off.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zhol.browser.config import load_platforms

SHAPES = frozenset({"text", "phones", "services", "file", "content"})
FIELD_KEYS = frozenset(
    {
        "nap",
        "shape",
        "profile",
        "source",
        "file_kind",
        "content_kind",
        "required_on_create",
        "snapshot_key",
        "on_update",
    }
)
ON_UPDATE = frozenset({"set", "skip", "when_empty"})
PAUSE_KINDS = ("login", "captcha", "sms")


@dataclass(frozen=True)
class FieldSpec:
    field: str
    nap: bool
    shape: str
    profile: str | None
    source: str
    file_kind: str | None
    content_kind: str | None
    required_on_create: bool
    snapshot_key: str | None
    on_update: str


@dataclass(frozen=True)
class CabinetSpec:
    platform: str
    submit: bool
    create: bool
    pause: dict[str, tuple[str, ...]]
    fields: dict[str, FieldSpec]
    read_only: frozenset[str]


def load_cabinet(platform: str, platforms: Mapping[str, Any] | None = None) -> CabinetSpec:
    if platforms is None:
        raw = load_platforms().get("platforms") or {}
    else:
        raw = platforms
    spec = raw.get(platform)
    if not isinstance(spec, Mapping):
        raise ValueError(f"platforms.{platform} is missing")
    return cabinet_from_mapping(platform, spec)


def cabinet_from_mapping(platform: str, spec: Mapping[str, Any]) -> CabinetSpec:
    cabinet = spec.get("cabinet")
    if not isinstance(cabinet, Mapping):
        raise ValueError(f"{platform} cabinet settings are missing")
    if cabinet.get("submit") is not False:
        raise ValueError(f"{platform} cabinet submit must be false in this batch")
    if cabinet.get("create") is not True:
        raise ValueError(f"{platform} cabinet create must be true")
    pause_raw = cabinet.get("pause")
    if not isinstance(pause_raw, Mapping):
        raise ValueError(f"{platform} cabinet pause phrases are missing")
    pause: dict[str, tuple[str, ...]] = {}
    for kind in PAUSE_KINDS:
        phrases = pause_raw.get(kind)
        if not isinstance(phrases, list) or not phrases or not all(isinstance(item, str) and item.strip() for item in phrases):
            raise ValueError(f"{platform} cabinet pause.{kind} needs phrases")
        pause[kind] = tuple(phrases)
    read_only_raw = cabinet.get("read_only")
    if not isinstance(read_only_raw, list) or not all(isinstance(item, str) and item for item in read_only_raw):
        raise ValueError(f"{platform} cabinet read_only must be a list of fields")
    read_only = frozenset(read_only_raw)
    editable = spec.get("editable_fields")
    if not isinstance(editable, Mapping) or not editable:
        raise ValueError(f"{platform} editable_fields are missing")
    fields: dict[str, FieldSpec] = {}
    for name, body in editable.items():
        if not isinstance(name, str) or not isinstance(body, Mapping):
            raise ValueError(f"{platform} editable field {name} is invalid")
        unknown = set(body) - FIELD_KEYS
        if unknown:
            raise ValueError(f"{platform}.{name} has unknown keys: {', '.join(sorted(unknown))}")
        fields[name] = _field(platform, name, body)
    overlap = set(fields) & set(read_only)
    if overlap:
        raise ValueError(f"{platform} lists writable fields as read-only: {', '.join(sorted(overlap))}")
    return CabinetSpec(
        platform=platform,
        submit=False,
        create=True,
        pause=pause,
        fields=fields,
        read_only=read_only,
    )


def _field(platform: str, name: str, body: Mapping[str, Any]) -> FieldSpec:
    shape = body.get("shape")
    if shape not in SHAPES:
        raise ValueError(f"{platform}.{name} shape is invalid")
    profile = body.get("profile")
    source = body.get("source")
    file_kind = body.get("file_kind")
    content_kind = body.get("content_kind")
    if shape in {"text", "phones", "services"}:
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError(f"{platform}.{name} needs a profile path")
        source = "profile"
    elif shape == "file":
        source = "client_file"
        if not isinstance(file_kind, str) or not file_kind.strip():
            raise ValueError(f"{platform}.{name} needs a file kind")
    else:
        source = "content_item"
        if not isinstance(content_kind, str) or not content_kind.strip():
            raise ValueError(f"{platform}.{name} needs a content kind")
    on_update = body.get("on_update")
    if on_update is None:
        on_update = "when_empty" if body.get("snapshot_key") == "photos_count" else "skip" if shape == "file" else "set"
    if on_update not in ON_UPDATE:
        raise ValueError(f"{platform}.{name} on_update is invalid")
    snapshot_key = body.get("snapshot_key")
    if snapshot_key is not None and not isinstance(snapshot_key, str):
        raise ValueError(f"{platform}.{name} snapshot_key must be text")
    return FieldSpec(
        field=name,
        nap=bool(body.get("nap")),
        shape=str(shape),
        profile=profile if isinstance(profile, str) else None,
        source=str(source),
        file_kind=file_kind if isinstance(file_kind, str) else None,
        content_kind=content_kind if isinstance(content_kind, str) else None,
        required_on_create=bool(body.get("required_on_create")),
        snapshot_key=snapshot_key if isinstance(snapshot_key, str) else None,
        on_update=str(on_update),
    )
