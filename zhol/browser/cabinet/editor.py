"""Owner-cabinet editor controls from config/platforms.yaml.

Accessible names live in the config. The prepare step does not invent selectors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zhol.browser.config import load_platforms
from zhol.fulfillment.adapters.base import CabinetAdapter
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter
from zhol.fulfillment.adapters.twogis import TwoGisAdapter
from zhol.fulfillment.adapters.yandex import YandexBusinessAdapter

FILLABLE = frozenset({"text", "phones", "services"})
ADAPTERS = {
    "2gis": TwoGisAdapter,
    "yandex": YandexBusinessAdapter,
    "google": GoogleBusinessAdapter,
}


@dataclass(frozen=True)
class ControlSpec:
    field: str
    role: str
    name: str


@dataclass(frozen=True)
class EditorSpec:
    organization_field: str
    access_field: str
    access_allowed: tuple[str, ...]
    access_denied: tuple[str, ...]
    submit_names: tuple[str, ...]
    fields: dict[str, ControlSpec]


@dataclass(frozen=True)
class CreateFormSpec:
    listing_state_field: str
    absence_phrases: tuple[str, ...]
    found_phrases: tuple[str, ...]
    ownership_phrases: tuple[str, ...]
    editor: EditorSpec


CREATE_SUBMIT = ("Создать", "Create", "Сохранить", "Save", "Опубликовать", "Publish", "Отправить", "Submit")


def load_create_form(platform: str, source: Mapping[str, Any] | None = None) -> CreateFormSpec:
    raw = load_platforms() if source is None else source
    body = _platform_body(platform, raw, source is not None)
    if not isinstance(body, Mapping):
        raise ValueError(f"{platform} create form settings are missing")
    cabinet = body.get("cabinet")
    if not isinstance(cabinet, Mapping):
        raise ValueError(f"{platform} create form settings are missing")
    form = cabinet.get("create_form")
    if not isinstance(form, Mapping):
        raise ValueError(f"{platform} create form settings are missing")
    adapter_cls = ADAPTERS.get(platform)
    if adapter_cls is None:
        raise ValueError(f"{platform} create form settings are missing")
    return create_form_from_mapping(platform, form, adapter_cls())


def create_form_from_mapping(platform: str, form: Mapping[str, Any], adapter: CabinetAdapter) -> CreateFormSpec:
    editor = editor_from_mapping(platform, form, adapter)
    missing = [name for name in CREATE_SUBMIT if name not in editor.submit_names]
    if missing:
        raise ValueError(f"{platform} create form must refuse {', '.join(missing)}")
    return CreateFormSpec(
        listing_state_field=_token(platform, form.get("listing_state_field"), "listing_state_field"),
        absence_phrases=_phrases(platform, form.get("absence_phrases"), "absence_phrases"),
        found_phrases=_phrases(platform, form.get("found_phrases"), "found_phrases"),
        ownership_phrases=_phrases(platform, form.get("ownership_phrases"), "ownership_phrases"),
        editor=editor,
    )


def load_platform_editor(platform: str, source: Mapping[str, Any] | None = None) -> EditorSpec:
    raw = load_platforms() if source is None else source
    body = _platform_body(platform, raw, source is not None)
    if not isinstance(body, Mapping):
        raise ValueError(f"{platform} editor settings are missing")
    cabinet = body.get("cabinet")
    if not isinstance(cabinet, Mapping):
        raise ValueError(f"{platform} editor settings are missing")
    editor = cabinet.get("editor")
    if not isinstance(editor, Mapping):
        raise ValueError(f"{platform} editor settings are missing")
    adapter_cls = ADAPTERS.get(platform)
    if adapter_cls is None:
        raise ValueError(f"{platform} editor settings are missing")
    return editor_from_mapping(platform, editor, adapter_cls())


def editor_from_mapping(platform: str, editor: Mapping[str, Any], adapter: CabinetAdapter) -> EditorSpec:
    organization = _token(platform, editor.get("organization_field"), "organization_field")
    access = _token(platform, editor.get("access_field"), "access_field")
    allowed = _phrases(platform, editor.get("access_allowed"), "access_allowed")
    denied = _phrases(platform, editor.get("access_denied"), "access_denied")
    submit = _phrases(platform, editor.get("submit_names"), "submit_names")
    raw_fields = editor.get("fields")
    if not isinstance(raw_fields, Mapping) or not raw_fields:
        raise ValueError(f"{platform} editor fields are missing")
    fields: dict[str, ControlSpec] = {}
    for name, item in raw_fields.items():
        if not isinstance(name, str) or not isinstance(item, Mapping):
            raise ValueError(f"{platform} editor field {name} is invalid")
        role = item.get("role")
        label = item.get("name")
        if role != "textbox" or not isinstance(label, str) or not label.strip():
            raise ValueError(f"{platform} editor field {name} needs a textbox label")
        spec = adapter.spec.fields.get(name)
        if spec is None or spec.shape not in FILLABLE or name in adapter.spec.read_only or name in adapter.forbidden:
            raise ValueError(f"{platform} editor field {name} is not writable")
        fields[name] = ControlSpec(name, "textbox", label.strip())
    overlap = set(submit) & {item.name for item in fields.values()}
    if overlap:
        raise ValueError(f"{platform} editor submit names overlap a field")
    return EditorSpec(organization, access, allowed, denied, submit, fields)


def _platform_body(platform: str, raw: Mapping[str, Any], explicit: bool) -> Any:
    if explicit and "cabinet" in raw and "editable_fields" in raw:
        return raw
    table = raw.get("platforms") if isinstance(raw.get("platforms"), Mapping) else raw
    if isinstance(table, Mapping) and ("platforms" in raw or platform in table):
        return table.get(platform)
    return None


def _token(platform: str, value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{platform} editor {label} is missing")
    return value.strip()


def _phrases(platform: str, value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{platform} editor {label} needs phrases")
    return tuple(item.strip() for item in value)
