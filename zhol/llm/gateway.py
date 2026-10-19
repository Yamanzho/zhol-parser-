from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from jsonschema import validators
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import LlmCall
from zhol.llm.config import Config, ConfigError, ModelSpec
from zhol.llm.prompts import Prompt, load_prompt, render
from zhol.llm.providers import Image, Provider, ProviderReply, default_providers
from zhol.redact import safe_error
from zhol.timeutil import now

log = logging.getLogger(__name__)

_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n```\s*\Z", re.S)
FOUR_PLACES = Decimal("0.0001")


class LlmError(RuntimeError):
    pass


class LlmCallError(LlmError):
    pass


class LlmSchemaError(LlmError):
    pass


@dataclass(frozen=True)
class LlmResult:
    data: Any
    model: str
    prompt_name: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal | None


def parse_json(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    match = _FENCE.match(text)
    body = match.group(1) if match else text
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"response is not JSON: {exc.msg} at position {exc.pos}"
    error = schema_error(data, schema)
    return (data, None) if error is None else (None, error)


def schema_error(data: Any, schema: dict[str, Any]) -> str | None:
    validator = validators.validator_for(schema)(schema)
    error = next(iter(sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))), None)
    if error is None:
        return None
    path = "/".join(str(p) for p in error.absolute_path) or "(root)"
    return f"{path}: {error.message[:300]}"


def _cost(spec: ModelSpec, reply: ProviderReply) -> Decimal | None:
    if spec.usd_per_mtok_in is None or spec.usd_per_mtok_out is None:
        return None
    if reply.tokens_in is None or reply.tokens_out is None:
        return None
    usd = (Decimal(reply.tokens_in) * Decimal(str(spec.usd_per_mtok_in)) + Decimal(reply.tokens_out) * Decimal(str(spec.usd_per_mtok_out))) / Decimal(1_000_000)
    return usd.quantize(FOUR_PLACES)


class Gateway:
    def __init__(
        self,
        config: Config,
        providers: Mapping[str, Provider] | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self._providers = dict(providers) if providers is not None else None

    def _provider(self, spec: ModelSpec) -> Provider:
        if self._providers is None:
            self._providers = default_providers()
        provider = self._providers.get(spec.provider or "")
        if provider is None:
            raise ConfigError(f"no provider for model {spec.name!r} (family {spec.family!r})")
        return provider

    def prompt(self, name: str) -> Prompt:
        return load_prompt(self.config.prompt_path(name))

    def run(
        self,
        prompt: str,
        input: Mapping[str, Any],
        images: Sequence[Image] = (),
        *,
        model: str | None = None,
    ) -> LlmResult:
        spec_prompt = self.prompt(prompt)
        spec = self.config.model(model) if model else self.config.author_model(prompt)
        if images and not spec.vision:
            raise ConfigError(f"model {spec.name!r} cannot read images (vision: false)")
        system = render(spec_prompt.system, dict(input))
        user = render(spec_prompt.user, dict(input))
        provider = self._provider(spec)
        digest = hashlib.sha256()
        for part in (system, user, *(image.id for image in images)):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        input_hash = digest.hexdigest()

        text = user
        tokens_in = tokens_out = 0
        cost: Decimal | None = Decimal(0)
        error: str | None = None
        for _ in range(2):
            started = time.monotonic()
            try:
                reply = provider.complete(
                    prompt=prompt,
                    model=spec.name,
                    system=system,
                    user=text,
                    temperature=spec_prompt.temperature,
                    images=images,
                    json_schema=spec_prompt.schema,
                )
            except Exception as exc:
                latency = int((time.monotonic() - started) * 1000)
                self._record(spec_prompt, spec.name, input_hash, False, None, None, None, None, latency)
                log.warning("llm call failed prompt=%s model=%s error=%s", prompt, spec.name, type(exc).__name__)
                raise LlmCallError(f"{spec.provider} call failed for {prompt}: {safe_error(exc)}") from None
            latency = int((time.monotonic() - started) * 1000)
            data, error = parse_json(reply.text, spec_prompt.schema)
            call_cost = _cost(spec, reply)
            self._record(spec_prompt, spec.name, input_hash, True, error is None, reply.tokens_in, reply.tokens_out, call_cost, latency)
            tokens_in += reply.tokens_in or 0
            tokens_out += reply.tokens_out or 0
            cost = None if cost is None or call_cost is None else cost + call_cost
            log.info("llm call prompt=%s model=%s schema_ok=%s latency_ms=%s", prompt, spec.name, error is None, latency)
            if error is None:
                return LlmResult(data, spec.name, prompt, spec_prompt.version, tokens_in, tokens_out, cost)
            text = (
                f"{user}\n\nПредыдущий ответ не прошёл JSON-схему: {error}\n"
                "Ответь только JSON по схеме."
            )
        raise LlmSchemaError(f"{prompt}: response failed the JSON schema twice: {error}")

    def _record(
        self,
        prompt: Prompt,
        model: str,
        input_hash: str,
        ok: bool,
        schema_ok: bool | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cost: Decimal | None,
        latency_ms: int,
    ) -> None:
        if self.session_factory is None:
            return
        with self.session_factory() as session, session.begin():
            session.add(
                LlmCall(
                    prompt_name=prompt.name,
                    prompt_version=prompt.version,
                    model=model,
                    input_hash=input_hash,
                    ok=ok,
                    schema_ok=schema_ok,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    at=now(),
                )
            )
