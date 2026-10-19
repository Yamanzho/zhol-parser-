"""Google Business Profile editor controls from config/platforms.yaml."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zhol.browser.cabinet.editor import EditorSpec, editor_from_mapping as _editor_from_mapping
from zhol.browser.cabinet.editor import load_platform_editor
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter


def load_editor(platforms: Mapping[str, Any] | None = None) -> EditorSpec:
    return load_platform_editor("google", platforms)


def editor_from_mapping(editor: Mapping[str, Any]) -> EditorSpec:
    return _editor_from_mapping("google", editor, GoogleBusinessAdapter())
