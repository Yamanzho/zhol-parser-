from __future__ import annotations

import logging
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import LlmCall
from zhol.llm import ConfigError, FakeProvider, Gateway, Image, LlmCallError, LlmSchemaError, PromptError
from zhol.llm.config import load_config
from zhol.llm.prompts import load_prompt, render, variables
from zhol.llm.providers import AnthropicProvider, GoogleProvider, OpenAIProvider

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "golden" / "review-engine" / "config"
FAKE_KEY = "sk" + "-" + "abcdefghijklmnop1234"
OBS_INPUT = {
    "name": "AOE",
    "niche": "салон",
    "city": "Алматы",
    "score_total": 40,
    "score_groups_json": {"2gis": 20},
    "fields_json": {"2gis.prices": "empty"},
    "gaps_json": [{"field": "2gis.prices", "points": 5}],
}
GOOD = {"strength": None, "observations": [{"text": "Вкладка цен в 2ГИС пустая", "field_ref": "2gis.prices"}]}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _gateway(script, factory=None) -> tuple[Gateway, FakeProvider]:
    provider = FakeProvider(script)
    providers = {name: provider for name in ("anthropic", "openai", "google")}
    return Gateway(load_config(STUB, ROOT), providers=providers, session_factory=factory), provider


def test_prompt_files_parse() -> None:
    for path in sorted((ROOT / "prompts").glob("*.md")):
        if path.name == "README.md":
            continue
        prompt = load_prompt(path)
        assert prompt.name == path.stem and prompt.version and prompt.schema["type"] == "object", path.name


def test_render_rejects_missing_variables() -> None:
    assert variables("{{a}} and {{b}}") == {"a", "b"}
    with pytest.raises(PromptError, match="b"):
        render("{{a}} {{b}}", {"a": 1})
    assert render("{{a}}", {"a": {"x": "ё"}}) == '{"x": "ё"}'


def test_missing_variable_fails_before_provider_call() -> None:
    gateway, provider = _gateway({"auditor-observations:stub-claude-sonnet": [GOOD]})
    with pytest.raises(PromptError):
        gateway.run("auditor-observations", {"name": "AOE"})
    assert provider.calls == []


def test_valid_reply_returns_data_and_cost() -> None:
    gateway, provider = _gateway({"auditor-observations:stub-claude-sonnet": [GOOD]})
    result = gateway.run("auditor-observations", OBS_INPUT)
    assert result.data == GOOD
    assert (result.model, result.prompt_name, result.prompt_version) == ("stub-claude-sonnet", "auditor-observations", "1")
    assert result.cost_usd == Decimal("0.0002")
    assert len(provider.calls) == 1


def test_schema_retry_appends_error_text() -> None:
    gateway, provider = _gateway({"auditor-observations:stub-claude-sonnet": [{"observations": []}, GOOD]})
    assert gateway.run("auditor-observations", OBS_INPUT).data == GOOD
    assert len(provider.calls) == 2
    assert "не прошёл JSON-схему" in provider.calls[1]["user"]
    assert "не прошёл JSON-схему" not in provider.calls[0]["user"]


def test_schema_fails_twice() -> None:
    gateway, provider = _gateway({"auditor-observations:stub-claude-sonnet": [{"raw": "nope"}, {"raw": "```json\n{}\n```"}]})
    with pytest.raises(LlmSchemaError):
        gateway.run("auditor-observations", OBS_INPUT)
    assert len(provider.calls) == 2


def test_fenced_json_accepted() -> None:
    import json

    fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    gateway, _ = _gateway({"auditor-observations:stub-claude-sonnet": [{"raw": fenced}]})
    assert gateway.run("auditor-observations", OBS_INPUT).data == GOOD


def test_provider_error_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gateway, _ = _gateway({"auditor-observations:stub-claude-sonnet": [{"error": f"bad key {FAKE_KEY}"}]})
    with pytest.raises(LlmCallError) as info:
        gateway.run("auditor-observations", OBS_INPUT)
    assert FAKE_KEY not in str(info.value)
    assert info.value.__cause__ is None
    assert FAKE_KEY not in caplog.text


def test_logs_never_contain_prompt_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gateway, _ = _gateway({"auditor-observations:stub-claude-sonnet": [GOOD]})
    gateway.run("auditor-observations", {**OBS_INPUT, "name": "SECRET-COMPANY-NAME"})
    assert "SECRET-COMPANY-NAME" not in caplog.text


def test_images_need_vision(tmp_path: Path) -> None:
    import shutil

    import yaml

    config_dir = tmp_path / "config"
    shutil.copytree(STUB, config_dir)
    models = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))
    models["models"]["stub-claude-sonnet"]["vision"] = False
    (config_dir / "models.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
    provider = FakeProvider({})
    gateway = Gateway(load_config(config_dir, ROOT), providers={"anthropic": provider})
    with pytest.raises(ConfigError, match="vision"):
        gateway.run("auditor-observations", OBS_INPUT, [Image("s1", "image/png", b"x")])
    assert provider.calls == []


def test_explicit_model_override() -> None:
    gateway, provider = _gateway({"auditor-observations:stub-gpt": [GOOD]})
    assert gateway.run("auditor-observations", OBS_INPUT, model="stub-gpt").model == "stub-gpt"
    assert provider.calls[0]["key"] == "auditor-observations:stub-gpt"


def test_calls_recorded(migrated_engine: Engine) -> None:
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)
    gateway, _ = _gateway(
        {"auditor-observations:stub-claude-sonnet": [{"raw": "bad"}, GOOD, {"error": f"x {FAKE_KEY}"}]}, factory
    )
    gateway.run("auditor-observations", OBS_INPUT)
    with pytest.raises(LlmCallError):
        gateway.run("auditor-observations", OBS_INPUT)
    with factory() as s:
        rows = s.scalars(select(LlmCall).order_by(LlmCall.id)).all()
    assert [(r.ok, r.schema_ok) for r in rows] == [(True, False), (True, True), (False, None)]
    assert {r.prompt_name for r in rows} == {"auditor-observations"}
    assert {r.prompt_version for r in rows} == {"1"}
    assert rows[0].input_hash == rows[1].input_hash and len(rows[0].input_hash) == 64
    assert rows[1].tokens_in == 100 and rows[1].cost_usd == Decimal("0.0002")
    assert all(r.latency_ms is not None for r in rows)


def _usage(**kw):
    return SimpleNamespace(**kw)


def test_anthropic_adapter_shapes_request() -> None:
    sent = {}

    class Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"a": 1}')],
                usage=_usage(input_tokens=7, output_tokens=3),
            )

    provider = AnthropicProvider(SimpleNamespace(messages=Messages()))
    reply = provider.complete(
        prompt="p", model="m", system="sys", user="u", temperature=0.2, images=[Image("i1", "image/png", b"x")], json_schema={}
    )
    assert (reply.text, reply.tokens_in, reply.tokens_out) == ('{"a": 1}', 7, 3)
    assert "temperature" not in sent and sent["system"] == "sys"
    content = sent["messages"][0]["content"]
    assert content[0]["text"] == "image id: i1" and content[1]["type"] == "image" and content[-1]["text"] == "u"


def test_openai_adapter_shapes_request() -> None:
    sent = {}

    class Completions:
        def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))],
                usage=_usage(prompt_tokens=5, completion_tokens=2),
            )

    provider = OpenAIProvider(SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    reply = provider.complete(prompt="p", model="m", system="sys", user="u", temperature=0.2, images=[], json_schema={})
    assert (reply.text, reply.tokens_in, reply.tokens_out) == ('{"a": 1}', 5, 2)
    assert sent["temperature"] == 0.2 and sent["response_format"] == {"type": "json_object"}
    assert sent["messages"][0] == {"role": "system", "content": "sys"}


def test_google_adapter_shapes_request() -> None:
    sent = {}

    class Models:
        def generate_content(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(text='{"a": 1}', usage_metadata=_usage(prompt_token_count=4, candidates_token_count=1))

    provider = GoogleProvider(SimpleNamespace(models=Models()))
    reply = provider.complete(
        prompt="p", model="m", system="sys", user="u", temperature=0.2, images=[Image("i1", "image/png", b"x")], json_schema={}
    )
    assert (reply.text, reply.tokens_in, reply.tokens_out) == ('{"a": 1}', 4, 1)
    assert sent["config"].system_instruction == "sys" and sent["config"].temperature == 0.2
    assert sent["config"].response_mime_type == "application/json"
    assert len(sent["contents"]) == 3


def test_fake_provider_unscripted_call_fails() -> None:
    gateway, _ = _gateway({})
    with pytest.raises(LlmCallError, match="FakeProviderError"):
        gateway.run("auditor-observations", OBS_INPUT)
