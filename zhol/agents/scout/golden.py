"""Offline golden checks. Model calls are reported and not executed."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zhol.agents.scout.extract import _parse, extract_card
from zhol.agents.scout.fields import enum_values, field_keys
from zhol.agents.scout.match import Candidate, Identity, candidates_from_document, choose
from zhol.browser.config import load_platforms
from zhol.browser.google import public as google_public
from zhol.browser.twogis import public as twogis_public
from zhol.browser.yandex import public as yandex_public

ROOT = Path(__file__).resolve().parents[3]
_READ = {"2gis": twogis_public.read_card, "yandex": yandex_public.read_card, "google": google_public.read_card}
_SEARCH = {"yandex": yandex_public.read_search, "google": google_public.read_search}


@dataclass
class GoldenReport:
    passed: int
    total: int
    problems: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    caught: int = 0
    planted: int = 0

    @property
    def ok(self) -> bool:
        if self.planted:
            return self.passed == self.total and self.caught == self.planted and not self.problems
        return self.passed == self.total and not self.problems


def _load(root: Path, name: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    folder = root / "tests" / "golden" / name
    rows = []
    for path in sorted((folder / "cases").glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        expected = json.loads((folder / "expected" / path.name).read_text(encoding="utf-8"))
        rows.append((path.stem, case, expected))
    return rows


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return left == right


def run_extract(root: Path | None = None) -> GoldenReport:
    root = root or ROOT
    cfg = load_platforms()
    report = GoldenReport(0, 0)
    unsupported_fields: set[str] = set()
    for name, case, expected in _load(root, "scout-extract"):
        report.total += 1
        platform = case["platform"]
        spec = cfg["platforms"][platform]
        html = (root / case["fixture"]).read_text(encoding="utf-8")
        try:
            document = _READ[platform](html, cfg["markers"])
            fields, sources = extract_card(
                platform,
                document,
                url=case["url"],
                seen_at=case["seen_at"],
                screenshot=case.get("screenshot") or "shot.png",
                raw_path=case["fixture"],
                code_fields=list(spec["code_fields"]),
                model_fields=list(spec["model_fields"]),
            )
        except Exception as exc:
            report.problems.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        problems = []
        for key, wanted in expected["fields"].items():
            if not _same(fields.get(key), wanted):
                problems.append(f"{key}: expected {wanted!r}, got {fields.get(key)!r}")
            elif wanted is not None:
                source = sources.get(key) or {}
                for part in ("url", "seen_at", "screenshot"):
                    if not source.get(part):
                        problems.append(f"{key}: source has no {part}")
                if source.get("read_by") != "code":
                    problems.append(f"{key}: read_by is {source.get('read_by')!r}")
        for key in spec["model_fields"]:
            unsupported_fields.add(key)
            if fields.get(key) is not None:
                problems.append(f"{key}: model field was filled without a model")
        if problems:
            report.problems.append(f"{name}: " + "; ".join(problems))
        else:
            report.passed += 1
    if unsupported_fields:
        report.unsupported.append(
            "поля "
            + ", ".join(sorted(unsupported_fields))
            + f" в {report.total} случаях (промт scout-extract, R6, платный вызов не делался)"
        )
    return report


def _identity(raw: dict[str, Any]) -> Identity:
    return Identity(raw.get("name"), raw.get("address"), list(raw.get("phones") or []))


def _candidate(raw: dict[str, Any]) -> Candidate:
    return Candidate(
        str(raw["candidate_id"]),
        raw.get("external_id"),
        raw.get("url"),
        raw.get("name"),
        raw.get("address"),
        list(raw.get("phones") or []),
    )


def run_match(root: Path | None = None) -> GoldenReport:
    root = root or ROOT
    cfg = load_platforms()
    report = GoldenReport(0, 0)
    for name, case, expected in _load(root, "scout-match"):
        report.total += 1
        identity = _identity(case["identity"])
        if case.get("fixture"):
            html = (root / case["fixture"]).read_text(encoding="utf-8")
            document = _SEARCH[case["platform"]](html, cfg["markers"])
            candidates = candidates_from_document(document)
        else:
            candidates = [_candidate(item) for item in case["candidates"]]
        decision = choose(identity, candidates)
        candidate_id = None if decision.candidate is None else decision.candidate.candidate_id
        problems = []
        if decision.verdict != expected["verdict"]:
            problems.append(f"verdict: expected {expected['verdict']!r}, got {decision.verdict!r}")
        if candidate_id != expected.get("candidate_id"):
            problems.append(f"candidate_id: expected {expected.get('candidate_id')!r}, got {candidate_id!r}")
        if "phone_match" in expected and decision.phone_match != expected["phone_match"]:
            problems.append(f"phone_match: expected {expected['phone_match']!r}, got {decision.phone_match!r}")
        if problems:
            report.problems.append(f"{name}: " + "; ".join(problems))
        else:
            report.passed += 1
    report.unsupported.append(
        "промт scout-match не вызывался (R6). Вердикт сверен кодом choose() с контрактом промта."
    )
    return report


def _shown_fields(html: str, platform: str, markers: dict[str, str]) -> dict[str, str]:
    document = _READ[platform](html, markers)
    return dict(document.fields)


def _issues(platform: str, shown: dict[str, str], recorded: dict[str, Any], other: dict[str, str] | None) -> set[int]:
    found: set[int] = set()
    keys = set(field_keys(platform)) | set(shown) | set(recorded)
    for key in keys:
        if key not in field_keys(platform) and key not in shown and key not in recorded:
            continue
        raw = shown.get(key)
        value = recorded.get(key)
        parsed = None
        parse_error = False
        if raw not in (None, ""):
            try:
                parsed = _parse(platform, key, raw)
            except Exception:
                parse_error = True
        if value is not None and raw in (None, "") and value == 0:
            found.add(3)
        if value is not None and (raw in (None, "") or parse_error or parsed != value):
            found.add(1)
        if value is None and raw not in (None, ""):
            found.add(2)
        allowed = enum_values(platform, key)
        if value is not None and allowed is not None and value not in allowed:
            found.add(5)
        if other and key in other and value is not None:
            try:
                copied = _parse(platform, key, other[key])
            except Exception:
                copied = None
            if copied == value and parsed != value:
                found.add(4)
    return found


def run_scout_fields(root: Path | None = None) -> GoldenReport:
    root = root or ROOT
    cfg = load_platforms()
    report = GoldenReport(0, 0)
    for name, case, expected in _load(root, "scout_fields"):
        shown = _shown_fields((root / case["fixture"]).read_text(encoding="utf-8"), case["platform"], cfg["markers"])
        other = None
        if case.get("other_fixture"):
            other = _shown_fields(
                (root / case["other_fixture"]).read_text(encoding="utf-8"),
                case["other_platform"],
                cfg["markers"],
            )
        issues = _issues(case["platform"], shown, case.get("fields") or {}, other)
        if expected.get("correct"):
            report.total += 1
            if issues:
                report.problems.append(f"{name}: лишние замечания {sorted(issues)}")
            else:
                report.passed += 1
        else:
            report.planted += 1
            missing = [rule for rule in expected["rules"] if rule not in issues]
            if missing:
                report.problems.append(f"{name}: не пойманы пункты {missing}, пойманы {sorted(issues)}")
            else:
                report.caught += 1
    report.unsupported.append(
        f"сверка со скриншотом ({report.total + report.planted} случаев, точка scout_fields, R6). "
        "Сверен текст сохранённой страницы."
    )
    return report


def format_report(title: str, report: GoldenReport) -> str:
    lines = [f"{title}: прошло {report.passed} из {report.total}"]
    if report.planted:
        lines.append(f"Ошибок поймано: {report.caught} из {report.planted}")
    for problem in report.problems:
        lines.append(f"- {problem}")
    if report.unsupported:
        lines.append("Не поддержано моделью: " + " ".join(report.unsupported))
    return "\n".join(lines)
