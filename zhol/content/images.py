"""Provider-independent illustrations. This batch ships an offline provider only."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from zhol.guards import refuse_fabricated_scene

ILLUSTRATIVE_KINDS = frozenset({"cover", "promo_banner"})
ORIGIN = "ai_illustration"
# 1x1 PNG. It is a labeled illustration file, not a photograph of a place or a person.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class IllustrationRequest:
    kind: str
    order_id: int
    prompt: str
    width: int = 1
    height: int = 1


@dataclass(frozen=True)
class GeneratedImage:
    origin: str
    illustrative: bool
    media_type: str
    data: bytes
    label: str


class IllustrationProvider(Protocol):
    def generate(self, request: IllustrationRequest) -> GeneratedImage: ...


@dataclass
class OfflineIllustrationProvider:
    """Writes a labeled PNG and does not call a network API."""

    calls: list[IllustrationRequest] = field(default_factory=list)

    def generate(self, request: IllustrationRequest) -> GeneratedImage:
        refuse_fabricated_scene(request.kind)
        if request.kind not in ILLUSTRATIVE_KINDS:
            raise PermissionError("only an abstract cover or promo illustration can be generated")
        self.calls.append(request)
        return GeneratedImage(
            origin=ORIGIN,
            illustrative=True,
            media_type="image/png",
            data=_PNG,
            label=ORIGIN,
        )


def write_illustration(directory: Path, order_id: int, kind: str, image: GeneratedImage) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"order-{order_id}-{kind}.png"
    path.write_bytes(image.data)
    note = directory / f"order-{order_id}-{kind}.origin.txt"
    note.write_text(f"{image.label}\n", encoding="utf-8")
    return str(path)


def suitable(kind: str, kinds: Sequence[str] = ILLUSTRATIVE_KINDS) -> bool:
    return kind in kinds
