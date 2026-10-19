"""Пакетный сбор карточек 2ГИС по Алматы.

Без --debug-limit каждый запрос идёт до подтверждённого конца выдачи.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from excel_writer import ExcelWriteError
from parser_service import ChromiumMissing, ManualConfirmationRequired, run_batch

CATEGORIES_PATH = Path(__file__).resolve().parent / "categories.json"
DEFAULT_OUTPUT = Path("output") / "almaty_businesses.xlsx"


def delay_range_text(value: str) -> tuple[float, float]:
    text = value.strip().replace("–", "-").replace("—", "-")
    parts = text.split("-", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError("Параметр --delay задаётся как минимум-максимум, например 2-5.")
    try:
        low, high = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Параметр --delay задаётся как минимум-максимум, например 2-5.") from exc
    if low < 0 or high < low:
        raise argparse.ArgumentTypeError("Параметр --delay: минимум не больше максимума, оба числа неотрицательные.")
    return (low, high)


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Нужно положительное число.") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("Нужно положительное число.")
    return number


def load_categories(path: Path) -> list[tuple[str, list[str]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Не прочитан {path.name}: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise SystemExit(f"{path.name} должен быть непустым объектом групп.")
    groups: list[tuple[str, list[str]]] = []
    for name, queries in payload.items():
        if not isinstance(queries, list) or not queries:
            raise SystemExit(f"Группа «{name}» должна содержать список запросов.")
        cleaned = []
        for query in queries:
            text = str(query).strip()
            if not text:
                raise SystemExit(f"В группе «{name}» есть пустой запрос.")
            cleaned.append(text)
        groups.append((str(name), cleaned))
    return groups


def canonical_query(text: str, groups: list[tuple[str, list[str]]]) -> tuple[str, str]:
    folded = text.casefold()
    for group, queries in groups:
        for query in queries:
            if query.casefold() == folded:
                return group, query
    return "Запрос", text.strip()


def select_jobs(args: argparse.Namespace, groups: list[tuple[str, list[str]]]) -> list[dict]:
    if args.group and args.query:
        raise SystemExit("Укажите либо --group, либо --query.")
    if args.group:
        folded = args.group.casefold()
        match = next((item for item in groups if item[0].casefold() == folded), None)
        if match is None:
            names = ", ".join(name for name, _queries in groups)
            raise SystemExit(f"Группа «{args.group}» не найдена. Доступно: {names}.")
        selected = [match]
    elif args.query:
        query_text = args.query.strip()
        if not query_text:
            raise SystemExit("Параметр --query не должен быть пустым.")
        group, query = canonical_query(query_text, groups)
        if group == "Запрос":
            selected = [(group, [query])]
        else:
            full = next(queries for name, queries in groups if name == group)
            selected = [(group, full)]
            args._only_query = query
    else:
        selected = groups

    only_query = getattr(args, "_only_query", None)
    jobs = []
    for group, queries in selected:
        for index, query in enumerate(queries, start=1):
            if only_query is not None and query != only_query:
                continue
            jobs.append(
                {
                    "group": group,
                    "query": query,
                    "index": index,
                    "group_size": len(queries),
                }
            )
    if args.max_queries is not None:
        jobs = jobs[: args.max_queries]
    if not jobs:
        raise SystemExit("Нет запросов для сбора.")
    return jobs


def resume_hint(args: argparse.Namespace, output: Path) -> str:
    parts = ["python collect_almaty.py", "--resume", "--output", str(output)]
    if args.group:
        parts.extend(["--group", args.group])
    if args.query:
        parts.extend(["--query", args.query])
    return " ".join(parts)


def print_summary(summary: dict) -> None:
    output = summary["output"]
    if summary["outcome"] == "completed":
        print("================================")
        print("COMPLETED")
        print(f"City: {summary['city']}")
        print(f"Groups: {summary['groups']}")
        print(f"Queries completed: {summary['queries_completed']}")
        print(f"Queries interrupted: 0")
        if summary["queries_unconfirmed"]:
            print(f"Queries unconfirmed: {summary['queries_unconfirmed']}")
        print(f"Unique companies: {summary['unique']}")
        print(f"Duplicates skipped: {summary['duplicates']}")
        print(f"Errors: {summary['errors']}")
        print(f"Output: {output}")
        print("================================")
        return
    print("================================")
    print("INTERRUPTED")
    print(f"Completed queries: {summary['queries_completed']}")
    print(f"Incomplete queries: {summary['incomplete_queries']}")
    print(f"Unique companies saved: {summary['unique']}")
    print("Resume with:")
    print(summary["resume_hint"])
    print("================================")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать карточки 2ГИС по категориям Алматы.",
    )
    parser.add_argument("--group", help="Только одна группа из categories.json, например Красота")
    parser.add_argument("--query", help="Только один запрос, например кофейни")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Куда писать Excel. По умолчанию {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Продолжить файл: пропустить сохранённые филиалы и уже завершённые запросы",
    )
    parser.add_argument(
        "--debug-limit",
        type=positive_int,
        help="Отладочный предел карточек на один запрос. Обычный сбор идёт без предела",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-queries",
        type=positive_int,
        help="Взять только первые N запросов. Только для проверки",
    )
    parser.add_argument(
        "--delay",
        type=delay_range_text,
        help="Пауза между карточками в секундах, например 2-5. По умолчанию 0-0.3",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Скрыть окно Chrome. Если 2ГИС попросит подтверждение, сбор остановится и сохранит Excel",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit is not None:
        print(
            "Параметр --limit устарел и работает как --debug-limit. Обычный сбор без предела.",
            file=sys.stderr,
        )
        if args.debug_limit is not None:
            raise SystemExit("Укажите только --debug-limit.")
        args.debug_limit = args.limit
    groups = load_categories(CATEGORIES_PATH)
    jobs = select_jobs(args, groups)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)
    hint = resume_hint(args, output)
    try:
        summary = run_batch(
            jobs,
            output,
            headless=args.headless,
            resume=args.resume,
            debug_limit=args.debug_limit,
            delay_range=args.delay,
            resume_hint=hint,
        )
    except ManualConfirmationRequired as exc:
        print(exc)
        print("Уже собранные строки записаны.")
        print("Resume with:")
        print(hint)
        raise SystemExit(2) from exc
    except ChromiumMissing as exc:
        print(exc)
        raise SystemExit(1) from exc
    except ExcelWriteError as exc:
        print(exc)
        raise SystemExit(1) from exc
    print_summary(summary)
    if summary["outcome"] == "confirmation":
        raise SystemExit(2)
    if summary["outcome"] == "interrupted":
        raise SystemExit(130)


if __name__ == "__main__":
    main()
