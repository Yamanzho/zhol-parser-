from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from zhol.llm.config import ConfigError, check_startup, load_config, validate_config
from zhol.llm.gateway import Gateway, LlmCallError, LlmError, LlmResult, LlmSchemaError
from zhol.llm.prompts import PromptError
from zhol.llm.providers import FakeProvider, Image

_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        from zhol.db.base import get_session_factory

        config = check_startup()
        factory = get_session_factory() if os.environ.get("DATABASE_URL") else None
        _gateway = Gateway(config, session_factory=factory)
    return _gateway


def set_gateway(gateway: Gateway | None) -> None:
    global _gateway
    _gateway = gateway


def run(prompt: str, input: Mapping[str, Any], images: Sequence[Image] = (), *, model: str | None = None) -> LlmResult:
    return get_gateway().run(prompt, input, images, model=model)


__all__ = [
    "ConfigError",
    "FakeProvider",
    "Gateway",
    "Image",
    "LlmCallError",
    "LlmError",
    "LlmResult",
    "LlmSchemaError",
    "PromptError",
    "check_startup",
    "get_gateway",
    "load_config",
    "run",
    "set_gateway",
    "validate_config",
]
