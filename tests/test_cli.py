from datetime import datetime, timezone
from pathlib import Path

import pytest

from zhol import llm
from zhol.cli import main
from zhol.llm import FakeProvider, Gateway
from zhol.llm.config import load_config
from zhol.timeutil import now, reset_clock

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
PASS_R1 = {"issues": [], "unverified": []}


def setup_function() -> None:
    reset_clock()


def teardown_function() -> None:
    reset_clock()
    llm.set_gateway(None)


def test_help_exits_zero() -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected argparse to exit on --help")


def test_now_flag_freezes_clock(capsys) -> None:
    assert main(["--now", "2026-11-02T09:00"]) == 0
    assert now() == datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    assert capsys.readouterr().out.strip() == "2026-11-02T09:00:00+00:00"


def test_now_flag_on_subcommand(capsys, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["eval", "--point", "review-engine", "--now", "2026-11-02T09:00"]) == 0
    assert now() == datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    capsys.readouterr()


def test_eval_review_engine(capsys, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["eval", "--point", "review-engine"]) == 0
    out = capsys.readouterr().out
    total = len(list((ROOT / "tests" / "golden" / "review-engine" / "cases").glob("*.json")))
    assert f"Прошло {total} из {total}" in out


@pytest.mark.parametrize("args", [["--point", "observations"], ["--prompt", "auditor-observations"]])
def test_eval_without_runner_exits_2(args, capsys) -> None:
    assert main(["eval", *args]) == 2
    assert "no golden set runner" in capsys.readouterr().err


def _stage_dir(tmp_path: Path, monkeypatch, *, report: bool = True) -> None:
    (tmp_path / "docs" / "plan" / "stages").mkdir(parents=True)
    (tmp_path / "docs" / "plan" / "stages" / "S0.md").write_text("# S0\n## Приёмка\n1. pytest\n", encoding="utf-8")
    if report:
        (tmp_path / "docs" / "acceptance").mkdir(parents=True)
        (tmp_path / "docs" / "acceptance" / "S0.md").write_text("# Отчёт S0\n1. pytest: PASS\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def _gateway(script) -> tuple[Gateway, FakeProvider]:
    provider = FakeProvider(script)
    return Gateway(load_config(STUB, ROOT), providers={n: provider for n in ("anthropic", "openai", "google")}), provider


def test_council_dev_pass(tmp_path, monkeypatch, capsys) -> None:
    _stage_dir(tmp_path, monkeypatch)
    gateway, provider = _gateway({f"council-r1:{m}": [PASS_R1] for m in ("stub-claude-opus", "stub-gpt", "stub-gemini")})
    assert main(["council-dev", "--stage", "S0", "--author", "gpt"], gateway=gateway) == 0
    assert "S0: pass" in capsys.readouterr().out
    assert len(provider.calls) == 3
    assert "Отчёт S0" in provider.calls[0]["user"]


def test_council_dev_return(tmp_path, monkeypatch, capsys) -> None:
    _stage_dir(tmp_path, monkeypatch)
    issue = {
        "id": "1", "severity": "critical", "where": "отчёт, пункт 1", "quote": "pytest: PASS",
        "what": "нет вывода команды", "evidence": "stage_file", "fix": "приложить вывод pytest",
    }
    gateway, _ = _gateway({
        "council-r1:stub-claude-opus": [{"issues": [issue], "unverified": []}],
        "council-r1:stub-gpt": [PASS_R1],
        "council-r1:stub-gemini": [PASS_R1],
        "council-r2:stub-claude-opus": [{"answers": [], "withdraw": [], "new": []}],
        "council-r2:stub-gpt": [{"answers": [], "withdraw": [], "new": []}],
        "council-r2:stub-gemini": [{"answers": [], "withdraw": [], "new": []}],
        "council-secretary:stub-claude-sonnet": [{"groups": [{"ids": ["A1"], "where": "отчёт", "what": "нет вывода", "fix": "приложить"}]}],
    })
    assert main(["council-dev", "--stage", "S0", "--author", "gpt"], gateway=gateway) == 1
    out = capsys.readouterr().out
    assert "S0: return (accepted_issues)" in out and "1. [critical]" in out


def test_council_dev_missing_report(tmp_path, monkeypatch, capsys) -> None:
    _stage_dir(tmp_path, monkeypatch, report=False)
    assert main(["council-dev", "--stage", "S0", "--author", "claude"]) == 2
    assert "docs/acceptance/S0.md" in capsys.readouterr().err


def test_council_dev_refuses_unusable_config(tmp_path, monkeypatch, capsys) -> None:
    _stage_dir(tmp_path, monkeypatch)
    monkeypatch.delenv("ZHOL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ZHOL_ROOT", str(ROOT))
    llm.set_gateway(None)
    assert main(["council-dev", "--stage", "S0", "--author", "claude"]) == 2
    assert "configuration is not usable" in capsys.readouterr().err


def test_author_flag_is_restricted() -> None:
    with pytest.raises(SystemExit):
        main(["council-dev", "--stage", "S0", "--author", "gemini"])
