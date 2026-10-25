from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from zhol.timeutil import now, parse_now, set_now

EVAL_POINTS = ("review-engine",)


def _now_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--now",
        metavar="ISO8601",
        default=argparse.SUPPRESS,
        help="Freeze timeutil.now() to this instant (naive values are treated as UTC)",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhol", description="Zhol CLI", parents=[_now_parent()])
    commands = parser.add_subparsers(dest="command")

    ev = commands.add_parser("eval", parents=[_now_parent()], help="Run a golden set")
    target = ev.add_mutually_exclusive_group(required=True)
    target.add_argument("--point", help="Review point or engine golden set (supported: review-engine)")
    target.add_argument("--prompt", help="Prompt golden set")

    dev = commands.add_parser("council-dev", parents=[_now_parent()], help="Development council for a stage report")
    dev.add_argument("--stage", required=True, help="Stage id, for example S0 or FINAL")
    dev.add_argument("--author", required=True, choices=("claude", "gpt"), help="Model family that wrote the stage code")
    dev.add_argument("--tests-output", type=Path, help="File with test and command output for the stage")
    dev.add_argument("--diff-base", default="HEAD", help="Git revision the stage diff is taken against")

    imp = commands.add_parser("import-etalon", parents=[_now_parent()], help="Import the 50 reference companies (idempotent)")
    imp.add_argument("--csv", type=Path, default=Path("source/etalon-50.csv"))

    chk = commands.add_parser("score-check", parents=[_now_parent()], help="Import the reference set and compare score() with балл_итого")
    chk.add_argument("--csv", type=Path, default=Path("source/etalon-50.csv"))

    sch = commands.add_parser("scheduler-tick", parents=[_now_parent()], help="Recover expired leases and enqueue due periodic jobs")
    sch.add_argument("--loop", type=float, metavar="SECONDS", help="Repeat every SECONDS until stopped (scheduler service)")

    scout = commands.add_parser("scout", parents=[_now_parent()], help="Collect public cards for existing companies")
    target = scout.add_mutually_exclusive_group(required=True)
    target.add_argument("--ids-file", type=Path, help="Text file with one 2GIS id per line")
    target.add_argument("--etalon", action="store_true", help="All 50 reference companies")
    scout.add_argument("--purpose", default="prospect", choices=("prospect", "order_fresh", "report", "subscription_start"))
    scout.add_argument("--fixtures", type=Path, help="Saved pages directory (default: tests/fixtures/pages)")
    scout.add_argument("--live", action="store_true", help="Public 2GIS pilot for exactly 3 companies")
    scout.add_argument("--manual", action="store_true", help="Visible 2GIS pilot for exactly 1 company")
    scout.add_argument("--visible", action="store_true", help="Visible 2GIS pilot for exactly 2 companies")
    scout.add_argument("--maps", action="store_true", help="Visible Yandex and Google read for exactly 2 companies")
    scout.add_argument("--enqueue-only", action="store_true", help="Queue scout.collect and do not run it")

    batch = commands.add_parser("scout-batch-check", parents=[_now_parent()], help="Reject a batch whose fields repeat a cross-platform formula")
    batch.add_argument("--since", required=True, help="ISO date or datetime. Date-only values start at midnight in Asia/Almaty")

    commands.add_parser("scout-compare", parents=[_now_parent()], help="Compare scout snapshots with the 30.09 reference import")

    contacts = commands.add_parser("import-contacts", parents=[_now_parent()], help="Import companies from base contacts, skipping the reference set")
    contacts.add_argument("--csv", type=Path, default=Path("source/base-contacts.csv"))

    exported = commands.add_parser("export-mailing", parents=[_now_parent()], help="Write the Bek Excel file from stored snapshots")
    exported.add_argument("--out", type=Path, default=Path("data/export/mailing.xlsx"))

    collected = commands.add_parser("scout-batch", parents=[_now_parent()], help="Collect a city and category into the Excel file")
    collected.add_argument("--city", help="City name, required unless --resume is set")
    collected.add_argument("--category", help="Supported category keyword, required unless --resume is set")
    collected.add_argument("--platforms", default="2gis,yandex,google", help="Comma-separated: 2gis, yandex, google")
    collected.add_argument("--limit", type=int, help="Stop after this many matching companies. Omit to take every match")
    collected.add_argument("--out", type=Path, default=Path("data/export/mailing.xlsx"))
    collected.add_argument("--resume", type=int, help="Continue a batch id without repeating completed companies")
    collected.add_argument("--source-mode", choices=("fixture", "manual", "live"), default="fixture")
    collected.add_argument("--catalog", type=Path, default=Path("tests/fixtures/catalog.yaml"))
    collected.add_argument("--discovery", type=Path, help="Saved search pages. Default remains the fixture catalog.")
    collected.add_argument("--fixtures", type=Path, help="Saved pages directory")

    created = commands.add_parser(
        "create-test-order",
        parents=[_now_parent()],
        help="Create a local test order from a JSON spec",
    )
    created.add_argument("--spec", type=Path, required=True, help="JSON file with the order spec")

    status = commands.add_parser("order-status", parents=[_now_parent()], help="Show a local order")
    status.add_argument("--order-id", type=int, required=True)

    ready = commands.add_parser(
        "check-order-readiness",
        parents=[_now_parent()],
        help="Show whether a local order can proceed to listing work",
    )
    ready.add_argument("--order-id", type=int, required=True)

    content = commands.add_parser(
        "prepare-content",
        parents=[_now_parent()],
        help="Draft listing text and an abstract illustration without publishing",
    )
    content.add_argument("--order-id", type=int, required=True)
    content.add_argument("--out", type=Path, default=Path("data/content"), help="Directory for draft files")

    content_view = commands.add_parser("content-status", parents=[_now_parent()], help="Show content drafts for an order")
    content_view.add_argument("--order-id", type=int, required=True)

    listing = commands.add_parser(
        "dry-run-listing",
        parents=[_now_parent()],
        help="Plan listing creates and updates without sending a form",
    )
    listing.add_argument("--order-id", type=int, required=True)
    listing.add_argument("--pages", type=Path, help="Saved cabinet HTML: 2gis.html, yandex.html, google.html")
    listing.add_argument("--evidence", type=Path, help="Directory for the plan and saved pages")

    yandex = commands.add_parser(
        "prepare-yandex-listing",
        parents=[_now_parent()],
        help="Prepare Yandex Business edits and stop before Save",
    )
    yandex.add_argument("--order-id", type=int, required=True)
    yandex.add_argument("--page", required=True, help="Saved cabinet HTML, or an https address when ZHOL_YANDEX_CABINET=1")
    yandex.add_argument("--evidence", type=Path, required=True)

    twogis = commands.add_parser(
        "prepare-2gis-listing",
        parents=[_now_parent()],
        help="Prepare 2GIS cabinet edits and stop before Save",
    )
    twogis.add_argument("--order-id", type=int, required=True)
    twogis.add_argument("--page", required=True, help="Saved cabinet HTML, or an https address when ZHOL_2GIS_CABINET=1")
    twogis.add_argument("--evidence", type=Path, required=True)

    google = commands.add_parser(
        "prepare-google-listing",
        parents=[_now_parent()],
        help="Prepare Google Business Profile edits and stop before Save",
    )
    google.add_argument("--order-id", type=int, required=True)
    google.add_argument("--page", required=True, help="Saved cabinet HTML, or an https address when ZHOL_GOOGLE_CABINET=1")
    google.add_argument("--evidence", type=Path, required=True)

    twogis_create = commands.add_parser(
        "prepare-2gis-create",
        parents=[_now_parent()],
        help="Prepare a new 2GIS listing and stop before Create",
    )
    twogis_create.add_argument("--order-id", type=int, required=True)
    twogis_create.add_argument("--page", required=True, help="Saved create-form HTML, or an https address when ZHOL_2GIS_CABINET=1")
    twogis_create.add_argument("--evidence", type=Path, required=True)

    yandex_create = commands.add_parser(
        "prepare-yandex-create",
        parents=[_now_parent()],
        help="Prepare a new Yandex Business listing and stop before Create",
    )
    yandex_create.add_argument("--order-id", type=int, required=True)
    yandex_create.add_argument("--page", required=True, help="Saved create-form HTML, or an https address when ZHOL_YANDEX_CABINET=1")
    yandex_create.add_argument("--evidence", type=Path, required=True)

    google_create = commands.add_parser(
        "prepare-google-create",
        parents=[_now_parent()],
        help="Prepare a new Google Business Profile listing and stop before Create",
    )
    google_create.add_argument("--order-id", type=int, required=True)
    google_create.add_argument("--page", required=True, help="Saved create-form HTML, or an https address when ZHOL_GOOGLE_CABINET=1")
    google_create.add_argument("--evidence", type=Path, required=True)

    approve = commands.add_parser(
        "approve-listing",
        parents=[_now_parent()],
        help="Approve one prepared listing draft for a later offline submit",
    )
    approve.add_argument("--order-id", type=int, required=True)
    approve.add_argument("--platform", required=True, choices=("2gis", "yandex", "google"))
    approve.add_argument("--operation", required=True, choices=("create", "update"))
    approve.add_argument("--page", required=True, help="Saved cabinet HTML showing the approved draft")
    approve.add_argument("--evidence", type=Path, required=True, help="Prepare evidence directory with review.json")

    submit = commands.add_parser(
        "submit-listing",
        parents=[_now_parent()],
        help="Simulate Save or Create for one approved draft. Live submit stays closed",
    )
    submit.add_argument("--order-id", type=int, required=True)
    submit.add_argument("--platform", required=True, choices=("2gis", "yandex", "google"))
    submit.add_argument("--operation", required=True, choices=("create", "update"))
    submit.add_argument("--page", required=True, help="Saved cabinet HTML with a fake platform response")
    submit.add_argument("--evidence", type=Path, required=True)
    submit.add_argument("--mode", default="offline", choices=("offline",), help="Only offline simulation is available")

    verify = commands.add_parser(
        "verify-listing",
        parents=[_now_parent()],
        help="Read a saved verification page. Publication is not decided by the submit click",
    )
    verify.add_argument("--order-id", type=int, required=True)
    verify.add_argument("--platform", required=True, choices=("2gis", "yandex", "google"))
    verify.add_argument("--operation", required=True, choices=("create", "update"))
    verify.add_argument("--page", required=True, help="Saved verification HTML")
    verify.add_argument("--evidence", type=Path, required=True)

    verify_order = commands.add_parser(
        "verify-order",
        parents=[_now_parent()],
        help="Classify stored listing evidence for one order. Does not open a cabinet",
    )
    verify_order.add_argument("--order-id", type=int, required=True)

    completion = commands.add_parser(
        "generate-completion-report",
        parents=[_now_parent()],
        help="Write the JSON and HTML completion report for one order",
    )
    completion.add_argument("--order-id", type=int, required=True)
    completion.add_argument("--out", type=Path, required=True)

    bek = commands.add_parser(
        "import-bek-audit",
        parents=[_now_parent()],
        help="Import a Bek audit workbook and store listing improvement plans",
    )
    bek.add_argument("--file", type=Path, required=True, help="xlsx workbook, for example karty-salony-almaty-2026-10.xlsx")

    planned = commands.add_parser(
        "bek-audit-plan",
        parents=[_now_parent()],
        help="Print the listing improvement plan for one company",
    )
    planned.add_argument("--company-id", type=int, required=True)

    demo = commands.add_parser("demo", parents=[_now_parent()], help="Start the local fixture demo on localhost")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--open", action="store_true", help="Open the demo page in the browser")

    jobs = commands.add_parser("jobs", help="Call the collection API without opening the panel")
    jobs.add_argument("--api", default="http://127.0.0.1:18080")
    jobs.add_argument("--token", default="")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    created = job_commands.add_parser("create")
    created.add_argument("--platform", required=True, choices=("2gis", "google", "yandex"))
    created.add_argument("--city", required=True)
    created.add_argument("--query", required=True)
    created.add_argument("--limit", type=int, default=20)
    created.add_argument("--idempotency-key", default=None)
    status = job_commands.add_parser("status")
    status.add_argument("job_id", type=int)
    results = job_commands.add_parser("results")
    results.add_argument("job_id", type=int)
    exported = job_commands.add_parser("export")
    exported.add_argument("job_id", type=int)
    exported.add_argument("--format", default="csv", choices=("xlsx", "csv", "json"))
    exported.add_argument("--out", type=Path, required=True)
    return parser


def _factory(factory: Any) -> Any:
    if factory is not None:
        return factory
    from zhol.db.base import get_session_factory

    try:
        return get_session_factory()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return None


def _import(session: Any, path: Path) -> None:
    from zhol.etalon import import_etalon

    stats = import_etalon(session, path)
    print(f"Импорт эталона: компаний +{stats.companies}, снимков +{stats.snapshots}, аудитов +{stats.audits}")
    for note in stats.skipped:
        print(f"- {note}")


def cmd_import_etalon(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.etalon import EtalonError

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            _import(session, args.csv)
    except EtalonError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def cmd_score_check(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.etalon import EtalonError, score_check

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            _import(session, args.csv)
            results = score_check(session, args.csv)
    except EtalonError as exc:
        print(exc, file=sys.stderr)
        return 2
    matched = sum(r.ok for r in results)
    print(f"Совпало {matched} из {len(results)}")
    for r in results:
        if not r.ok:
            print(f"- {r.id_2gis} {r.name}: ожидалось {r.expected}, получено {r.actual}")
    return 0 if results and matched == len(results) else 1


def cmd_scheduler_tick(args: argparse.Namespace, factory: Any = None, notifier: Any = None) -> int:
    import time

    from zhol.integrations import telegram
    from zhol.scheduler import tick

    factory = _factory(factory)
    if factory is None:
        return 2
    notifier = notifier or telegram.notify
    while True:
        try:
            result = tick(factory, notifier=notifier)
        except Exception as exc:
            if args.loop is None:
                raise
            print(f"scheduler tick failed: {type(exc).__name__}", file=sys.stderr)
        else:
            print(f"Тик {result.at.isoformat()}: поставлено {len(result.enqueued)}, уже было {len(result.already)}, dead {len(result.dead)}")
            for job_type, slot, job_id in result.enqueued:
                print(f"+ {job_type} {slot} (job {job_id})")
        if args.loop is None:
            return 0
        time.sleep(args.loop)


def cmd_eval(args: argparse.Namespace) -> int:
    if args.prompt in ("scout-extract", "scout-match") or args.point == "scout_fields":
        from zhol.agents.scout.golden import format_report, run_extract, run_match, run_scout_fields

        if args.prompt == "scout-extract":
            report = run_extract()
            print(format_report("scout-extract", report))
        elif args.prompt == "scout-match":
            report = run_match()
            print(format_report("scout-match", report))
        else:
            report = run_scout_fields()
            print(format_report("scout_fields", report))
        return 0 if report.ok else 1
    if args.prompt is not None or args.point not in EVAL_POINTS:
        name = args.prompt or args.point
        print(f"no golden set runner for {name!r} yet; supported: {', '.join(EVAL_POINTS)}, scout_fields", file=sys.stderr)
        return 2
    from zhol.review.evaluate import run_golden

    results = run_golden()
    passed = sum(r.ok for r in results)
    print(f"Прошло {passed} из {len(results)}")
    for r in results:
        if not r.ok:
            print(f"- {r.name}: {'; '.join(r.problems)}")
    return 0 if results and passed == len(results) else 1


def _git_diff(base: str) -> str:
    try:
        out = subprocess.run(
            ["git", "diff", base, "--", ".", ":(exclude)source"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(git diff unavailable: {type(exc).__name__})"
    return out.stdout


def cmd_council_dev(args: argparse.Namespace, gateway: Any = None) -> int:
    from zhol.llm.config import ConfigError
    from zhol.redact import redact
    from zhol.review import run

    stage_file = Path("docs/plan/stages") / f"{args.stage}.md"
    report_file = Path("docs/acceptance") / f"{args.stage}.md"
    missing = [str(p) for p in (report_file,) if not p.exists()]
    if args.stage != "FINAL" and not stage_file.exists():
        missing.insert(0, str(stage_file))
    if missing:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        return 2
    if gateway is None:
        from zhol.llm import get_gateway

        try:
            gateway = get_gateway()
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    materials = {
        "stage_file": stage_file.read_text(encoding="utf-8") if stage_file.exists() else None,
        "diff": redact(_git_diff(args.diff_base)),
        "tests_output": redact(args.tests_output.read_text(encoding="utf-8")) if args.tests_output else None,
    }
    result = run(
        "dev_stage",
        "stage",
        None,
        object_data={"stage": args.stage, "report": report_file.read_text(encoding="utf-8")},
        materials=materials,
        gateway=gateway,
        author_family=args.author,
        code_check=lambda _obj: [],
    )
    print(f"{args.stage}: {result.verdict}" + (f" ({result.reason})" if result.reason else ""))
    for group in result.groups:
        if group.get("status") == "accepted":
            print(f"{group.get('n')}. [{group['severity']}] {group['where']}: {group['what']} -> {group['fix']}")
    for note in result.notes:
        print(f"note [{note['severity']}] {note['where']}: {note['what']}")
    return 0 if result.verdict == "pass" else 1


def _read_ids(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _scout_live(args: argparse.Namespace) -> int:
    import os

    if args.etalon:
        print("live collection of all 50 is not part of this batch", file=sys.stderr)
        return 2
    if os.environ.get("ZHOL_SCOUT_LIVE") != "1":
        print("set ZHOL_SCOUT_LIVE=1 to run the 2GIS pilot", file=sys.stderr)
        return 2
    ids = _read_ids(args.ids_file)
    if sum(bool(flag) for flag in (args.manual, args.visible, args.maps)) > 1:
        print("choose one of --manual, --visible, --maps", file=sys.stderr)
        return 2
    if args.maps:
        if len(ids) != 2:
            print("maps read accepts exactly 2 company ids", file=sys.stderr)
            return 2
        from zhol.agents.scout.maps import run_maps

        return run_maps(ids)
    if args.visible:
        if len(ids) != 2:
            print("visible pilot accepts exactly 2 company ids", file=sys.stderr)
            return 2
        from zhol.agents.scout.pilot import run_visible

        return run_visible(ids)
    if args.manual:
        if len(ids) != 1:
            print("manual pilot accepts exactly 1 company id", file=sys.stderr)
            return 2
        from zhol.agents.scout.pilot import run_manual

        return run_manual(ids)
    if len(ids) != 3:
        print("live pilot accepts exactly 3 company ids", file=sys.stderr)
        return 2
    from zhol.agents.scout.pilot import run_pilot

    return run_pilot(ids)


def cmd_scout(args: argparse.Namespace, factory: Any = None, notifier: Any = None) -> int:
    if args.manual and not args.live:
        print("manual pilot requires --live", file=sys.stderr)
        return 2
    if args.visible and not args.live:
        print("visible pilot requires --live", file=sys.stderr)
        return 2
    if args.maps and not args.live:
        print("maps read requires --live", file=sys.stderr)
        return 2
    if args.live:
        return _scout_live(args)
    from zhol.agents.scout.jobs import drain, enqueue_collect
    from zhol.browser.fixtures import DEFAULT_FIXTURES
    from zhol.db.models import Company

    factory = _factory(factory)
    if factory is None:
        return 2
    fixtures = args.fixtures or DEFAULT_FIXTURES
    if args.etalon:
        from zhol.etalon import EtalonError, read_rows

        try:
            ids = [row["id_2gis"].strip() for row in read_rows()]
        except EtalonError as exc:
            print(exc, file=sys.stderr)
            return 2
    else:
        ids = _read_ids(args.ids_file)
    if not ids:
        print("companies not found", file=sys.stderr)
        return 2
    notifier = notifier or (lambda _text: None)
    with factory() as session, session.begin():
        from sqlalchemy import select

        companies = {row.id_2gis: row for row in session.scalars(select(Company).where(Company.id_2gis.in_(ids)))}
        missing = [item for item in ids if item not in companies]
        if missing:
            print(f"companies not found: {', '.join(missing)}", file=sys.stderr)
            return 2
        queued = already = 0
        for item in ids:
            _job_id, existed = enqueue_collect(
                session, companies[item].id, args.purpose, str(fixtures)
            )
            if existed:
                already += 1
            else:
                queued += 1
    print(f"Сбор: поставлено {queued}, уже было {already}")
    if args.enqueue_only:
        return 0
    drain(factory, notifier=notifier)
    return 0


def cmd_scout_batch_check(args: argparse.Namespace, factory: Any = None) -> int:
    from datetime import date, datetime, time

    from zhol.agents.scout.formula import check_since
    from zhol.timeutil import ALMATY

    factory = _factory(factory)
    if factory is None:
        return 2
    text = args.since
    if len(text) == 10:
        since = datetime.combine(date.fromisoformat(text), time(0, 0), tzinfo=ALMATY)
    else:
        since = parse_now(text)
    with factory() as session, session.begin():
        reasons = check_since(session, since)
    if not reasons:
        print("Проверка пачки: зависимостей нет")
        return 0
    print("Проверка пачки: найдено")
    for reason in reasons:
        print(f"- {reason}")
    return 1


def cmd_scout_compare(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.agents.scout.compare import compare_companies

    factory = _factory(factory)
    if factory is None:
        return 2
    with factory() as session:
        rows = compare_companies(session)
    scout_rows = [row for row in rows if row.new is not None or row.screenshot]
    print(f"Строк сравнения: {len(rows)}")
    for row in rows:
        old = "" if row.old is None else row.old
        new = "" if row.new is None else row.new
        print(f"{row.id_2gis}\t{row.platform}\t{row.field}\t{old}\t{new}\t{'да' if row.match else 'нет'}\t{row.screenshot or ''}")
    if not scout_rows:
        print("снимков разведчика: 0")
    return 0


def _discovery_resume(factory: Any, resume: int | None) -> bool:
    if resume is None or factory is None:
        return False
    from zhol.db.models import CollectionBatch

    with factory() as session:
        batch = session.get(CollectionBatch, resume)
    if batch is None:
        return False
    return batch.source_mode == "manual" or batch.progress is not None


def cmd_scout_batch(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.agents.scout.batch import run_batch
    from zhol.agents.scout.catalog import BatchError
    from zhol.agents.scout.discover import run_discovery

    if args.source_mode == "live":
        try:
            run_batch(
                factory,
                city=args.city,
                category=args.category,
                platforms=tuple(args.platforms.split(",")),
                limit=args.limit,
                out=args.out,
                source_mode="live",
                catalog=args.catalog,
                fixtures_dir=args.fixtures,
                resume=args.resume,
            )
        except BatchError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 2
    factory = _factory(factory)
    if factory is None:
        return 2
    discover = args.discovery is not None or args.source_mode == "manual" or _discovery_resume(factory, args.resume)
    try:
        if discover:
            if args.source_mode == "manual" and args.discovery is None and args.resume is None:
                raise BatchError("manual discovery needs --discovery")
            counts = run_discovery(
                factory,
                city=args.city,
                category=args.category,
                platforms=tuple(args.platforms.split(",")),
                limit=args.limit,
                out=args.out,
                source_mode=args.source_mode,
                root=args.discovery or args.catalog,
                fixtures_dir=args.fixtures,
                resume=args.resume,
            )
        else:
            counts = run_batch(
                factory,
                city=args.city,
                category=args.category,
                platforms=tuple(args.platforms.split(",")),
                limit=args.limit,
                out=args.out,
                source_mode=args.source_mode,
                catalog=args.catalog,
                fixtures_dir=args.fixtures,
                resume=args.resume,
            )
    except BatchError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"batch {counts.batch_id}")
    print(
        "collected {completed}, failed {failed}, blocked {blocked}, skipped {skipped}, duplicate {duplicate}, pending {pending}".format(
            completed=counts.completed,
            failed=counts.failed,
            blocked=counts.blocked,
            skipped=counts.skipped,
            duplicate=counts.duplicate,
            pending=counts.pending,
        )
    )
    print(f"Excel: {counts.excel} строк {counts.rows}")
    if discover:
        from zhol.db.models import CollectionBatch

        with factory() as session:
            batch = session.get(CollectionBatch, counts.batch_id)
        progress = {} if batch is None else (batch.progress or {})
        print(
            "progress pages {pages}, scrolls {scrolls}, hits {hits}, duplicates {duplicates}, missing {missing}, status {status}".format(
                pages=progress.get("pages") or 0,
                scrolls=progress.get("scrolls") or 0,
                hits=progress.get("hits") or 0,
                duplicates=progress.get("duplicates") or 0,
                missing=len(progress.get("missing") or []),
                status=batch.status if batch is not None else "",
            )
        )
    return 0 if counts.failed == 0 and counts.blocked == 0 else 1


def cmd_export_mailing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.export.mailing import load_rows, write_xlsx

    factory = _factory(factory)
    if factory is None:
        return 2
    with factory() as session:
        rows = load_rows(session)
    count = write_xlsx(rows, args.out)
    print(f"Excel: {count} строк, {args.out}")
    return 0


def cmd_create_test_order(args: argparse.Namespace, factory: Any = None) -> int:
    import json

    from zhol.orders.intake import OrderIntakeError, create_test_order

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print("spec must be a JSON object", file=sys.stderr)
        return 2
    try:
        with factory() as session, session.begin():
            order = create_test_order(session, spec)
            order_id, stage = order.id, order.stage
    except OrderIntakeError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"order {order_id} stage {stage}")
    return 0


def cmd_order_status(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.orders.intake import format_status, order_status

    factory = _factory(factory)
    if factory is None:
        return 2
    with factory() as session:
        view = order_status(session, args.order_id)
    if view is None:
        print("order not found", file=sys.stderr)
        return 2
    print(format_status(view))
    return 0


def cmd_check_order_readiness(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.orders.intake import check_readiness, format_readiness

    factory = _factory(factory)
    if factory is None:
        return 2
    with factory() as session:
        report = check_readiness(session, args.order_id)
    if report is None:
        print("order not found", file=sys.stderr)
        return 2
    print(format_readiness(report))
    return 0 if report.status == "ready" else 1


def cmd_prepare_content(args: argparse.Namespace, factory: Any = None, gateway: Any = None) -> int:
    from zhol.content.prepare import ContentError, format_report, prepare_content
    from zhol.llm.config import ConfigError

    factory = _factory(factory)
    if factory is None:
        return 2
    if gateway is None:
        from zhol.llm import get_gateway

        try:
            gateway = get_gateway()
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 2
    try:
        with factory() as session, session.begin():
            report = prepare_content(session, args.order_id, gateway=gateway, output_dir=args.out)
    except ContentError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


def cmd_content_status(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.content.prepare import content_status, format_report

    factory = _factory(factory)
    if factory is None:
        return 2
    with factory() as session:
        report = content_status(session, args.order_id)
    if report is None:
        print("order not found", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


def cmd_dry_run_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.fulfillment.engine import FulfillmentError, FulfillmentRefused, format_run, run_listing

    factory = _factory(factory)
    if factory is None:
        return 2
    pages = _saved_pages(args.pages)
    try:
        with factory() as session, session.begin():
            report = run_listing(session, args.order_id, pages=pages, evidence_dir=args.evidence)
    except (FulfillmentError, FulfillmentRefused) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(format_run(report))
    return 0 if report.ok else 1


def cmd_prepare_yandex_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.browser.yandex.prepare import format_prepare, prepare_yandex_listing

    return _cmd_prepare_listing(args, factory, prepare_yandex_listing, format_prepare)


def cmd_prepare_2gis_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.browser.twogis.prepare import format_prepare, prepare_2gis_listing

    return _cmd_prepare_listing(args, factory, prepare_2gis_listing, format_prepare)


def cmd_prepare_google_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.browser.google.prepare import format_prepare, prepare_google_listing

    return _cmd_prepare_listing(args, factory, prepare_google_listing, format_prepare)


def cmd_prepare_2gis_create(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.browser.twogis.create import format_prepare, prepare_2gis_create

    return _cmd_prepare_listing(args, factory, prepare_2gis_create, format_prepare)


def cmd_prepare_yandex_create(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.browser.yandex.create import format_prepare, prepare_yandex_create

    return _cmd_prepare_listing(args, factory, prepare_yandex_create, format_prepare)


def cmd_prepare_google_create(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.browser.google.create import format_prepare, prepare_google_create

    return _cmd_prepare_listing(args, factory, prepare_google_create, format_prepare)


def cmd_approve_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.fulfillment.submission import approve_listing, format_submit

    return _cmd_submit(args, factory, approve_listing, format_submit)


def cmd_submit_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.fulfillment.submission import format_submit, submit_listing

    return _cmd_submit(args, factory, submit_listing, format_submit)


def cmd_verify_listing(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.fulfillment.submission import format_submit, verify_listing

    return _cmd_submit(args, factory, verify_listing, format_submit)


def cmd_verify_order(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.reports.completion import CompletionError, format_verification, verify_order

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            document = verify_order(session, args.order_id)
    except CompletionError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(format_verification(document))
    return 0


def cmd_generate_completion_report(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.reports.completion import CompletionError, format_verification, generate_completion_report

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            result = generate_completion_report(session, args.order_id, args.out)
    except CompletionError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(format_verification(result["report"]))
    print(f"json {result['json']}")
    print(f"html {result['html']}")
    return 0


def _cmd_submit(args: argparse.Namespace, factory: Any, run: Any, format_report: Any) -> int:
    from zhol.fulfillment.engine import FulfillmentError, FulfillmentRefused

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            report = run(
                session,
                args.order_id,
                platform=args.platform,
                operation=args.operation,
                page=args.page,
                evidence_dir=args.evidence,
            )
    except (FulfillmentError, FulfillmentRefused, PermissionError, FileNotFoundError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(format_report(report))
    return 0 if report.status in {"approved", "submitted", "moderation_pending", "published"} else 1


def _cmd_prepare_listing(args: argparse.Namespace, factory: Any, prepare: Any, format_prepare: Any) -> int:
    from zhol.fulfillment.engine import FulfillmentError, FulfillmentRefused

    factory = _factory(factory)
    if factory is None:
        return 2
    wait = 180 if str(args.page).startswith("https://") else 0
    try:
        with factory() as session, session.begin():
            report = prepare(
                session,
                args.order_id,
                page=args.page,
                evidence_dir=args.evidence,
                wait_seconds=wait,
            )
    except (FulfillmentError, FulfillmentRefused, PermissionError, FileNotFoundError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(format_prepare(report))
    return 0 if report.status in {"prepared", "already"} else 1


def _saved_pages(folder: Path | None) -> dict[str, str] | None:
    if folder is None:
        return None
    pages: dict[str, str] = {}
    for name in ("2gis", "yandex", "google"):
        path = folder / f"{name}.html"
        if path.is_file():
            pages[name] = str(path)
    return pages


def cmd_import_contacts(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.agents.scout.contacts import ContactsError, import_contacts

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            stats = import_contacts(session, args.csv)
    except ContactsError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Импорт контактов: компаний +{stats.companies}, пропущено {stats.skipped}")
    return 0


def cmd_import_bek_audit(args: argparse.Namespace, factory: Any = None) -> int:
    from zhol.audit.bek import BekAuditError, import_bek_audit

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            stats = import_bek_audit(session, args.file)
    except BekAuditError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        "Импорт аудита Бека: "
        f"лист {stats.sheet}, компании +{stats.companies}, снимки +{stats.snapshots}, "
        f"аудиты +{stats.audits}, планы +{stats.plans}, без пары {len(stats.unmatched)}, "
        f"спорные {len(stats.ambiguous)}, новее не тронуто {len(stats.skipped_newer)}"
    )
    if stats.tasks_by_type:
        parts = ", ".join(f"{key}={stats.tasks_by_type[key]}" for key in sorted(stats.tasks_by_type))
        print(f"задачи: {parts}")
    for record in stats.unmatched:
        print(f"- без пары строка {record['row']}: {record['reason']}")
    for record in stats.ambiguous:
        print(f"- спорно: {record.get('reason')}")
    return 0


def cmd_bek_audit_plan(args: argparse.Namespace, factory: Any = None) -> int:
    import json

    from zhol.audit.bek import BekAuditError, company_plan

    factory = _factory(factory)
    if factory is None:
        return 2
    try:
        with factory() as session, session.begin():
            plan = company_plan(session, args.company_id)
    except BekAuditError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None, *, gateway: Any = None, factory: Any = None, notifier: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        return cmd_demo(args)
    if args.command == "jobs":
        return cmd_jobs(args)
    if getattr(args, "now", None) is not None:
        set_now(parse_now(args.now))
    if args.command == "eval":
        return cmd_eval(args)
    if args.command == "council-dev":
        return cmd_council_dev(args, gateway)
    if args.command == "import-etalon":
        return cmd_import_etalon(args, factory)
    if args.command == "score-check":
        return cmd_score_check(args, factory)
    if args.command == "scheduler-tick":
        return cmd_scheduler_tick(args, factory, notifier)
    if args.command == "scout":
        return cmd_scout(args, factory, notifier)
    if args.command == "scout-batch-check":
        return cmd_scout_batch_check(args, factory)
    if args.command == "scout-compare":
        return cmd_scout_compare(args, factory)
    if args.command == "import-contacts":
        return cmd_import_contacts(args, factory)
    if args.command == "export-mailing":
        return cmd_export_mailing(args, factory)
    if args.command == "scout-batch":
        return cmd_scout_batch(args, factory)
    if args.command == "create-test-order":
        return cmd_create_test_order(args, factory)
    if args.command == "order-status":
        return cmd_order_status(args, factory)
    if args.command == "check-order-readiness":
        return cmd_check_order_readiness(args, factory)
    if args.command == "prepare-content":
        return cmd_prepare_content(args, factory, gateway)
    if args.command == "content-status":
        return cmd_content_status(args, factory)
    if args.command == "dry-run-listing":
        return cmd_dry_run_listing(args, factory)
    if args.command == "prepare-yandex-listing":
        return cmd_prepare_yandex_listing(args, factory)
    if args.command == "prepare-2gis-listing":
        return cmd_prepare_2gis_listing(args, factory)
    if args.command == "prepare-google-listing":
        return cmd_prepare_google_listing(args, factory)
    if args.command == "prepare-2gis-create":
        return cmd_prepare_2gis_create(args, factory)
    if args.command == "prepare-yandex-create":
        return cmd_prepare_yandex_create(args, factory)
    if args.command == "prepare-google-create":
        return cmd_prepare_google_create(args, factory)
    if args.command == "approve-listing":
        return cmd_approve_listing(args, factory)
    if args.command == "submit-listing":
        return cmd_submit_listing(args, factory)
    if args.command == "verify-listing":
        return cmd_verify_listing(args, factory)
    if args.command == "verify-order":
        return cmd_verify_order(args, factory)
    if args.command == "generate-completion-report":
        return cmd_generate_completion_report(args, factory)
    if args.command == "import-bek-audit":
        return cmd_import_bek_audit(args, factory)
    if args.command == "bek-audit-plan":
        return cmd_bek_audit_plan(args, factory)
    print(now().isoformat())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from zhol.demo.server import DemoLaunchError, serve

    try:
        message = serve(args.host, args.port, open_browser=args.open)
    except DemoLaunchError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    if message:
        print(message)
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    import json
    import os

    from zhol.integration.client import create_job, export_job, job_results, job_status
    from zhol.integration.schema import IntegrationError

    token = args.token or os.environ.get("ZHOL_ADMIN_TOKEN", "")
    try:
        if args.jobs_command == "create":
            body = create_job(
                args.api,
                token,
                platform=args.platform,
                city=args.city,
                query=args.query,
                limit=args.limit,
                idempotency_key=args.idempotency_key,
            )
        elif args.jobs_command == "status":
            body = job_status(args.api, token, args.job_id)
        elif args.jobs_command == "results":
            body = job_results(args.api, token, args.job_id)
        else:
            export_job(args.api, token, args.job_id, args.format, args.out)
            print(args.out)
            return 0
    except IntegrationError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
