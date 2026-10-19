from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from zhol.llm.config import ConfigError, check_startup, load_config, validate_config

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
KEYS = {"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "x", "GOOGLE_API_KEY": "x"}


def _copy_stub(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(STUB, target)
    return target


def _edit(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_stub_config_is_valid_offline() -> None:
    cfg = load_config(STUB, ROOT)
    assert validate_config(cfg, require_credentials=False) == []


def test_stub_config_needs_credentials() -> None:
    errors = validate_config(load_config(STUB, ROOT), env={})
    assert {"ANTHROPIC_API_KEY is not set", "OPENAI_API_KEY is not set", "GOOGLE_API_KEY is not set"} <= set(errors)
    assert validate_config(load_config(STUB, ROOT), env=KEYS) == []


def test_production_config_fails_closed() -> None:
    errors = validate_config(load_config(root=ROOT), env=KEYS)
    joined = "\n".join(errors)
    assert "claude-sonnet-5-5" in joined and "price" in joined
    assert "gpt" in joined and "gemini" in joined
    with pytest.raises(ConfigError, match="not usable"):
        check_startup(root=ROOT, env=KEYS)


def test_production_config_has_every_author_prompt() -> None:
    cfg = load_config(root=ROOT)
    for prompt in cfg.authors:
        assert cfg.prompt_path(prompt).exists(), prompt


def test_missing_price_rejected(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(config_dir / "models.yaml", lambda d: d["models"]["stub-gpt"].update(usd_per_mtok_in=None))
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("stub-gpt" in e and "price" in e for e in errors)


def test_unknown_author_model_rejected(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(config_dir / "models.yaml", lambda d: d["authors"].update({"auditor-observations": "made-up"}))
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("made-up" in e for e in errors)


def test_only_author_family_reviewers_rejected(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(config_dir / "review_policy.yaml", lambda d: d["points"]["observations"].update(members=["stub-claude-opus"]))
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("observations" in e for e in errors)


def test_ambiguous_family_member_rejected(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(
        config_dir / "models.yaml",
        lambda d: d["models"].update(
            {"stub-gpt-2": {"family": "gpt", "vision": True, "usd_per_mtok_in": 1.0, "usd_per_mtok_out": 2.0}}
        ),
    )
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("gpt" in e for e in errors)


def test_missing_checklist_rejected(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(config_dir / "review_policy.yaml", lambda d: d["points"]["package"].update(checklist="no-such-list"))
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("no-such-list" in e for e in errors)


def test_image_point_needs_vision(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(config_dir / "models.yaml", lambda d: d["models"]["stub-gpt"].update(vision=False))
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("vision" in e for e in errors)


def test_council_needs_two_members(tmp_path: Path) -> None:
    config_dir = _copy_stub(tmp_path)
    _edit(config_dir / "review_policy.yaml", lambda d: d["points"]["report"].update(members=["gpt"]))
    errors = validate_config(load_config(config_dir, ROOT), require_credentials=False)
    assert any("report" in e for e in errors)
