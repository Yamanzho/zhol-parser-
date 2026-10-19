from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FAMILIES = ("claude", "gpt", "gemini")
PROVIDER_BY_FAMILY = {"claude": "anthropic", "gpt": "openai", "gemini": "google"}
API_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "gpt": "OPENAI_API_KEY", "gemini": "GOOGLE_API_KEY"}
LEVELS = ("single", "council")
MODES = ("double_read", "double_decision")
# Points and prompts that receive screenshots or images; their models must have vision.
IMAGE_POINTS = frozenset({"scout_fields", "applied", "content"})
IMAGE_PROMPTS = frozenset({"scout-extract"})
DEV_AUTHOR_FAMILIES = ("claude", "gpt")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    family: str
    vision: bool
    usd_per_mtok_in: float | None
    usd_per_mtok_out: float | None

    @property
    def provider(self) -> str | None:
        return PROVIDER_BY_FAMILY.get(self.family)


@dataclass(frozen=True)
class Config:
    root: Path
    models: dict[str, ModelSpec]
    authors: dict[str, str]
    usd_to_kzt: float | None
    policy: dict[str, Any]

    def model(self, name: str) -> ModelSpec:
        try:
            return self.models[name]
        except KeyError:
            raise ConfigError(f"model {name!r} is not in models.yaml") from None

    def author_model(self, prompt: str) -> ModelSpec:
        name = self.authors.get(prompt)
        if name is None:
            raise ConfigError(f"prompt {prompt!r} has no author model in models.yaml")
        return self.model(name)

    def resolve_member(self, member: str) -> ModelSpec:
        if member in self.models:
            return self.models[member]
        if member in FAMILIES:
            candidates = [m for m in self.models.values() if m.family == member]
            if len(candidates) == 1:
                return candidates[0]
            if not candidates:
                raise ConfigError(f"no model of family {member!r} in models.yaml")
            raise ConfigError(f"several models of family {member!r}; name the model in review_policy.yaml")
        raise ConfigError(f"reviewer {member!r} is neither a model nor a family")

    def weight(self, family: str) -> float:
        return float(self.policy["weights"][family])

    def point(self, name: str) -> dict[str, Any]:
        try:
            return self.policy["points"][name]
        except KeyError:
            raise ConfigError(f"review point {name!r} is not in review_policy.yaml") from None

    def prompt_path(self, name: str) -> Path:
        return self.root / "prompts" / f"{name}.md"

    def checklist_path(self, name: str) -> Path:
        return self.root / "council" / "checklists" / f"{name}.md"


def _yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} is missing") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping")
    return data


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def load_config(config_dir: str | Path | None = None, root: str | Path | None = None) -> Config:
    root_path = Path(root or os.environ.get("ZHOL_ROOT") or Path.cwd())
    directory = Path(config_dir or os.environ.get("ZHOL_CONFIG_DIR") or root_path / "config")
    raw = _yaml(directory / "models.yaml")
    policy = _yaml(directory / "review_policy.yaml")
    models: dict[str, ModelSpec] = {}
    for name, spec in (raw.get("models") or {}).items():
        spec = spec or {}
        models[str(name)] = ModelSpec(
            name=str(name),
            family=str(spec.get("family")),
            vision=spec.get("vision") is True,
            usd_per_mtok_in=_price(spec.get("usd_per_mtok_in")),
            usd_per_mtok_out=_price(spec.get("usd_per_mtok_out")),
        )
    return Config(
        root=root_path,
        models=models,
        authors={str(k): str(v) for k, v in (raw.get("authors") or {}).items()},
        usd_to_kzt=_price(raw.get("usd_to_kzt")),
        policy=policy,
    )


def _point_errors(cfg: Config, name: str, point: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    where = f"review point {name!r}"
    level = point.get("level")
    if level not in LEVELS:
        errors.append(f"{where}: level must be one of {LEVELS}")
    mode = point.get("mode")
    if mode is not None and mode not in MODES:
        errors.append(f"{where}: unknown mode {mode!r}")
    checklists = point.get("checklists") or ([point["checklist"]] if point.get("checklist") else [])
    if mode is None and not checklists:
        errors.append(f"{where}: no checklist")
    for checklist in checklists:
        if not cfg.checklist_path(checklist).is_file():
            errors.append(f"{where}: checklist file council/checklists/{checklist}.md is missing")
    members: list[ModelSpec] = []
    for member in point.get("members") or []:
        try:
            members.append(cfg.resolve_member(str(member)))
        except ConfigError as exc:
            errors.append(f"{where}: {exc}")
    if not point.get("members"):
        errors.append(f"{where}: no reviewers")
    if level == "council" and len(point.get("members") or []) < 2:
        errors.append(f"{where}: a council needs at least 2 reviewers")
    if point.get("checklists") and len(point["checklists"]) != len(point.get("members") or []):
        errors.append(f"{where}: checklists and members must pair one to one")
    author = point.get("author_family")
    if author == "from_flag":
        authors = DEV_AUTHOR_FAMILIES
    elif author == "none":
        authors = ()
    elif author in FAMILIES:
        authors = (author,)
    else:
        errors.append(f"{where}: author_family must be a family, none or from_flag")
        authors = ()
    resolved = len(members) == len(point.get("members") or [])
    for family in authors:
        if resolved and members and all(m.family == family for m in members):
            errors.append(f"{where}: every reviewer is from the author family {family!r}")
    if name in IMAGE_POINTS:
        for m in members:
            if not m.vision:
                errors.append(f"{where}: reviewer {m.name!r} must have vision: true")
    return errors


def validate_config(
    cfg: Config, env: Mapping[str, str] | None = None, *, require_credentials: bool = True
) -> list[str]:
    env = os.environ if env is None else env
    errors: list[str] = []
    for m in cfg.models.values():
        if m.family not in FAMILIES:
            errors.append(f"model {m.name!r}: family must be one of {FAMILIES}")
        if m.usd_per_mtok_in is None or m.usd_per_mtok_out is None:
            errors.append(f"model {m.name!r}: price is not set (decision R6)")
        elif m.usd_per_mtok_in < 0 or m.usd_per_mtok_out < 0:
            errors.append(f"model {m.name!r}: price must not be negative")
    if cfg.usd_to_kzt is None or cfg.usd_to_kzt <= 0:
        errors.append("usd_to_kzt must be a positive number")
    for prompt, model in cfg.authors.items():
        if not cfg.prompt_path(prompt).is_file():
            errors.append(f"author prompt {prompt!r}: prompts/{prompt}.md is missing")
        if model not in cfg.models:
            errors.append(f"author prompt {prompt!r}: model {model!r} is not in models.yaml")
        elif prompt in IMAGE_PROMPTS and not cfg.models[model].vision:
            errors.append(f"author prompt {prompt!r}: model {model!r} must have vision: true")
    weights = cfg.policy.get("weights") or {}
    for family in FAMILIES:
        if _price(weights.get(family)) is None:
            errors.append(f"review_policy.yaml: weight for {family!r} is missing")
    if not isinstance(cfg.policy.get("max_rounds"), int) or cfg.policy["max_rounds"] < 1:
        errors.append("review_policy.yaml: max_rounds must be a positive integer")
    if cfg.policy.get("council_model_claude") not in cfg.models:
        errors.append("review_policy.yaml: council_model_claude is not in models.yaml")
    points = cfg.policy.get("points") or {}
    if not points:
        errors.append("review_policy.yaml: no points")
    for name, point in points.items():
        errors.extend(_point_errors(cfg, str(name), point or {}))
    if require_credentials:
        for family in sorted({m.family for m in cfg.models.values() if m.family in FAMILIES}):
            if not env.get(API_KEY_ENV[family]):
                errors.append(f"{API_KEY_ENV[family]} is not set")
    return errors


def check_startup(
    config_dir: str | Path | None = None,
    root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    cfg = load_config(config_dir, root)
    errors = validate_config(cfg, env)
    if errors:
        raise ConfigError("configuration is not usable:\n- " + "\n- ".join(errors))
    return cfg
