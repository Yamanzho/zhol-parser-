from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
_SECTION = re.compile(r"^## (system|user|schema)[ \t]*$", re.M)
_SCHEMA = re.compile(r"```json\s*\n(.*?)\n```", re.S)
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    agent: str
    temperature: float
    system: str
    user: str
    schema: dict[str, Any]


@lru_cache(maxsize=64)
def load_prompt(path: Path) -> Prompt:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptError(f"prompt file {path} is missing") from None
    match = _FRONT.match(text)
    if match is None:
        raise PromptError(f"{path}: front matter is missing")
    front = yaml.safe_load(match.group(1)) or {}
    parts = _SECTION.split(match.group(2))
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    missing = {"system", "user", "schema"} - sections.keys()
    if missing:
        raise PromptError(f"{path}: sections missing: {sorted(missing)}")
    schema_match = _SCHEMA.search(sections["schema"])
    if schema_match is None:
        raise PromptError(f"{path}: schema has no ```json block")
    try:
        schema = json.loads(schema_match.group(1))
    except json.JSONDecodeError as exc:
        raise PromptError(f"{path}: schema is not valid JSON: {exc}") from None
    for key in ("name", "version", "temperature"):
        if key not in front:
            raise PromptError(f"{path}: front matter has no {key!r}")
    return Prompt(
        name=str(front["name"]),
        version=str(front["version"]),
        agent=str(front.get("agent", "")),
        temperature=float(front["temperature"]),
        system=sections["system"],
        user=sections["user"],
        schema=schema,
    )


def variables(template: str) -> set[str]:
    return set(_VARIABLE.findall(template))


def _value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render(template: str, values: dict[str, Any]) -> str:
    missing = variables(template) - values.keys()
    if missing:
        raise PromptError(f"prompt variables missing: {sorted(missing)}")
    return _VARIABLE.sub(lambda m: _value(values[m.group(1)]), template)
