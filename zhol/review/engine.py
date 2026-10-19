from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from zhol import guards
from zhol.db.models import CouncilItem, CouncilRun, HumanTask, ReviewQuality
from zhol.llm.config import DEV_AUTHOR_FAMILIES, Config
from zhol.llm.gateway import Gateway, LlmError, schema_error
from zhol.llm.providers import Image
from zhol.redact import safe_error
from zhol.review import votes
from zhol.timeutil import now

log = logging.getLogger(__name__)

FIX_BY_REPLY = frozenset(
    {"observations", "package", "review_reply_positive", "review_reply_negative", "change_request", "report"}
)
TEXT_POINTS = frozenset(
    {
        "observations",
        "package",
        "review_reply_positive",
        "review_reply_negative",
        "change_request",
        "report",
        "kazakh_text",
        "content",
    }
)
KAZAKH_LETTERS = frozenset("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
QUALITY_SOURCES = frozenset({"owner", "client", "native_speaker", "late_signal", "weekly_audit"})
LABELS = "ABCDEFGH"
HUMAN_REASON = "review_human"
HUMAN_FIRST_REASON = "review_human_first"

Compare = Callable[[Any, Any], list[dict[str, Any]]]
CodeCheck = Callable[[Any], list[dict[str, Any]]]


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def is_kazakh(value: Any) -> bool:
    return any(ch in KAZAKH_LETTERS for _, text in _strings(value) for ch in text)


def text_guard_check(obj: Any) -> list[dict[str, Any]]:
    issues = []
    for path, text in _strings(obj):
        where = path or "(root)"
        for phrase in guards.forbidden_phrases(text):
            issues.append(
                {
                    "severity": "critical",
                    "where": where,
                    "quote": text[:150],
                    "what": f"запрещённая фраза «{phrase}»",
                    "evidence": "guards.forbidden_phrases",
                    "fix": f"убрать «{phrase}»",
                }
            )
        if not guards.no_long_dash(text):
            issues.append(
                {
                    "severity": "minor",
                    "where": where,
                    "quote": text[:150],
                    "what": "длинное тире",
                    "evidence": "guards.no_long_dash",
                    "fix": "заменить на дефис, запятую или двоеточие",
                }
            )
    return issues


@dataclass
class Participant:
    label: str
    model: str
    family: str
    weight: float
    checklists: list[str]
    checklist_text: str
    main: set[int]

    def public(self) -> dict[str, Any]:
        return {"label": self.label, "model": self.model, "family": self.family, "weight": self.weight}


@dataclass
class Round:
    number: int
    verdict: str = "pass"
    reason: str | None = None
    groups: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    unverified: list[dict[str, Any]] = field(default_factory=list)
    code_issues: list[dict[str, Any]] = field(default_factory=list)
    discrepancies: list[dict[str, Any]] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    cost: Decimal | None = Decimal(0)
    secretary_fallback: bool = False
    started: datetime = field(default_factory=now)
    run_id: int | None = None


@dataclass
class ReviewResult:
    verdict: str
    reason: str | None
    level: str
    rounds: list[Round]
    object: Any
    human_task_id: int | None = None
    human_gate: str | None = None

    @property
    def groups(self) -> list[dict[str, Any]]:
        return self.rounds[-1].groups

    @property
    def notes(self) -> list[dict[str, Any]]:
        return [note for r in self.rounds for note in r.notes]

    @property
    def run_ids(self) -> list[int]:
        return [r.run_id for r in self.rounds if r.run_id is not None]


@dataclass
class _Context:
    point: str
    policy: Mapping[str, Any]
    object_type: str
    object_id: int | None
    level: str
    family: str | None
    participants: list[Participant]
    materials: Mapping[str, Any]
    images: Sequence[Image]
    gateway: Gateway
    factory: sessionmaker[Session] | None
    human_task_id: int | None = None
    human_gate: str | None = None

    @property
    def by_label(self) -> dict[str, Participant]:
        return {p.label: p for p in self.participants}

    @property
    def public(self) -> dict[str, dict[str, Any]]:
        return {p.label: p.public() for p in self.participants}


def run(
    point: str,
    object_type: str,
    object_id: int | None,
    *,
    object_data: Any,
    materials: Mapping[str, Any],
    gateway: Gateway | None = None,
    session_factory: sessionmaker[Session] | None = None,
    author_family: str | None = None,
    author_prompt: str | None = None,
    author_input: Mapping[str, Any] | None = None,
    compare: Compare | None = None,
    code_check: CodeCheck | None = None,
    images: Sequence[Image] = (),
    escalate: bool = False,
    language: str | None = None,
) -> ReviewResult:
    if gateway is None:
        from zhol.llm import get_gateway

        gateway = get_gateway()
    cfg = gateway.config
    policy = cfg.point(point)
    family = _author_family(policy, author_family)
    kazakh = point != "kazakh_text" and (language == "kk" if language else is_kazakh(object_data))
    level, participants = _plan(cfg, policy, kazakh=kazakh, escalate=escalate)
    ctx = _Context(
        point=point,
        policy=policy,
        object_type=object_type,
        object_id=object_id,
        level=level,
        family=family,
        participants=participants,
        materials=materials,
        images=images,
        gateway=gateway,
        factory=session_factory or gateway.session_factory,
    )
    check = code_check if code_check is not None else (text_guard_check if point in TEXT_POINTS else None)
    max_rounds = int(cfg.policy.get("max_rounds", 3))
    can_fix = point in FIX_BY_REPLY and author_prompt is not None

    rounds: list[Round] = []
    current = object_data
    open_groups: list[dict[str, Any]] = []
    replies: Any = None
    for number in range(1, max_rounds + 1):
        code_issues = check(current) if check else []
        if any(i["severity"] in votes.BLOCKING for i in code_issues):
            r = _code_round(number, code_issues, open_groups)
        elif policy.get("mode"):
            r = _double_round(ctx, current, author_prompt, author_input, compare)
        elif number == 1:
            r = _first_round(ctx, current)
        else:
            r = _recheck_round(ctx, number, current, open_groups, replies)
        r.code_issues = code_issues
        r.notes.extend(i for i in code_issues if i["severity"] not in votes.BLOCKING)
        rounds.append(r)
        if r.verdict != "return" or policy.get("mode") or not can_fix:
            break
        if number == max_rounds:
            r.verdict, r.reason = "human", "max_rounds"
            break
        open_groups = [g for g in r.groups if g["status"] == "accepted" and g["severity"] in votes.BLOCKING]
        fixed = _fix(ctx, r, author_prompt, current, open_groups)
        if fixed is None:
            r.verdict, r.reason = "human", "fix_failed"
            break
        current, replies = fixed
        _persist(ctx, r, final=False)

    last = rounds[-1]
    _persist(ctx, last, final=True)
    log.info("review %s %s:%s -> %s (%s)", point, object_type, object_id, last.verdict, last.reason)
    return ReviewResult(
        verdict=last.verdict,
        reason=last.reason,
        level=level,
        rounds=rounds,
        object=current,
        human_task_id=ctx.human_task_id,
        human_gate=ctx.human_gate,
    )


def record_quality(
    session: Session,
    run_id: int,
    source: str,
    *,
    agreed: bool | None = None,
    overridden_items: list[Any] | None = None,
    missed: list[Any] | None = None,
    note: str | None = None,
) -> ReviewQuality:
    if source not in QUALITY_SOURCES:
        raise ValueError(f"unknown review_quality source {source!r}")
    row = ReviewQuality(
        run_id=run_id,
        source=source,
        agreed=agreed,
        overridden_items=overridden_items,
        missed=missed,
        note=note,
        at=now(),
    )
    session.add(row)
    session.flush()
    return row


def _author_family(policy: Mapping[str, Any], given: str | None) -> str | None:
    declared = policy.get("author_family")
    if declared == "from_flag":
        if given not in DEV_AUTHOR_FAMILIES:
            raise ValueError(f"author_family must be one of {DEV_AUTHOR_FAMILIES}")
        return given
    if given is not None and given != declared:
        raise ValueError(f"author_family {given!r} does not match policy {declared!r}")
    return None if declared == "none" else declared


def _point_checklists(policy: Mapping[str, Any]) -> list[str]:
    if policy.get("checklists"):
        return list(policy["checklists"])
    return [policy["checklist"]] if policy.get("checklist") else []


def _plan(cfg: Config, policy: Mapping[str, Any], *, kazakh: bool, escalate: bool) -> tuple[str, list[Participant]]:
    lists = _point_checklists(policy)
    if kazakh:
        kz = cfg.point("kazakh_text")
        level, members = "council", list(kz["members"])
        per_member = [lists + [kz["checklist"]]] * len(members)
    elif escalate:
        level, members = "council", ["gpt", "gemini", cfg.policy["council_model_claude"]]
        per_member = [lists] * len(members)
    else:
        level, members = policy["level"], list(policy["members"])
        if level == "single" and len(lists) > 1:
            per_member = [[name] for name in lists]
        else:
            per_member = [lists] * len(members)
    participants = []
    for label, member, names in zip(LABELS, members, per_member):
        spec = cfg.resolve_member(member)
        texts = {name: cfg.checklist_path(name).read_text(encoding="utf-8") for name in names}
        if len(texts) > 1:
            body = "\n\n".join(f"### {name}\n{text}" for name, text in texts.items())
        else:
            body = next(iter(texts.values()), "")
        main = set().union(*(votes.main_items(text) for text in texts.values())) if texts else set()
        participants.append(
            Participant(label, spec.name, spec.family, cfg.weight(spec.family), list(names), body, main)
        )
    return level, participants


def _call(
    ctx: _Context,
    r: Round,
    prompt: str,
    variables: Mapping[str, Any],
    *,
    model: str | None,
    stage: str,
    images: Sequence[Image] = (),
    attempts: int = 2,
) -> Any:
    error = None
    for _ in range(attempts):
        try:
            result = ctx.gateway.run(prompt, variables, images, model=model)
        except LlmError as exc:
            error = safe_error(exc)
            continue
        r.items.append({"stage": stage, "model": result.model, "payload": result.data})
        if r.cost is not None and result.cost_usd is not None:
            r.cost += result.cost_usd
        else:
            r.cost = None
        return result.data
    r.items.append({"stage": stage, "model": model or ctx.gateway.config.authors.get(prompt, "?"), "payload": {"error": error}})
    return None


def _add_issue(issues: dict[str, dict[str, Any]], counters: dict[str, int], label: str, issue: Mapping[str, Any]) -> None:
    counters[label] = counters.get(label, 0) + 1
    issue_id = f"{label}{counters[label]}"
    issues[issue_id] = {**issue, "id": issue_id, "label": label, "own_id": issue.get("id")}


def _labeled(issue: Mapping[str, Any]) -> dict[str, Any]:
    return {k: issue[k] for k in ("id", "label", "severity", "where", "quote", "what", "evidence", "fix") if k in issue}


def _first_round(ctx: _Context, obj: Any) -> Round:
    r = Round(1)
    base = {"point": ctx.point, "object_json": obj, "materials_json": ctx.materials}
    by_label = ctx.by_label
    responses: dict[str, Any] = {}
    for p in ctx.participants:
        data = _call(ctx, r, "council-r1", {**base, "checklist": p.checklist_text}, model=p.model, stage="r1", images=ctx.images)
        if data is not None:
            responses[p.label] = data
    responded = [by_label[label] for label in responses]
    has_quorum = votes.quorum([p.public() for p in responded], ctx.level, ctx.family, len(ctx.participants))

    issues: dict[str, dict[str, Any]] = {}
    counters: dict[str, int] = {}
    for label, data in responses.items():
        for issue in data["issues"]:
            _add_issue(issues, counters, label, issue)
        r.unverified.extend({**u, "label": label} for u in data["unverified"])
    unverified_main = any(u["item"] in by_label[u["label"]].main for u in r.unverified)

    answers: list[dict[str, Any]] = []
    if ctx.level == "council" and len(responded) >= 2 and issues:
        withdrawn: set[str] = set()
        new: list[tuple[str, Mapping[str, Any]]] = []
        for p in responded:
            own = [_labeled(i) for i in issues.values() if i["label"] == p.label]
            others = [_labeled(i) for i in issues.values() if i["label"] != p.label]
            data = _call(
                ctx,
                r,
                "council-r2",
                {**base, "checklist": p.checklist_text, "own_issues_json": own, "others_issues_labeled_json": others},
                model=p.model,
                stage="r2",
                images=ctx.images,
            )
            if data is None:
                continue
            for answer in data["answers"]:
                target = issues.get(answer["ref"])
                if target is not None and target["label"] != p.label:
                    answers.append({**answer, "label": p.label})
            withdrawn.update(w for w in data["withdraw"] if w in issues and issues[w]["label"] == p.label)
            new.extend((p.label, issue) for issue in data["new"])
        for issue_id in withdrawn:
            del issues[issue_id]
        answers = [a for a in answers if a["ref"] in issues]
        for label, issue in new:
            _add_issue(issues, counters, label, issue)

    if ctx.level == "single":
        groups = [
            {
                "ids": [issue_id],
                "where": i["where"],
                "what": i["what"],
                "fix": i["fix"],
                "severity": i["severity"],
                "status": "accepted",
                "supporters": [i["label"]],
                "weight": by_label[i["label"]].weight,
            }
            for issue_id, i in issues.items()
        ]
        votes.number_accepted(groups)
    else:
        partition = _secretary(ctx, r, issues)
        groups = votes.tally(partition, issues, answers, ctx.public, ctx.family, ctx.materials)
    r.groups = groups
    r.notes = [
        g
        for g in groups
        if (g["status"] == "accepted" and g["severity"] == "minor")
        or (g["status"] == "disputed" and g["severity"] != "critical")
    ]
    r.verdict, r.reason = votes.verdict(has_quorum=has_quorum, groups=groups, unverified_main=unverified_main)
    return r


def _secretary(ctx: _Context, r: Round, issues: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not issues:
        return []
    payload = [{k: i[k] for k in ("id", "label", "severity", "where", "what", "fix")} for i in issues.values()]
    for _ in range(2):
        data = _call(ctx, r, "council-secretary", {"all_issues_json": payload}, model=None, stage="secretary", attempts=1)
        if data is not None and _partition_ok(data["groups"], issues):
            return data["groups"]
    r.secretary_fallback = True
    return [{"ids": [issue_id], "where": i["where"], "what": i["what"], "fix": i["fix"]} for issue_id, i in issues.items()]


def _partition_ok(groups: Sequence[Mapping[str, Any]], issues: Mapping[str, Any]) -> bool:
    seen = [issue_id for g in groups for issue_id in g["ids"]]
    return len(seen) == len(set(seen)) and set(seen) == set(issues)


def _recheck_round(ctx: _Context, number: int, obj: Any, open_groups: list[dict[str, Any]], replies: Any) -> Round:
    r = Round(number)
    previous = [{k: g[k] for k in ("n", "severity", "where", "what", "fix")} for g in open_groups]
    answers: dict[str, dict[int, bool]] = {}
    new: list[dict[str, Any]] = []
    responded = []
    for p in ctx.participants:
        data = _call(
            ctx,
            r,
            "council-recheck",
            {
                "round": number,
                "point": ctx.point,
                "checklist": p.checklist_text,
                "previous_issues_json": previous,
                "author_reply_json": replies,
                "object_json": obj,
                "materials_json": ctx.materials,
            },
            model=p.model,
            stage="recheck",
            images=ctx.images,
        )
        if data is None:
            continue
        responded.append(p.public())
        answers[p.label] = {a["n"]: a["fixed"] for a in data["answers"]}
        new.extend({**issue, "label": p.label} for issue in data["new"])
    has_quorum = votes.quorum(responded, ctx.level, ctx.family, len(ctx.participants))
    not_fixed, new_accepted, new_disputed = votes.recheck(open_groups, answers, new, ctx.public, ctx.family, ctx.level)
    next_n = max((g["n"] for g in open_groups), default=0) + 1

    def from_new(issue: Mapping[str, Any], status: str) -> dict[str, Any]:
        return {
            "ids": [],
            "where": issue["where"],
            "what": issue["what"],
            "fix": issue["fix"],
            "evidence": issue["evidence"],
            "severity": "critical",
            "status": status,
            "supporters": [issue["label"]],
            "source": "recheck",
        }

    remaining = [dict(g) for g in not_fixed]
    for k, issue in enumerate(new_accepted):
        remaining.append({**from_new(issue, "accepted"), "n": next_n + k})
    r.groups = remaining + [from_new(issue, "disputed") for issue in new_disputed]
    if not has_quorum:
        r.verdict, r.reason = "human", "no_quorum"
    elif new_disputed:
        r.verdict, r.reason = "human", "disputed_critical"
    elif remaining:
        r.verdict, r.reason = "return", "accepted_issues"
    else:
        r.verdict, r.reason = "pass", None
    return r


def _code_round(number: int, code_issues: list[dict[str, Any]], open_groups: list[dict[str, Any]]) -> Round:
    r = Round(number)
    start = max((g["n"] for g in open_groups), default=0) + 1
    r.groups = votes.number_accepted(
        [
            {**i, "ids": [], "status": "accepted", "source": "code"}
            for i in code_issues
            if i["severity"] in votes.BLOCKING
        ],
        start,
    )
    r.verdict, r.reason = "return", "code_check"
    return r


def _double_round(
    ctx: _Context,
    obj: Any,
    author_prompt: str | None,
    author_input: Mapping[str, Any] | None,
    compare: Compare | None,
) -> Round:
    if author_prompt is None or author_input is None or compare is None:
        raise ValueError(f"{ctx.policy.get('mode')} needs author_prompt, author_input and compare")
    r = Round(1)
    reviewer = ctx.participants[0]
    data = _call(ctx, r, author_prompt, author_input, model=reviewer.model, stage="r1", images=ctx.images)
    if data is None or not votes.quorum([reviewer.public()], "single", ctx.family, 1):
        r.verdict, r.reason = "human", "no_quorum"
        return r
    r.discrepancies = compare(obj, data)
    if r.discrepancies:
        r.verdict, r.reason = "return", "discrepancy"
    return r


def _fix(
    ctx: _Context, r: Round, author_prompt: str | None, current: Any, open_groups: list[dict[str, Any]]
) -> tuple[Any, Any] | None:
    issues = [{k: g[k] for k in ("n", "severity", "where", "what", "fix")} for g in open_groups]
    data = _call(
        ctx,
        r,
        "executor-fix-reply",
        {
            "original_prompt_name": author_prompt,
            "object_json": current,
            "issues_json": issues,
            "materials_json": ctx.materials,
        },
        model=None,
        stage="author_reply",
    )
    if data is None:
        return None
    error = schema_error(data["fixed_object"], ctx.gateway.prompt(author_prompt).schema)
    if error:
        r.items.append(
            {"stage": "author_reply", "model": "code", "payload": {"error": f"fixed_object fails {author_prompt} schema: {error}"}}
        )
        return None
    return data["fixed_object"], data["replies"]


def _groups_payload(r: Round) -> dict[str, Any]:
    return {
        "reason": r.reason,
        "groups": r.groups,
        "notes": r.notes,
        "unverified": r.unverified,
        "code_issues": r.code_issues,
        "discrepancies": r.discrepancies,
        "secretary_fallback": r.secretary_fallback,
    }


def _persist(ctx: _Context, r: Round, *, final: bool) -> None:
    if ctx.factory is None:
        return
    with ctx.factory() as session, session.begin():
        row = CouncilRun(
            point=ctx.point,
            level=ctx.level,
            object_type=ctx.object_type,
            object_id=ctx.object_id,
            author_family=ctx.family or "none",
            round=r.number,
            members=[p.public() for p in ctx.participants],
            verdict=r.verdict,
            groups=_groups_payload(r),
            cost_usd=r.cost,
            started_at=r.started,
            finished_at=now(),
        )
        session.add(row)
        session.flush()
        r.run_id = row.id
        for item in r.items:
            session.add(CouncilItem(run_id=row.id, stage=item["stage"], model=item["model"], payload=item["payload"], at=now()))
        if not final:
            return
        if r.verdict == "human":
            task = HumanTask(
                reason=HUMAN_REASON,
                object_type=ctx.object_type,
                object_id=ctx.object_id,
                details={"point": ctx.point, "run_id": row.id, "reason": r.reason, "groups": r.groups},
            )
            session.add(task)
            session.flush()
            ctx.human_task_id = task.id
        elif r.verdict == "pass":
            _human_first(session, ctx, row.id)


def _human_first(session: Session, ctx: _Context, run_id: int) -> None:
    limit = ctx.policy.get("human_first")
    if not limit or not ctx.policy.get("human_first_blocking"):
        return
    passes = session.scalar(
        select(func.count())
        .select_from(CouncilRun)
        .where(CouncilRun.point == ctx.point, CouncilRun.verdict == "pass", CouncilRun.id != run_id)
    )
    if passes >= limit:
        return
    task = HumanTask(
        reason=HUMAN_FIRST_REASON,
        object_type=ctx.object_type,
        object_id=ctx.object_id,
        details={"point": ctx.point, "run_id": run_id, "n": passes + 1, "of": limit},
    )
    session.add(task)
    session.flush()
    ctx.human_task_id = task.id
    ctx.human_gate = "owner"
