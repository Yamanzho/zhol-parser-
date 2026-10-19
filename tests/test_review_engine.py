from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.db.models import CouncilItem, CouncilRun, HumanTask, LlmCall, ReviewQuality
from zhol.llm import FakeProvider, Gateway
from zhol.llm.config import load_config
from zhol.review import record_quality, run, text_guard_check
from zhol.review import votes
from zhol.review.evaluate import GOLDEN_DIR, run_golden

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / GOLDEN_DIR
CASES = sorted(p.stem for p in (GOLDEN / "cases").glob("*.json"))
OBS = {"strength": None, "observations": [{"text": "Вкладка цен в 2ГИС пустая", "field_ref": "2gis.prices"}]}
PASS_R1 = {"issues": [], "unverified": []}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(scope="module")
def golden_results():
    return {r.name: r for r in run_golden(ROOT)}


def _gateway(script, factory=None) -> tuple[Gateway, FakeProvider]:
    provider = FakeProvider(script)
    providers = {name: provider for name in ("anthropic", "openai", "google")}
    return Gateway(load_config(GOLDEN / "config", ROOT), providers=providers, session_factory=factory), provider


def _case(name: str) -> dict:
    return json.loads((GOLDEN / "cases" / f"{name}.json").read_text(encoding="utf-8"))


def test_golden_set_is_large_enough() -> None:
    assert len(CASES) >= 12
    assert {p.stem for p in (GOLDEN / "expected").glob("*.json")} == set(CASES)


@pytest.mark.parametrize("name", CASES)
def test_golden_case(golden_results, name: str) -> None:
    result = golden_results[name]
    assert result.ok, result.problems


def test_evidence_rules() -> None:
    materials = {"business_data": {}, "files": [{"id": "12"}]}
    assert votes.evidence_ok("file:12, цитата «...»", materials)
    assert votes.evidence_ok("business_data.phones: «+7»", materials)
    assert not votes.evidence_ok("file:99", materials)
    assert not votes.evidence_ok("мне кажется", materials)
    assert not votes.evidence_ok("", materials)


def test_refine_raises_group_severity() -> None:
    participants = {"A": {"family": "gpt", "weight": 1}, "B": {"family": "gemini", "weight": 0.5}}
    issues = {"A1": {"label": "A", "severity": "minor"}}
    answers = [{"ref": "A1", "label": "B", "stance": "refine", "severity": "critical"}]
    [group] = votes.tally([{"ids": ["A1"]}], issues, answers, participants, "claude", {})
    assert (group["severity"], group["status"], group["weight"], group["n"]) == ("critical", "accepted", 1.5, 1)


def test_main_items_parsed_from_checklists() -> None:
    text = (ROOT / "council" / "checklists" / "review_reply.md").read_text(encoding="utf-8")
    assert votes.main_items(text) == {1, 2}


def test_text_guard_check_flags_dash_as_minor() -> None:
    issues = text_guard_check({"reply": "Спасибо \u2014 ждём вас"})
    assert [i["severity"] for i in issues] == ["minor"]


def test_long_dash_note_does_not_block() -> None:
    obj = {"strength": None, "observations": [{"text": "Цен нет \u2014 вкладка пустая", "field_ref": "2gis.prices"}]}
    gateway, _ = _gateway({"council-r1:stub-gpt": [PASS_R1]})
    result = run("observations", "audit", 1, object_data=obj, materials={}, gateway=gateway)
    assert result.verdict == "pass"
    assert [n["what"] for n in result.notes] == ["длинное тире"]


def test_escalation_uses_council() -> None:
    script = {f"council-r1:{m}": [PASS_R1] for m in ("stub-gpt", "stub-gemini", "stub-claude-opus")}
    gateway, provider = _gateway(script)
    result = run(
        "change_request", "change_item", 1, object_data={"items": []}, materials={}, gateway=gateway, escalate=True
    )
    assert (result.verdict, result.level, len(provider.calls)) == ("pass", "council", 3)


def test_kazakh_checklist_is_added() -> None:
    script = {f"council-r1:{m}": [PASS_R1] for m in ("stub-gpt", "stub-gemini", "stub-claude-opus")}
    gateway, provider = _gateway(script)
    run("review_reply_positive", "review", 1, object_data={"reply": "ok"}, materials={}, gateway=gateway, language="kk")
    assert all("### kazakh_text" in call["user"] and "### review_reply" in call["user"] for call in provider.calls)


def test_content_reviewers_get_their_own_checklist() -> None:
    gateway, provider = _gateway({"council-r1:stub-gemini": [PASS_R1], "council-r1:stub-gpt": [PASS_R1]})
    result = run("content", "content_item", 1, object_data={"text": "Акция"}, materials={}, gateway=gateway)
    assert result.verdict == "pass" and result.level == "single"
    by_model = {c["key"].split(":")[1]: c["user"] for c in provider.calls}
    lists = ROOT / "council" / "checklists"
    image_list = (lists / "content_image.md").read_text(encoding="utf-8").strip()
    text_list = (lists / "content_text.md").read_text(encoding="utf-8").strip()
    assert image_list in by_model["stub-gemini"] and text_list not in by_model["stub-gemini"]
    assert text_list in by_model["stub-gpt"] and image_list not in by_model["stub-gpt"]


def test_author_family_flag_rules() -> None:
    gateway, _ = _gateway({})
    with pytest.raises(ValueError):
        run("dev_stage", "stage", None, object_data={}, materials={}, gateway=gateway)
    with pytest.raises(ValueError):
        run("observations", "audit", 1, object_data=OBS, materials={}, gateway=gateway, author_family="gpt")


def test_fix_reply_failing_schema_goes_to_human() -> None:
    case = _case("03_single_fixed_by_reply")
    case["script"]["executor-fix-reply:stub-claude-sonnet"] = [{"fixed_object": {"observations": []}, "replies": []}]
    gateway, _ = _gateway(case["script"])
    result = run(
        "observations", "audit", 1, object_data=case["object"], materials=case["materials"], gateway=gateway,
        author_prompt="auditor-observations",
    )
    assert (result.verdict, result.reason) == ("human", "fix_failed")


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def test_rounds_persisted_with_items_and_human_task(factory: sessionmaker[Session]) -> None:
    case = _case("11_three_rounds_not_fixed")
    gateway, _ = _gateway(case["script"], factory)
    result = run(
        "review_reply_negative", "review", 42, object_data=case["object"], materials=case["materials"],
        gateway=gateway, author_prompt=case["author_prompt"],
    )
    assert result.verdict == "human" and len(result.run_ids) == 3
    with factory() as s:
        runs = s.scalars(select(CouncilRun).order_by(CouncilRun.id)).all()
        stages = s.scalars(select(CouncilItem.stage).where(CouncilItem.run_id == runs[0].id)).all()
        task = s.get(HumanTask, result.human_task_id)
        llm_rows = s.scalar(select(func.count()).select_from(LlmCall))
    assert [(r.round, r.verdict) for r in runs] == [(1, "return"), (2, "return"), (3, "human")]
    assert {r.level for r in runs} == {"council"} and {r.author_family for r in runs} == {"claude"}
    assert [m["model"] for m in runs[0].members] == ["stub-gpt", "stub-gemini", "stub-claude-opus"]
    assert sorted(set(stages)) == ["author_reply", "r1", "r2", "secretary"]
    assert runs[0].groups["groups"][0]["n"] == 1 and runs[0].cost_usd is not None
    assert (task.reason, task.object_type, task.object_id) == ("review_human", "review", 42)
    assert task.details["reason"] == "max_rounds"
    assert llm_rows == 15


def test_human_first_gate(factory: sessionmaker[Session]) -> None:
    for i in range(2):
        gateway, _ = _gateway({"council-r1:stub-gpt": [PASS_R1]}, factory)
        result = run("observations", "audit", i, object_data=OBS, materials={}, gateway=gateway)
        assert result.verdict == "pass" and result.human_gate == "owner"
    with factory() as s:
        tasks = s.scalars(select(HumanTask).order_by(HumanTask.id)).all()
    assert [(t.reason, t.details["n"], t.details["of"]) for t in tasks] == [
        ("review_human_first", 1, 20),
        ("review_human_first", 2, 20),
    ]


def test_no_human_first_for_points_without_it(factory: sessionmaker[Session]) -> None:
    gateway, _ = _gateway({"council-r1:stub-gpt": [PASS_R1]}, factory)
    result = run("review_reply_positive", "review", 1, object_data={"reply": "Спасибо!"}, materials={}, gateway=gateway)
    assert result.verdict == "pass" and result.human_gate is None and result.human_task_id is None


def test_record_quality(factory: sessionmaker[Session]) -> None:
    gateway, _ = _gateway({"council-r1:stub-gpt": [PASS_R1]}, factory)
    result = run("review_reply_positive", "review", 1, object_data={"reply": "Спасибо!"}, materials={}, gateway=gateway)
    with factory() as s, s.begin():
        row = record_quality(s, result.run_ids[-1], "owner", agreed=False, overridden_items=[1], note="ок")
        with pytest.raises(ValueError):
            record_quality(s, result.run_ids[-1], "somebody")
    with factory() as s:
        stored = s.get(ReviewQuality, row.id)
    assert (stored.source, stored.agreed, stored.overridden_items) == ("owner", False, [1])
