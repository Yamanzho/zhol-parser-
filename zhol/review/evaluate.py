from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zhol.llm.config import ConfigError, load_config, validate_config
from zhol.llm.gateway import Gateway
from zhol.llm.providers import FakeProvider
from zhol.review.engine import ReviewResult, run

GOLDEN_DIR = Path("tests") / "golden" / "review-engine"


def _equal(author: Any, reviewer: Any) -> list[dict[str, Any]]:
    if not isinstance(author, dict) or not isinstance(reviewer, dict):
        return [] if author == reviewer else [{"field": None, "author": author, "reviewer": reviewer}]
    keys = sorted(set(author) | set(reviewer))
    return [
        {"field": key, "author": author.get(key), "reviewer": reviewer.get(key)}
        for key in keys
        if author.get(key) != reviewer.get(key)
    ]


COMPARATORS = {"equal": _equal}


@dataclass
class CaseResult:
    name: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    result: ReviewResult | None = None


def summary(result: ReviewResult, provider: FakeProvider) -> dict[str, Any]:
    first = result.rounds[0]
    return {
        "verdict": result.verdict,
        "reason": result.reason,
        "level": result.level,
        "rounds": len(result.rounds),
        "accepted": sum(g["status"] == "accepted" for g in first.groups),
        "disputed": sum(g["status"] == "disputed" for g in first.groups),
        "notes": len(first.notes),
        "provider_calls": len(provider.calls),
        "secretary_fallback": first.secretary_fallback,
        "discrepancies": len(first.discrepancies or []),
    }


def run_case(case: dict[str, Any], gateway_config: Any) -> tuple[ReviewResult, FakeProvider]:
    provider = FakeProvider(case.get("script", {}))
    gateway = Gateway(gateway_config, providers={name: provider for name in ("anthropic", "openai", "google")})
    compare = COMPARATORS[case["compare"]] if case.get("compare") else None
    result = run(
        case["point"],
        case.get("object_type", "golden"),
        None,
        object_data=case["object"],
        materials=case.get("materials", {}),
        gateway=gateway,
        author_family=case.get("author_family"),
        author_prompt=case.get("author_prompt"),
        author_input=case.get("author_input"),
        compare=compare,
        escalate=case.get("escalate", False),
        language=case.get("language"),
    )
    return result, provider


def run_golden(root: str | Path | None = None) -> list[CaseResult]:
    root = Path(root or os.environ.get("ZHOL_ROOT") or Path.cwd())
    golden = root / GOLDEN_DIR
    config = load_config(golden / "config", root)
    errors = validate_config(config, require_credentials=False)
    if errors:
        raise ConfigError("golden config is not usable:\n- " + "\n- ".join(errors))
    results = []
    for path in sorted((golden / "cases").glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        expected = json.loads((golden / "expected" / path.name).read_text(encoding="utf-8"))
        try:
            result, provider = run_case(case, config)
        except Exception as exc:
            results.append(CaseResult(path.stem, False, [f"raised {type(exc).__name__}: {exc}"]))
            continue
        actual = summary(result, provider)
        problems = [
            f"{key}: expected {value!r}, got {actual.get(key)!r}"
            for key, value in expected.items()
            if actual.get(key) != value
        ]
        results.append(CaseResult(path.stem, not problems, problems, result))
    return results
