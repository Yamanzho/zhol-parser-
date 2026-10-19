from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class Image:
    id: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class ProviderReply:
    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None


class Provider(Protocol):
    def complete(
        self,
        *,
        prompt: str,
        model: str,
        system: str,
        user: str,
        temperature: float,
        images: Sequence[Image],
        json_schema: dict[str, Any],
    ) -> ProviderReply: ...


def _b64(image: Image) -> str:
    return base64.b64encode(image.data).decode("ascii")


class AnthropicProvider:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, *, prompt, model, system, user, temperature, images, json_schema) -> ProviderReply:
        content: list[dict[str, Any]] = []
        for image in images:
            content.append({"type": "text", "text": f"image id: {image.id}"})
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": _b64(image)}}
            )
        content.append({"type": "text", "text": user})
        # The pinned anthropic SDK's messages.create has no temperature parameter.
        response = self._get().messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return ProviderReply(text, response.usage.input_tokens, response.usage.output_tokens)


class OpenAIProvider:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.OpenAI()
        return self._client

    def complete(self, *, prompt, model, system, user, temperature, images, json_schema) -> ProviderReply:
        parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images:
            parts.append({"type": "text", "text": f"image id: {image.id}"})
            parts.append({"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{_b64(image)}"}})
        response = self._get().chat.completions.create(
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": parts}],
        )
        usage = response.usage
        return ProviderReply(
            response.choices[0].message.content or "",
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )


class GoogleProvider:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _get(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
        return self._client

    def complete(self, *, prompt, model, system, user, temperature, images, json_schema) -> ProviderReply:
        from google.genai import types

        contents: list[Any] = [types.Part.from_text(text=user)]
        for image in images:
            contents.append(types.Part.from_text(text=f"image id: {image.id}"))
            contents.append(types.Part.from_bytes(data=image.data, mime_type=image.media_type))
        response = self._get().models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                response_mime_type="application/json",
            ),
        )
        usage = response.usage_metadata
        return ProviderReply(
            response.text or "",
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
        )


class FakeProviderError(RuntimeError):
    pass


@dataclass
class FakeProvider:
    """Offline provider. Replies are scripted per "<prompt>:<model>" and returned in order.

    A reply is a JSON object, {"raw": "<text>"} for a raw response, or {"error": "<text>"} for a failed call.
    """

    script: Mapping[str, Sequence[Any]]
    tokens_in: int = 100
    tokens_out: int = 50
    calls: list[dict[str, Any]] = field(default_factory=list)
    _positions: dict[str, int] = field(default_factory=dict)

    def complete(self, *, prompt, model, system, user, temperature, images, json_schema) -> ProviderReply:
        key = f"{prompt}:{model}"
        self.calls.append({"key": key, "system": system, "user": user, "images": [i.id for i in images]})
        replies = self.script.get(key, ())
        position = self._positions.get(key, 0)
        if position >= len(replies):
            raise FakeProviderError(f"no scripted reply for {key} (call {position + 1})")
        self._positions[key] = position + 1
        reply = replies[position]
        if isinstance(reply, dict) and set(reply) == {"error"}:
            raise FakeProviderError(str(reply["error"]))
        if isinstance(reply, dict) and set(reply) == {"raw"}:
            return ProviderReply(str(reply["raw"]), self.tokens_in, self.tokens_out)
        return ProviderReply(json.dumps(reply, ensure_ascii=False), self.tokens_in, self.tokens_out)


def default_providers() -> dict[str, Provider]:
    return {"anthropic": AnthropicProvider(), "openai": OpenAIProvider(), "google": GoogleProvider()}
