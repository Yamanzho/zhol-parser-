"""Three-company public 2GIS pilot. It does not open Yandex or Google."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from zhol.agents.scout.extract import extract_card
from zhol.agents.scout.fields import field_keys
from zhol.browser.config import load_platforms
from zhol.browser.fixtures import LoadedPage
from zhol.browser.live import LiveCollectionDisabled
from zhol.browser.page import AccessRestricted, PageNotLoaded, UnexpectedPage
from zhol.browser.twogis.capture import BrowserUnavailable, capture_firm
from zhol.browser.twogis.visible import live_spec
from zhol.scoring.adapter import COLUMNS, PRICES_2GIS
from zhol.timeutil import now

PILOT_SIZE = 3
MANUAL_SIZE = 1
VISIBLE_SIZE = 2
MANUAL_PAUSE_SECONDS = 180
DEFAULT_OUTPUT = Path("data/scout-pilot")
DEFAULT_REPORT = Path("docs/pilot/s1-batch2-2gis.md")
MANUAL_OUTPUT = Path("data/scout-pilot/manual")
MANUAL_REPORT = Path("docs/pilot/s1-batch2-1-manual.md")
VISIBLE_OUTPUT = Path("data/scout-pilot/e2")
VISIBLE_REPORT = Path("docs/pilot/e2-2gis.md")
MAILING_PATH = Path("data/export/mailing.xlsx")
_PHONE_KEYS = {"phones", "whatsapp"}


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits


def _norm(key: str, value: str) -> str:
    text = value.strip()
    if key in _PHONE_KEYS:
        return _digits(text)
    if key == "instagram":
        text = text.split("?")[0].rstrip("/")
        text = text.split("/")[-1]
        return text[1:] if text.startswith("@") else text.lower()
    if key in ("name", "address", "link"):
        return " ".join(text.casefold().split())
    if key == "rating":
        try:
            return f"{float(text.replace(',', '.')):.1f}"
        except ValueError:
            return text.casefold()
    if key in ("ratings_count", "reviews_count", "photos_count", "reviews_shown", "replies_count", "branches"):
        try:
            return str(int(text))
        except ValueError:
            return text.casefold()
    return text.casefold()


def _as_text(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if key == "prices":
        return {item: label for label, item in PRICES_2GIS.items()}.get(str(value), str(value))
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def compare_reference(fields: dict[str, Any], row: dict[str, str]) -> list[dict[str, str]]:
    diffs = []
    for platform, key, column in COLUMNS:
        if platform != "2gis":
            continue
        raw = (row.get(column) or "").strip()
        value = fields.get(key)
        if value is None:
            kind = "same" if raw == "" else "missing"
        else:
            kind = "same" if _norm(key, _as_text(key, value)) == _norm(key, raw) else "different"
        if kind != "same":
            diffs.append({"field": key, "reference": raw, "extracted": "" if value is None else _as_text(key, value), "kind": kind})
    return diffs


def _notes(folder: Path) -> dict[str, str]:
    path = folder / "notes.txt"
    if not path.is_file():
        return {}
    notes = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            notes[key] = value
    return notes


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run_pilot(
    ids: list[str],
    *,
    output: Path = DEFAULT_OUTPUT,
    report_path: Path = DEFAULT_REPORT,
    sleeper: Callable[[float], None] = time.sleep,
    opener: Callable[..., LoadedPage] = capture_firm,
) -> int:
    if len(ids) != PILOT_SIZE:
        print(f"live pilot accepts exactly {PILOT_SIZE} company ids")
        return 2
    cfg = load_platforms()
    spec = live_spec(cfg)
    code_fields = list(cfg["platforms"]["2gis"]["code_fields"])
    model_fields = list(cfg["platforms"]["2gis"]["model_fields"])
    from zhol.etalon import read_rows

    reference = {row["id_2gis"].strip(): row for row in read_rows()}
    lines = [
        "# Пилот 2ГИС: 3 компании эталона",
        "",
        f"Дата: {now().isoformat()}",
        "",
        "Яндекс и Google не открывались. Ограничения площадки не обходились.",
        "",
    ]
    blocked = False
    exit_code = 0
    last: Any = None
    for id_2gis in ids:
        if blocked:
            lines.extend([f"## {id_2gis}", "", "Не открывалась: предыдущая страница ограничила доступ.", ""])
            continue
        row = reference.get(id_2gis)
        url = cfg["platforms"]["2gis"]["card_url"].format(id=id_2gis)
        folder = output / id_2gis
        wait = 0 if last is None else float(cfg["rate_limit_seconds"])
        if wait:
            sleeper(wait)
        last = now()
        lines.extend([f"## {id_2gis}", "", f"Название в эталоне: {row['название'] if row else 'нет в эталоне'}", f"Адрес страницы: {url}"])
        try:
            page = opener(url, folder, spec)
        except (AccessRestricted, BrowserUnavailable) as exc:
            blocked = True
            exit_code = 2
            lines.extend(
                [
                    "Результат: доступ ограничен, сбор остановлен.",
                    f"Причина: {exc}",
                    f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                    "",
                ]
            )
            continue
        except (PageNotLoaded, UnexpectedPage, LiveCollectionDisabled) as exc:
            exit_code = 1
            lines.extend(
                [
                    "Результат: страница не разобрана.",
                    f"Причина: {type(exc).__name__}: {exc}",
                    f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                    "",
                ]
            )
            continue
        seen = now().isoformat()
        fields, sources = extract_card(
            "2gis",
            page.document,
            url=page.url,
            seen_at=seen,
            screenshot=page.screenshot_path,
            raw_path=page.raw_path,
            code_fields=code_fields,
            model_fields=model_fields,
        )
        notes = _notes(folder)
        missing = []
        for key in field_keys("2gis"):
            if fields.get(key) is None:
                missing.append(f"{key}: {notes.get(key) or (sources.get(key) or {}).get('reason') or 'not_shown'}")
        diffs = compare_reference(fields, row) if row else []
        stored = _store(id_2gis, page)
        lines.extend(
            [
                f"Фактический адрес: {page.url}",
                f"Скриншот: {page.screenshot_path}",
                f"Текст страницы: {folder / 'text.txt'}",
                "Результат: поля, которые читаются из видимого текста, записаны. Остальные оставлены пустыми.",
                f"База: {stored}",
                "Пустые поля:",
            ]
        )
        lines.extend(f"- {item}" for item in missing)
        lines.append("Расхождения с эталоном 30.09:")
        if not diffs:
            lines.append("- нет")
        else:
            lines.extend(
                f"- {item['field']}: было {item['reference'] or 'пусто'}, стало {item['extracted'] or 'пусто'} ({item['kind']})"
                for item in diffs
            )
        lines.append("")
    lines.append("Модель для категории не вызывалась (R6).")
    _write(report_path, lines)
    print(f"Отчёт: {report_path}")
    return exit_code


def run_manual(
    ids: list[str],
    *,
    output: Path = MANUAL_OUTPUT,
    report_path: Path = MANUAL_REPORT,
    previous: Path = DEFAULT_OUTPUT,
    pause_seconds: float = MANUAL_PAUSE_SECONDS,
    opener: Callable[..., LoadedPage] = capture_firm,
) -> int:
    """One visible 2GIS page. A robot check is left for the person at the computer."""
    if len(ids) != MANUAL_SIZE:
        print("manual pilot accepts exactly 1 company id")
        return 2
    id_2gis = ids[0]
    cfg = load_platforms()
    spec = live_spec(cfg)
    code_fields = list(cfg["platforms"]["2gis"]["code_fields"])
    model_fields = list(cfg["platforms"]["2gis"]["model_fields"])
    from zhol.browser.twogis.access import diagnose_saved
    from zhol.etalon import read_rows

    reference = {row["id_2gis"].strip(): row for row in read_rows()}
    row = reference.get(id_2gis)
    url = cfg["platforms"]["2gis"]["card_url"].format(id=id_2gis)
    saved_folder = previous / id_2gis
    lines = [
        "# Ручной пилот 2ГИС: 1 компания",
        "",
        f"Дата: {now().isoformat()}",
        "",
        "Открыта одна карточка в обычном видимом браузере. Другие адреса, профили и аккаунты не использовались.",
        "Яндекс и Google не открывались. Проверка на робота программой не решалась.",
        "",
    ]
    if (saved_folder / "text.txt").is_file():
        saved = diagnose_saved(saved_folder, http_status=200)
        lines.extend(
            [
                "## Прошлый автоматический заход",
                "",
                f"Доступ: {saved['access']}",
                f"Итог: {saved['outcome']}",
                f"Скриншот: {saved['screenshot']}",
                "Поля компании из этой страницы не читались.",
                "",
            ]
        )
    folder = output / id_2gis
    lines.extend([f"## {id_2gis}", "", f"Название в эталоне: {row['название'] if row else 'нет в эталоне'}", f"Адрес страницы: {url}", ""])
    exit_code = 0
    try:
        page = opener(url, folder, spec, headed=True, pause_seconds=pause_seconds)
    except (AccessRestricted, BrowserUnavailable, PageNotLoaded, UnexpectedPage, LiveCollectionDisabled) as exc:
        access = getattr(exc, "access", None) or ""
        outcome = getattr(exc, "outcome", None) or ""
        exit_code = 2 if isinstance(exc, (AccessRestricted, BrowserUnavailable)) else 1
        lines.extend(
            [
                f"Доступ: {access or type(exc).__name__}",
                f"Итог: {outcome or 'stopped'}",
                f"Причина: {exc}",
                f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                "Поля компании не читались.",
                "",
            ]
        )
        lines.append("Модель для категории не вызывалась (R6).")
        _write(report_path, lines)
        print(f"Отчёт: {report_path}")
        return exit_code
    seen = now().isoformat()
    fields, sources = extract_card(
        "2gis",
        page.document,
        url=page.url,
        seen_at=seen,
        screenshot=page.screenshot_path,
        raw_path=page.raw_path,
        code_fields=code_fields,
        model_fields=model_fields,
    )
    notes = _notes(folder)
    missing = []
    for key in field_keys("2gis"):
        if fields.get(key) is None:
            missing.append(f"{key}: {notes.get(key) or (sources.get(key) or {}).get('reason') or 'not_shown'}")
    diffs = compare_reference(fields, row) if row else []
    stored = _store(id_2gis, page)
    lines.extend(
        [
            "Доступ: card_visible",
            "Итог: collected",
            f"Фактический адрес: {page.url}",
            f"Время просмотра: {seen}",
            f"Скриншот: {page.screenshot_path}",
            f"Текст страницы: {folder / 'text.txt'}",
            f"База: {stored}",
            "Пустые поля:",
        ]
    )
    lines.extend(f"- {item}" for item in missing)
    lines.append("Расхождения с эталоном 30.09:")
    if not diffs:
        lines.append("- нет")
    else:
        lines.extend(
            f"- {item['field']}: было {item['reference'] or 'пусто'}, стало {item['extracted'] or 'пусто'} ({item['kind']})"
            for item in diffs
        )
    lines.extend(["", "Модель для категории не вызывалась (R6)."])
    _write(report_path, lines)
    print(f"Отчёт: {report_path}")
    return exit_code


def _export_mailing() -> str:
    try:
        from zhol.db.base import get_session_factory

        factory = get_session_factory()
    except RuntimeError as exc:
        return str(exc)
    from zhol.export.mailing import load_categories, load_rows, write_xlsx

    with factory() as session:
        rows = load_rows(session, load_categories())
    count = write_xlsx(rows, MAILING_PATH)
    return f"{MAILING_PATH} строк {count}"


def run_visible(
    ids: list[str],
    *,
    output: Path = VISIBLE_OUTPUT,
    report_path: Path = VISIBLE_REPORT,
    pause_seconds: float = MANUAL_PAUSE_SECONDS,
    reader: Callable[..., LoadedPage] | None = None,
    exporter: Callable[[], str] | None = None,
) -> int:
    """Two public 2GIS cards in one visible browser. A robot check stops the second card."""
    if len(ids) != VISIBLE_SIZE:
        print(f"visible pilot accepts exactly {VISIBLE_SIZE} company ids")
        return 2
    from zhol.browser.twogis.capture import capture_firms, open_sequence
    from zhol.export.mailing import classify, load_categories

    cfg = load_platforms()
    spec = live_spec(cfg)
    code_fields = list(cfg["platforms"]["2gis"]["code_fields"])
    model_fields = list(cfg["platforms"]["2gis"]["model_fields"])
    from zhol.etalon import read_rows

    reference = {row["id_2gis"].strip(): row for row in read_rows()}
    table = load_categories()
    pairs = [(cfg["platforms"]["2gis"]["card_url"].format(id=id_2gis), output / id_2gis) for id_2gis in ids]
    lines = [
        "# Пилот 2ГИС: 2 оставшиеся компании эталона",
        "",
        f"Дата: {now().isoformat()}",
        "",
        "Открыты две карточки в одном обычном видимом браузере.",
        "Яндекс и Google не открывались. Проверка на робота программой не решалась.",
        "Объявления компаний не менялись.",
        "",
    ]
    if reader is None:
        results = capture_firms(
            pairs,
            spec,
            headed=True,
            pause_seconds=pause_seconds,
            wait_seconds=float(cfg["rate_limit_seconds"]),
        )
    else:
        results = open_sequence(pairs, lambda url, folder: reader(url, folder, spec, headed=True, pause_seconds=pause_seconds))
    by_url = {item.url: item for item in results}
    exit_code = 0
    collected = False
    for id_2gis, (url, folder) in zip(ids, pairs, strict=True):
        row = reference.get(id_2gis)
        lines.extend(["", f"## {id_2gis}", "", f"Название в эталоне: {row['название'] if row else 'нет в эталоне'}", f"Адрес страницы: {url}", ""])
        item = by_url.get(url)
        if item is None:
            lines.extend(["Не открывалась: предыдущая страница ограничила доступ.", ""])
            exit_code = 2
            continue
        if item.error is not None:
            exc = item.error
            exit_code = 2 if isinstance(exc, (AccessRestricted, BrowserUnavailable)) else 1
            lines.extend(
                [
                    f"Доступ: {getattr(exc, 'access', None) or type(exc).__name__}",
                    f"Итог: {getattr(exc, 'outcome', None) or 'stopped'}",
                    f"Причина: {exc}",
                    f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                    "Поля компании не читались.",
                    "",
                ]
            )
            continue
        page = item.page
        assert page is not None
        collected = True
        seen = now().isoformat()
        fields, sources = extract_card(
            "2gis",
            page.document,
            url=page.url,
            seen_at=seen,
            screenshot=page.screenshot_path,
            raw_path=page.raw_path,
            code_fields=code_fields,
            model_fields=model_fields,
        )
        notes = _notes(folder)
        missing = []
        for key in field_keys("2gis"):
            if fields.get(key) is None:
                missing.append(f"{key}: {notes.get(key) or (sources.get(key) or {}).get('reason') or 'not_shown'}")
        diffs = compare_reference(fields, row) if row else []
        stored = _store(id_2gis, page, collection_key=f"e2-2gis:{id_2gis}")
        label = fields.get("category") if isinstance(fields.get("category"), str) else None
        sector, category, subcategory = classify([label] if label else [], table)
        lines.extend(
            [
                "Доступ: card_visible",
                "Итог: collected",
                f"Фактический адрес: {page.url}",
                f"Время просмотра: {seen}",
                f"Скриншот: {page.screenshot_path}",
                f"Текст страницы: {folder / 'text.txt'}",
                f"База: {stored}",
                f"Рубрика: {label or 'пусто'} ({notes.get('category') or 'not_shown'})",
                f"Сектор: {sector or 'пусто'}",
                f"Категория: {category or 'пусто'}",
                f"Подкатегория: {subcategory or 'пусто'}",
                f"Услуги: {notes.get('service_names') or 'пусто'}",
                "Пустые поля:",
            ]
        )
        lines.extend(f"- {item_line}" for item_line in missing)
        lines.append("Расхождения с эталоном 30.09:")
        if not diffs:
            lines.append("- нет")
        else:
            lines.extend(
                f"- {diff['field']}: было {diff['reference'] or 'пусто'}, стало {diff['extracted'] or 'пусто'} ({diff['kind']})"
                for diff in diffs
            )
        lines.append("")
    exported = "файл не перезаписан: карточка не собрана"
    if collected:
        exported = (exporter or _export_mailing)()
    lines.extend(["", f"Excel: {exported}", "", "Модель для категории не вызывалась. Рубрика сверена со списком config/categories.yaml."])
    _write(report_path, lines)
    print(f"Отчёт: {report_path}")
    return exit_code


def _store(id_2gis: str, page: LoadedPage, *, collection_key: str | None = None) -> str:
    try:
        from zhol.db.base import get_session_factory
        from zhol.db.models import Company

        factory = get_session_factory()
    except RuntimeError as exc:
        return str(exc)
    from sqlalchemy import select

    from zhol.agents.scout.collect import collect_company
    from zhol.browser.config import load_platforms

    class _Ready:
        def __init__(self, loaded: LoadedPage) -> None:
            self.loaded = loaded
            self.screenshot = None

        def card(self, platform: str, external_id: str, url: str) -> LoadedPage:
            if platform != "2gis":
                raise LiveCollectionDisabled(platform)
            return self.loaded

        def search(self, platform: str, id_2gis: str, url: str) -> LoadedPage:
            raise LiveCollectionDisabled(platform)

    try:
        with factory() as session, session.begin():
            company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
            if company is None:
                return "компании нет в базе, снимок не записан"
            result = collect_company(
                session,
                company=company,
                purpose="prospect",
                collection_key=collection_key or f"s1b2-2gis:{id_2gis}",
                pages=_Ready(page),  # type: ignore[arg-type]
                cfg=load_platforms(),
                platforms=("2gis",),
            )
            snapshot = result.snapshots[0]
            if result.failed:
                return f"снимок {snapshot.id} со статусом failed: {result.reason}"
            names = []
            if page.document is not None:
                raw = (page.document.notes or {}).get("service_names") or ""
                names = [item.strip() for item in raw.split(";") if item.strip()]
            from zhol.agents.scout.persist import save_verified_services

            forbidden = [company.name]
            if page.document is not None and page.document.fields.get("name"):
                forbidden.append(page.document.fields["name"])
            services = save_verified_services(session, id_2gis, names, forbidden=forbidden)
            return f"снимок {snapshot.id}, статус {snapshot.status}; {services}"
    except Exception as exc:
        return f"снимок не записан: {type(exc).__name__}"
