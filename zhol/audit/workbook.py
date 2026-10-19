"""Read a Bek audit workbook. The detailed sheet is chosen from the headers, not the tab name."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import Cell

from zhol.scoring.adapter import COLUMNS, STATUS_COLUMNS

FIRM_ID = re.compile(r"/firm/(\d+)")
ID_COLUMNS = frozenset({"id_2gis"})
SCORE_COLUMNS = frozenset({"балл_итого"})
GROUP_COLUMNS = {
    "балл_отзывы": "reviews",
    "балл_цены": "prices",
    "балл_фото": "photos",
    "балл_контакты": "contacts",
}

# English mailing headers and a few Russian extras, mapped onto the etalon column names.
ALIASES = {
    "company id": "id_2gis",
    "company name": "название",
    "branch name": "название_филиала",
    "city": "город",
    "address": "адрес_2гис",
    "phone": "контактные_телефоны",
    "whatsapp": "номер_whatsapp",
    "instagram": "аккаунт_instagram",
    "2gis url": "url_2gis",
    "2gis rating": "2гис_рейтинг",
    "2gis review count": "2гис_отзывов",
    "2gis photo count": "2гис_фото",
    "2gis description": "2гис_описание",
    "2gis opening hours": "2гис_часы",
    "google url": "url_google",
    "google rating": "google_рейтинг",
    "google review count": "google_отзывов",
    "google photo count": "google_фото",
    "google description": "google_описание",
    "google opening hours": "google_часы",
    "yandex url": "url_яндекс",
    "yandex rating": "яндекс_рейтинг",
    "yandex review count": "яндекс_отзывов",
    "yandex photo count": "яндекс_фото",
    "yandex description": "яндекс_описание",
    "yandex opening hours": "яндекс_часы",
    "overall completeness score": "балл_итого",
    "identified problems": "расхождения",
    "missing information": "не_хватает",
    "collection date": "дата_просмотра",
    "source": "источник",
    "verification status": "verification_status",
    "2гис описание": "2гис_описание",
    "2гис часы": "2гис_часы",
    "2гис рубрика": "2гис_рубрика",
    "яндекс описание": "яндекс_описание",
    "яндекс часы": "яндекс_часы",
    "яндекс рубрика": "яндекс_рубрика",
    "яндекс сайт": "яндекс_сайт",
    "google описание": "google_описание",
    "google часы": "google_часы",
    "google рубрика": "google_рубрика",
    "google фото": "google_фото",
}

NOTE_MARKERS = ("наблюд", "problem", "missing", "комментар", "comment", "note")
HEADER_SCAN_ROWS = 25
KNOWN = (
    {column for _, _, column in COLUMNS}
    | {column for column, _ in STATUS_COLUMNS.values()}
    | set(ALIASES.values())
    | set(GROUP_COLUMNS)
    | {
        "id_2gis",
        "название",
        "тип",
        "слой",
        "адрес_2гис",
        "url_2gis",
        "url_яндекс",
        "url_google",
        "дата_просмотра",
        "расхождения",
        "балл_итого",
        "номер_whatsapp",
        "аккаунт_instagram",
        "город",
    }
)


class WorkbookError(ValueError):
    pass


@dataclass
class AuditRow:
    excel_row: int
    values: dict[str, str]
    formulas: frozenset[str]
    notes: list[tuple[str, str]] = field(default_factory=list)
    hyperlinks: dict[str, str] = field(default_factory=dict)
    skip_reason: str | None = None
    skip_kind: str | None = None
    # "section" means the row followed a later header layout in the same sheet, not the first salon header.
    layout: str | None = None

    def text(self, column: str) -> str | None:
        if column in self.formulas or column not in self.values:
            return None
        return self.values[column]


@dataclass(frozen=True)
class AuditSheet:
    path: str
    sheets: tuple[str, ...]
    sheet: str
    rows: tuple[AuditRow, ...]
    guide: tuple[tuple[str, str], ...] = ()


def read_workbook(path: str | Path) -> AuditSheet:
    file = Path(path)
    if not file.is_file():
        raise WorkbookError(f"{file} does not exist")
    if file.suffix.lower() != ".xlsx":
        raise WorkbookError(f"{file.name} is not an xlsx workbook")
    workbook = load_workbook(file, data_only=False, read_only=False)
    try:
        names = tuple(workbook.sheetnames)
        if _is_original(names):
            return _read_original(workbook, file, names)
        chosen: tuple[str, int, tuple[str, ...]] | None = None
        for name in names:
            sheet = workbook[name]
            found = _header(sheet)
            if found is None:
                continue
            score, columns = found
            if chosen is None or score > chosen[1]:
                chosen = (name, score, columns)
        if chosen is None:
            raise WorkbookError(f"{file.name} has no detailed audit sheet")
        name, _score, columns = chosen
        rows = tuple(_rows(workbook[name], columns))
        if not rows:
            raise WorkbookError(f"{file.name} sheet {name!r} has no data rows")
        return AuditSheet(str(file), names, name, rows)
    finally:
        workbook.close()


def _canonical(header: str) -> str:
    text = re.sub(r"\s+", " ", header.replace("\n", " ")).strip()
    folded = text.casefold()
    if folded in ALIASES:
        return ALIASES[folded]
    return text


def _header(sheet) -> tuple[int, tuple[str, ...]] | None:
    best: tuple[int, tuple[str, ...]] | None = None
    for row in sheet.iter_rows(max_row=HEADER_SCAN_ROWS):
        labels = tuple(_canonical(str(cell.value).strip()) if cell.value is not None else "" for cell in row)
        known = {label for label in labels if label in KNOWN}
        if not known & ID_COLUMNS or len(known) < 8:
            continue
        score = len(known) + (10 if known & SCORE_COLUMNS else 0)
        if best is None or score > best[0]:
            best = (score, labels)
    return best


def _rows(sheet, columns: tuple[str, ...]) -> list[AuditRow]:
    header_at = None
    for index, row in enumerate(sheet.iter_rows(max_row=HEADER_SCAN_ROWS), start=1):
        labels = tuple(_canonical(str(cell.value).strip()) if cell.value is not None else "" for cell in row)
        if labels == columns:
            header_at = index
            break
    if header_at is None:
        return []
    found: list[AuditRow] = []
    for row in sheet.iter_rows(min_row=header_at + 1):
        if not any(cell.value is not None for cell in row):
            continue
        values: dict[str, str] = {}
        formulas: set[str] = set()
        notes: list[tuple[str, str]] = []
        for column, cell in zip(columns, row, strict=False):
            if not column:
                continue
            parsed, formula = _cell(cell)
            if formula:
                formulas.add(column)
                continue
            if column in values:
                continue
            values[column] = parsed
            if parsed and _is_note(column) and column != "расхождения":
                notes.append((column, parsed))
        if not any(values.values()) and not formulas:
            continue
        found.append(AuditRow(row[0].row, values, frozenset(formulas), notes))
    return found


def _is_note(column: str) -> bool:
    folded = column.casefold()
    return any(marker in folded for marker in NOTE_MARKERS)


def _cell(cell: Cell) -> tuple[str, bool]:
    if cell.data_type == "f":
        return "", True
    value = cell.value
    if value is None:
        return "", False
    if isinstance(value, datetime):
        return value.date().isoformat(), False
    if isinstance(value, date):
        return value.isoformat(), False
    if isinstance(value, float) and value.is_integer():
        return str(int(value)), False
    if isinstance(value, bool):
        return "1" if value else "0", False
    text = str(value).strip()
    return text, False


# The Bek workbook Timu handed over. Column titles repeat under each platform,
# so a label is read together with the merged group on the row above it.
ORIGINAL_SHEETS = ("Сводка", "Контакты для связи", "Все данные", "Как читать")
DETAIL_SHEET = "Все данные"
SUMMARY_SHEET = "Сводка"
CONTACT_SHEET = "Контакты для связи"
GUIDE_SHEET = "Как читать"
GUIDE_CHECKS = {
    "Что это": ("50", "30.09.2026"),
    "Итоговый балл": ("100", "25"),
    "Вкладка цен": ("Прайс компании", "Фото прайса", "Пусто"),
    "Карточка: не найдена": ("названием и адресом",),
    "Карточка: найдена, адрес другой": ("другому адресу",),
    "Права заявлены (Google)": ("нет",),
    "Пустая ячейка": ("Данных нет",),
}
DETAIL_FIELDS = (
    (("Салон", "Название"), "название"),
    (("Салон", "Тип"), "тип"),
    (("Салон", "Слой выборки"), "слой"),
    (("Салон", "Адрес в 2ГИС"), "адрес_2гис"),
    (("Салон", "Дата просмотра"), "дата_просмотра"),
    (("2ГИС", "Рейтинг"), "2гис_рейтинг"),
    (("2ГИС", "Оценок"), "2гис_оценок"),
    (("2ГИС", "Отзывов"), "2гис_отзывов"),
    (("2ГИС", "Показано отзывов"), "2гис_показано_отзывов"),
    (("2ГИС", "Из них с ответом"), "2гис_ответов"),
    (("2ГИС", "Доля ответов, %"), "2гис_доля_ответов"),
    (("2ГИС", "Последний отзыв"), "2гис_последний_отзыв"),
    (("2ГИС", "Фото"), "2гис_фото"),
    (("2ГИС", "Вкладка цен"), "2гис_цены"),
    (("2ГИС", "Онлайн-запись"), "2гис_онлайн_запись"),
    (("2ГИС", "Реклама"), "2гис_реклама"),
    (("2ГИС", "Филиалов"), "2гис_филиалов"),
    (("2ГИС", "Телефон"), "контактные_телефоны"),
    (("2ГИС", "WhatsApp"), "номер_whatsapp"),
    (("2ГИС", "Instagram"), "аккаунт_instagram"),
    (("2ГИС", "Сайт или запись"), "2гис_ссылка"),
    (("2ГИС", "Ссылка 2ГИС"), "url_2gis"),
    (("Яндекс Карты", "Карточка"), "яндекс_статус"),
    (("Яндекс Карты", "Рейтинг"), "яндекс_рейтинг"),
    (("Яндекс Карты", "Оценок"), "яндекс_оценок"),
    (("Яндекс Карты", "Отзывов"), "яндекс_отзывов"),
    (("Яндекс Карты", "Фото"), "яндекс_фото"),
    (("Яндекс Карты", "Цены"), "яндекс_цены"),
    (("Яндекс Карты", "Новостей"), "яндекс_новости"),
    (("Яндекс Карты", "Владелец следит"), "яндекс_владелец_следит"),
    (("Яндекс Карты", "Ссылка Яндекс"), "url_яндекс"),
    (("Google Карты", "Карточка"), "google_статус"),
    (("Google Карты", "Рейтинг"), "google_рейтинг"),
    (("Google Карты", "Отзывов"), "google_отзывов"),
    (("Google Карты", "Сайт"), "google_сайт"),
    (("Google Карты", "Права заявлены"), "google_права_заявлены"),
    (("Сверка", "Расхождения между сервисами"), "расхождения"),
    (("Баллы", "Отзывы"), "балл_отзывы"),
    (("Баллы", "Цены"), "балл_цены"),
    (("Баллы", "Фото"), "балл_фото"),
    (("Баллы", "Контакты"), "балл_контакты"),
    (("Баллы", "Итого"), "балл_итого"),
)
BOOL_FIELDS = {
    "2гис_онлайн_запись",
    "2гис_реклама",
    "яндекс_владелец_следит",
    "google_права_заявлены",
}
URL_FIELDS = {"url_2gis", "url_яндекс"}
SCORE_FIELDS = ("балл_отзывы", "балл_цены", "балл_фото", "балл_контакты", "балл_итого")
PRICE_WORDS = frozenset({"прайс компании", "фото прайса", "пусто"})
YANDEX_PRICE_WORDS = frozenset({"есть", "есть, давно не обновлялись", "нет"})
STATUS_WORDS = {
    "яндекс_статус": frozenset({"найдена", "найдена, адрес другой", "не найдена"}),
    "google_статус": frozenset({"найдена", "найдена, адрес или филиал другой", "не найдена"}),
}
SUMMARY_SCORE_HEADERS = {
    "Отзывы из 25": "балл_отзывы",
    "Цены из 25": "балл_цены",
    "Фото из 25": "балл_фото",
    "Контакты из 25": "балл_контакты",
    "Итого из 100": "балл_итого",
}


def _is_original(names: tuple[str, ...]) -> bool:
    return set(ORIGINAL_SHEETS).issubset(names)


def _read_original(workbook, file: Path, names: tuple[str, ...]) -> AuditSheet:
    missing = [name for name in ORIGINAL_SHEETS if name not in names]
    if missing:
        raise WorkbookError(f"{file.name} is missing {missing}")
    guide = _guide(workbook[GUIDE_SHEET])
    detail = workbook[DETAIL_SHEET]
    columns = _grouped_columns(detail)
    absent = [pair for pair, _canonical in DETAIL_FIELDS if pair not in columns]
    if absent:
        raise WorkbookError(f"{DETAIL_SHEET} is missing columns {absent}")
    summary = _sheet_columns(workbook[SUMMARY_SHEET], {**SUMMARY_SCORE_HEADERS, "Название": "название", "Главные пробелы": "главные_пробелы"})
    contacts = _sheet_columns(
        workbook[CONTACT_SHEET],
        {
            "Название": "название",
            "WhatsApp (номер)": "номер_whatsapp",
            "Телефон": "контактные_телефоны",
            "Ссылка 2ГИС": "url_2gis",
            "Ссылка Яндекс": "url_яндекс",
            "Главные пробелы для аудита": "главные_пробелы",
        },
    )
    detail_rows = _named_rows(detail, columns[(("Салон", "Название"))])
    summary_rows = _named_rows(workbook[SUMMARY_SHEET], summary["название"])
    contact_rows = _named_rows(workbook[CONTACT_SHEET], contacts["название"])
    if not (len(detail_rows) == len(summary_rows) == len(contact_rows)):
        raise WorkbookError(
            f"sheet row counts differ: {DETAIL_SHEET} {len(detail_rows)}, "
            f"{SUMMARY_SHEET} {len(summary_rows)}, {CONTACT_SHEET} {len(contact_rows)}"
        )
    summary_sheet = workbook[SUMMARY_SHEET]
    contact_sheet = workbook[CONTACT_SHEET]
    rows = tuple(
        _original_row(detail, summary_sheet, contact_sheet, columns, summary, contacts, left, middle, right)
        for left, middle, right in zip(detail_rows, summary_rows, contact_rows, strict=True)
    )
    if not any(row.skip_reason is None for row in rows):
        raise WorkbookError(f"{file.name} has no rows that follow the {DETAIL_SHEET} header")
    return AuditSheet(str(file), names, DETAIL_SHEET, rows, guide)


def _guide(sheet) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for row in sheet.iter_rows(max_col=2):
        label = _label(row[0].value)
        body = _label(row[1].value)
        if label and body:
            found.append((label, body))
    text = dict(found)
    missing = [label for label in GUIDE_CHECKS if label not in text]
    if missing:
        raise WorkbookError(f"{GUIDE_SHEET} is missing {missing}")
    for label, parts in GUIDE_CHECKS.items():
        if any(part not in text[label] for part in parts):
            raise WorkbookError(f"{GUIDE_SHEET} entry {label!r} does not match the expected definition")
    return tuple(found)


def _label(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def _grouped_columns(sheet) -> dict[tuple[str, str], int]:
    groups: dict[int, str] = {}
    for merged in sheet.merged_cells.ranges:
        if merged.min_row != 1:
            continue
        title = _label(sheet.cell(1, merged.min_col).value)
        for column in range(merged.min_col, merged.max_col + 1):
            groups[column] = title
    found: dict[tuple[str, str], int] = {}
    for column in range(1, sheet.max_column + 1):
        label = _label(sheet.cell(2, column).value)
        if not label:
            continue
        found[(groups.get(column, ""), label)] = column
    return found


def _sheet_columns(sheet, wanted: dict[str, str]) -> dict[str, int]:
    header = None
    for index, row in enumerate(sheet.iter_rows(max_row=HEADER_SCAN_ROWS), start=1):
        labels = {_label(cell.value) for cell in row}
        if set(wanted).issubset(labels):
            header = index
            break
    if header is None:
        raise WorkbookError(f"{sheet.title} is missing columns {sorted(set(wanted) - set())}")
    found: dict[str, int] = {}
    for column in range(1, sheet.max_column + 1):
        label = _label(sheet.cell(header, column).value)
        if label in wanted and wanted[label] not in found:
            found[wanted[label]] = column
    missing = [name for name in wanted.values() if name not in found]
    if missing:
        raise WorkbookError(f"{sheet.title} is missing columns {missing}")
    return found


def _named_rows(sheet, name_column: int) -> list[int]:
    return [row for row in range(1, sheet.max_row + 1) if _label(sheet.cell(row, name_column).value) and row > 1 and _label(sheet.cell(row, name_column).value) != "Название"]


def _original_row(detail, summary, contacts, columns, summary_columns, contact_columns, detail_row: int, summary_row: int, contact_row: int) -> AuditRow:
    name = _label(detail.cell(detail_row, columns[(("Салон", "Название"))]).value)
    summary_name = _label(summary.cell(summary_row, summary_columns["название"]).value)
    contact_name = _label(contacts.cell(contact_row, contact_columns["название"]).value)
    if name != summary_name or name != contact_name:
        return AuditRow(
            detail_row,
            {"название": name},
            frozenset(),
            skip_reason="Сводка, Контакты для связи and Все данные do not name the same row",
            skip_kind="ambiguous",
        )
    total_cell = detail.cell(detail_row, columns[(("Баллы", "Итого"))])
    link_cell = detail.cell(detail_row, columns[(("2ГИС", "Ссылка 2ГИС"))])
    total_text, total_formula = _cell(total_cell)
    firm = _http(link_cell)
    firm_ok = firm is not None and FIRM_ID.search(firm) is not None
    if total_formula and firm_ok:
        return _ambiguous(detail_row, name, "балл_итого is a formula, not a value")
    if not total_text.isdigit() or not firm_ok:
        shifted = _section_row(detail, summary, contacts, columns, detail_row, summary_row, contact_row, name)
        if shifted is not None:
            return shifted
        return AuditRow(
            detail_row,
            {"название": name, "слой": _label(detail.cell(detail_row, columns[(("Салон", "Слой выборки"))]).value)},
            frozenset(),
            skip_reason="detail row does not match the Все данные header",
            skip_kind="unmatched",
        )
    values: dict[str, str] = {}
    formulas: set[str] = set()
    links: dict[str, str] = {}
    for pair, canonical in DETAIL_FIELDS:
        cell = detail.cell(detail_row, columns[pair])
        parsed, formula = _cell(cell)
        if formula:
            formulas.add(canonical)
            continue
        target = _http(cell)
        if canonical in URL_FIELDS:
            if target:
                values[canonical] = target
                links[canonical] = target
            continue
        if target and canonical in {"номер_whatsapp", "аккаунт_instagram"}:
            links[canonical] = target
        if not parsed:
            # «Как читать»: an empty cell was not determined. It is not a confirmed gap and not zero.
            continue
        if canonical == "google_права_заявлены" and parsed.casefold() == "нет данных":
            continue
        if canonical in BOOL_FIELDS:
            mapped = _yes_no(parsed)
            if mapped is None:
                return _ambiguous(detail_row, name, f"{canonical} value {parsed!r} is not да or нет")
            values[canonical] = mapped
            continue
        values[canonical] = parsed
    if not _flags(values, links):
        return _ambiguous(detail_row, name, "a contact cell is neither a phone, an instagram account, nor нет")
    firm_id = FIRM_ID.search(values.get("url_2gis", ""))
    if firm_id is None:
        return _ambiguous(detail_row, name, "2GIS link has no firm id")
    values["id_2gis"] = firm_id.group(1)
    problem = _check_original(values, formulas, summary, contacts, summary_columns, contact_columns, summary_row, contact_row, links)
    if problem:
        return _ambiguous(detail_row, name, problem)
    gap = _gap_text(summary, contacts, summary_columns, contact_columns, summary_row, contact_row)
    if gap[0] is None and gap[1]:
        return _ambiguous(detail_row, name, gap[1])
    if gap[0]:
        values["главные_пробелы"] = gap[0]
    _prefer_contact_links(contacts, contact_columns, contact_row, values, links)
    link_problem = _same_identity(values, links)
    if link_problem:
        return _ambiguous(detail_row, name, link_problem)
    return AuditRow(detail_row, values, frozenset(formulas), [], links)


def _section_row(detail, summary, contacts, columns, detail_row: int, summary_row: int, contact_row: int, name: str) -> AuditRow | None:
    """Read a row whose cells no longer sit under the first salon header.

    Columns are taken from the summary group that still accepts the value, and only when
    «Все данные» has the same value. A copied Yandex rating and a firm id reused as a
    Yandex organization id are not stored.
    """
    groups = _column_groups(summary)
    score_keys = (
        ("балл_отзывы", "Балл", "Отзывы из 25"),
        ("балл_цены", "Балл", "Цены из 25"),
        ("балл_фото", "Балл", "Фото из 25"),
        ("балл_контакты", "Балл", "Контакты из 25"),
        ("балл_итого", "Балл", "Итого из 100"),
    )
    scores: list[str] = []
    for _key, group, label in score_keys:
        column = _group_column(summary, groups, group, label)
        if column is None:
            return None
        parsed, formula = _cell(summary.cell(summary_row, column))
        if formula or not parsed.isdigit():
            return None
        scores.append(parsed)
    firm_column = _header_column(contacts, "Ссылка 2ГИС")
    phone_column = _header_column(contacts, "Телефон")
    whatsapp_column = _header_column(contacts, "WhatsApp (номер)")
    instagram_column = _header_column(contacts, "Instagram")
    address_column = _header_column(contacts, "Адрес в 2ГИС")
    type_column = _header_column(contacts, "Тип")
    layer_column = _header_column(contacts, "Слой")
    score_column = _header_column(contacts, "Балл")
    gap_column = _header_column(contacts, "Главные пробелы для аудита")
    yandex_column = _header_column(contacts, "Ссылка Яндекс")
    needed = (
        firm_column,
        phone_column,
        whatsapp_column,
        instagram_column,
        address_column,
        type_column,
        layer_column,
        score_column,
        gap_column,
    )
    if any(column is None for column in needed):
        return None
    firm_url = _http(contacts.cell(contact_row, firm_column))
    firm_match = FIRM_ID.search(firm_url or "")
    if firm_match is None:
        return None
    firm_id = firm_match.group(1)
    contact_score, score_formula = _cell(contacts.cell(contact_row, score_column))
    if score_formula or contact_score != scores[-1]:
        return _ambiguous(detail_row, name, "Сводка балл_итого does not match Контакты для связи")
    detail_texts = [_detail_text(detail, detail_row, column) for column in range(1, detail.max_column + 1)]
    found_at = [index for index in range(len(detail_texts) - len(scores) + 1) if detail_texts[index : index + len(scores)] == scores]
    if not found_at:
        return None
    if len(found_at) > 1:
        return _ambiguous(detail_row, name, "scores in the shifted section do not match Сводка")
    for column in range(1, detail.max_column + 1):
        other = _firm(detail.cell(detail_row, column))
        if other and other != firm_id:
            return _ambiguous(detail_row, name, "2GIS link on Контакты для связи does not match Все данные")
    values: dict[str, str] = {"название": name}
    links: dict[str, str] = {}
    problem = _same_text(detail, contacts, columns, detail_row, contact_row, ("Салон", "Тип"), type_column, "тип")
    if problem:
        return _ambiguous(detail_row, name, problem)
    values["тип"] = _detail_text(detail, detail_row, columns[(("Салон", "Тип"))]) or ""
    problem = _same_text(detail, contacts, columns, detail_row, contact_row, ("Салон", "Слой выборки"), layer_column, "слой")
    if problem:
        return _ambiguous(detail_row, name, problem)
    values["слой"] = _detail_text(detail, detail_row, columns[(("Салон", "Слой выборки"))]) or ""
    summary_type = _group_text(summary, groups, summary_row, "Салон и контакты", "Тип")
    summary_layer = _group_text(summary, groups, summary_row, "Салон и контакты", "Слой выборки")
    if summary_type != values["тип"] or summary_layer != values["слой"]:
        return _ambiguous(detail_row, name, "Сводка category does not match Все данные")
    problem = _same_text(detail, contacts, columns, detail_row, contact_row, ("Салон", "Адрес в 2ГИС"), address_column, "адрес_2гис")
    if problem:
        return _ambiguous(detail_row, name, problem)
    values["адрес_2гис"] = _detail_text(detail, detail_row, columns[(("Салон", "Адрес в 2ГИС"))]) or ""
    seen, seen_formula = _cell(detail.cell(detail_row, columns[(("Салон", "Дата просмотра"))]))
    if seen_formula or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", seen):
        return AuditRow(detail_row, {"название": name}, frozenset(), skip_reason="missing_source_date", skip_kind="unmatched")
    values["дата_просмотра"] = seen
    copied = _copied_platform(summary, groups, summary_row)
    rating = _agreed_metric(detail, summary, groups, columns, detail_row, summary_row, "2ГИС", "Рейтинг", ("2ГИС", "Рейтинг"), _is_rating)
    if rating is None:
        return _ambiguous(detail_row, name, "2GIS rating on Сводка does not match Все данные")
    if rating:
        values["2гис_рейтинг"] = rating
    counted = _agreed_metric(detail, summary, groups, columns, detail_row, summary_row, "2ГИС", "Оценок", ("2ГИС", "Оценок"), str.isdigit)
    if counted is None:
        return _ambiguous(detail_row, name, "2GIS rating count on Сводка does not match Все данные")
    if counted:
        values["2гис_оценок"] = counted
    photos = _summary_value(summary, groups, summary_row, "2ГИС", "Фото")
    if photos and photos.isdigit():
        if not _value_present(detail, detail_row, photos):
            return _ambiguous(detail_row, name, "2GIS photo count on Сводка is not in Все данные")
        values["2гис_фото"] = photos
    price = _summary_value(summary, groups, summary_row, "2ГИС", "Вкладка цен")
    if price in PRICE_WORDS:
        if not _value_present(detail, detail_row, price):
            return _ambiguous(detail_row, name, "2GIS price tab on Сводка is not in Все данные")
        values["2гис_цены"] = price
    for key, score in zip((item[0] for item in score_keys), scores, strict=True):
        values[key] = score
    phone, phone_formula = _cell(contacts.cell(contact_row, phone_column))
    if phone_formula or not phone or not _value_present(detail, detail_row, phone):
        return _ambiguous(detail_row, name, "phone on Контакты для связи does not match Все данные")
    values["контактные_телефоны"] = phone
    whatsapp, whatsapp_formula = _cell(contacts.cell(contact_row, whatsapp_column))
    if whatsapp_formula or not whatsapp or not _value_present(detail, detail_row, whatsapp):
        return _ambiguous(detail_row, name, "WhatsApp on Контакты для связи does not match Все данные")
    values["номер_whatsapp"] = whatsapp
    whatsapp_link = _http(contacts.cell(contact_row, whatsapp_column))
    if whatsapp_link:
        if _conflicting_link(detail, detail_row, whatsapp, whatsapp_link):
            return _ambiguous(detail_row, name, "WhatsApp link on Контакты для связи does not match Все данные")
        links["номер_whatsapp"] = whatsapp_link
    instagram, instagram_formula = _cell(contacts.cell(contact_row, instagram_column))
    if instagram_formula:
        return _ambiguous(detail_row, name, "instagram on Контакты для связи is a formula, not a value")
    if instagram:
        if not _value_present(detail, detail_row, instagram):
            return _ambiguous(detail_row, name, "instagram on Контакты для связи does not match Все данные")
        values["аккаунт_instagram"] = instagram
        instagram_link = _http(contacts.cell(contact_row, instagram_column))
        if instagram_link:
            if _conflicting_link(detail, detail_row, instagram, instagram_link):
                return _ambiguous(detail_row, name, "instagram link on Контакты для связи does not match Все данные")
            links["аккаунт_instagram"] = instagram_link
    elif _handle_present(detail, detail_row):
        return _ambiguous(detail_row, name, "instagram on Контакты для связи does not match Все данные")
    gap, gap_formula = _cell(contacts.cell(contact_row, gap_column))
    if gap_formula:
        return _ambiguous(detail_row, name, "главные пробелы is a formula, not a value")
    if gap:
        values["главные_пробелы"] = gap
    if not copied:
        _keep_yandex(contacts, summary, groups, contact_row, summary_row, yandex_column, firm_id, values, links)
    if _google_missing(summary, groups, summary_row):
        values["google_проверка"] = "не найдена"
    if not _flags(values, links):
        return _ambiguous(detail_row, name, "a contact cell is neither a phone, an instagram account, nor нет")
    values["url_2gis"] = firm_url
    links["url_2gis"] = firm_url
    values["id_2gis"] = firm_id
    identity = _same_identity(values, links)
    if identity:
        return _ambiguous(detail_row, name, identity)
    return AuditRow(detail_row, values, frozenset(), [], links, layout="section")


def _column_groups(sheet) -> dict[int, str]:
    groups: dict[int, str] = {}
    for merged in sheet.merged_cells.ranges:
        if merged.min_row != 1:
            continue
        title = _label(sheet.cell(1, merged.min_col).value)
        for column in range(merged.min_col, merged.max_col + 1):
            groups[column] = title
    for column in range(1, sheet.max_column + 1):
        title = _label(sheet.cell(1, column).value)
        if title:
            groups[column] = title
    return groups


def _group_column(sheet, groups: dict[int, str], group: str, label: str) -> int | None:
    for column, title in groups.items():
        if title == group and _label(sheet.cell(2, column).value) == label:
            return column
    return None


def _header_column(sheet, label: str, header_row: int = 1) -> int | None:
    for column in range(1, sheet.max_column + 1):
        if _label(sheet.cell(header_row, column).value) == label:
            return column
    return None


def _detail_text(sheet, row: int, column: int) -> str | None:
    parsed, formula = _cell(sheet.cell(row, column))
    if formula:
        return None
    return parsed


def _summary_value(summary, groups: dict[int, str], row: int, group: str, label: str) -> str:
    column = _group_column(summary, groups, group, label)
    if column is None:
        return ""
    parsed, formula = _cell(summary.cell(row, column))
    if formula:
        return ""
    return parsed


def _group_text(summary, groups: dict[int, str], row: int, group: str, label: str) -> str:
    return _summary_value(summary, groups, row, group, label)


def _same_text(detail, contacts, columns, detail_row: int, contact_row: int, pair: tuple[str, str], contact_column: int, canonical: str) -> str | None:
    left = _detail_text(detail, detail_row, columns[pair])
    right, formula = _cell(contacts.cell(contact_row, contact_column))
    if formula or not left or left != right:
        return f"{canonical} on Контакты для связи does not match Все данные"
    return None


def _value_present(sheet, row: int, text: str) -> bool:
    for column in range(1, sheet.max_column + 1):
        parsed, formula = _cell(sheet.cell(row, column))
        if not formula and parsed == text:
            return True
    return False


def _conflicting_link(sheet, row: int, text: str, link: str) -> bool:
    for column in range(1, sheet.max_column + 1):
        cell = sheet.cell(row, column)
        parsed, formula = _cell(cell)
        if formula or parsed != text:
            continue
        other = _http(cell)
        if other and _norm_url(other) != _norm_url(link):
            return True
    return False


def _handle_present(sheet, row: int) -> bool:
    for column in range(1, sheet.max_column + 1):
        parsed, formula = _cell(sheet.cell(row, column))
        if not formula and parsed.startswith("@"):
            return True
    return False


def _is_rating(text: str) -> bool:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return False
    return 0 <= value <= 5


def _agreed_metric(detail, summary, groups, columns, detail_row: int, summary_row: int, group: str, label: str, pair: tuple[str, str], accept) -> str | None:
    summary_text = _summary_value(summary, groups, summary_row, group, label)
    detail_text = _detail_text(detail, detail_row, columns[pair])
    if summary_text == "" and not detail_text:
        return ""
    if summary_text and detail_text == summary_text and accept(summary_text):
        return summary_text
    return None


def _copied_platform(summary, groups: dict[int, str], row: int) -> bool:
    twogis = _summary_value(summary, groups, row, "2ГИС", "Рейтинг")
    yandex = _summary_value(summary, groups, row, "Яндекс Карты", "Рейтинг")
    if not _is_rating(twogis) or not _is_rating(yandex):
        return False
    # The same offset as the formula rows that must not be audited: Yandex is 2GIS minus 0.1.
    return abs((float(twogis) - 0.1) - float(yandex)) < 1e-9


def _yandex_reliable(url: str | None, firm_id: str) -> bool:
    if not url:
        return False
    match = re.search(r"/org/([^/]*)/(\d+)/?", url)
    if match is None:
        return False
    slug, org_id = match.group(1), match.group(2)
    if not slug or firm_id.startswith(org_id):
        return False
    return True


def _keep_yandex(contacts, summary, groups, contact_row: int, summary_row: int, yandex_column: int | None, firm_id: str, values: dict[str, str], links: dict[str, str]) -> None:
    if yandex_column is None:
        return
    target = _http(contacts.cell(contact_row, yandex_column))
    if not _yandex_reliable(target, firm_id):
        return
    status = _summary_value(summary, groups, summary_row, "Яндекс Карты", "Карточка")
    if status not in STATUS_WORDS["яндекс_статус"]:
        return
    values["url_яндекс"] = target
    links["url_яндекс"] = target
    values["яндекс_статус"] = status


def _google_missing(summary, groups: dict[int, str], row: int) -> bool:
    for column, group in groups.items():
        if "Google" not in group:
            continue
        parsed, formula = _cell(summary.cell(row, column))
        if not formula and parsed == "не найдена":
            return True
    return False


def _ambiguous(excel_row: int, name: str, reason: str) -> AuditRow:
    return AuditRow(excel_row, {"название": name}, frozenset(), skip_reason=reason, skip_kind="ambiguous")


def _yes_no(text: str) -> str | None:
    folded = text.casefold()
    if folded == "да":
        return "1"
    if folded == "нет":
        return "0"
    return None


def _flags(values: dict[str, str], links: dict[str, str]) -> bool:
    if not _presence(values, links, "контактные_телефоны", "2гис_телефон", phone=True):
        return False
    if not _presence(values, links, "номер_whatsapp", "2гис_whatsapp", phone=True):
        return False
    return _presence(values, links, "аккаунт_instagram", "2гис_instagram", phone=False)


def _presence(values: dict[str, str], links: dict[str, str], column: str, flag: str, *, phone: bool) -> bool:
    if column not in values:
        return True
    text = values[column]
    if text.casefold() == "нет":
        values.pop(column)
        links.pop(column, None)
        values[flag] = "0"
        return True
    if phone and text.startswith("+"):
        values[flag] = "1"
        return True
    if not phone and text.startswith("@"):
        values[flag] = "1"
        return True
    return False


def _check_original(values, formulas, summary, contacts, summary_columns, contact_columns, summary_row: int, contact_row: int, links: dict[str, str]) -> str | None:
    for column in (*SCORE_FIELDS, "2гис_цены", "яндекс_статус", "google_статус", "яндекс_цены"):
        if column in formulas:
            return f"{column} is a formula, not a value"
    for column in SCORE_FIELDS:
        raw = values.get(column, "")
        if not raw.isdigit():
            return f"{column} is missing"
    for column, words in STATUS_WORDS.items():
        raw = values.get(column, "")
        if raw and raw not in words:
            return f"{column} value {raw!r} is not a known card status"
    price = values.get("2гис_цены", "")
    if price and price not in PRICE_WORDS:
        return f"2гис_цены value {price!r} is not a known price tab"
    yandex_price = values.get("яндекс_цены", "")
    if yandex_price and yandex_price not in YANDEX_PRICE_WORDS:
        return f"яндекс_цены value {yandex_price!r} is not a known price value"
    for canonical in SCORE_FIELDS:
        cell = summary.cell(summary_row, summary_columns[canonical])
        parsed, formula = _cell(cell)
        if formula or parsed != values.get(canonical, ""):
            return f"Сводка {canonical} does not match Все данные"
    contact_firm = _firm(contacts.cell(contact_row, contact_columns["url_2gis"]))
    if contact_firm and contact_firm != values["id_2gis"]:
        return "2GIS link on Контакты для связи does not match Все данные"
    contact_yandex = _http(contacts.cell(contact_row, contact_columns["url_яндекс"]))
    detail_yandex = links.get("url_яндекс") or values.get("url_яндекс")
    if contact_yandex and detail_yandex and _org_text(contact_yandex) != _org_text(detail_yandex):
        return "Yandex link on Контакты для связи does not match Все данные"
    contact_phone, _formula = _cell(contacts.cell(contact_row, contact_columns["контактные_телефоны"]))
    if contact_phone and contact_phone != values.get("контактные_телефоны", ""):
        return "phone on Контакты для связи does not match Все данные"
    contact_wa_cell = contacts.cell(contact_row, contact_columns["номер_whatsapp"])
    contact_wa, _formula = _cell(contact_wa_cell)
    if contact_wa and contact_wa != values.get("номер_whatsapp", ""):
        return "WhatsApp on Контакты для связи does not match Все данные"
    contact_wa_link = _http(contact_wa_cell)
    detail_wa_link = links.get("номер_whatsapp")
    if contact_wa_link and detail_wa_link and _norm_url(contact_wa_link) != _norm_url(detail_wa_link):
        return "WhatsApp link on Контакты для связи does not match Все данные"
    return None


def _gap_text(summary, contacts, summary_columns, contact_columns, summary_row: int, contact_row: int) -> tuple[str | None, str | None]:
    left, left_formula = _cell(summary.cell(summary_row, summary_columns["главные_пробелы"]))
    right, right_formula = _cell(contacts.cell(contact_row, contact_columns["главные_пробелы"]))
    if left_formula or right_formula:
        return None, "главные пробелы is a formula, not a value"
    if left and right and left != right:
        return None, "главные пробелы on Сводка does not match Контакты для связи"
    return (left or right or None), None


def _prefer_contact_links(contacts, contact_columns, contact_row: int, values: dict[str, str], links: dict[str, str]) -> None:
    for canonical in ("url_2gis", "url_яндекс", "номер_whatsapp"):
        target = _http(contacts.cell(contact_row, contact_columns[canonical]))
        if not target:
            continue
        current = links.get(canonical) or values.get(canonical)
        if current and _norm_url(current) != _norm_url(target) and canonical != "номер_whatsapp":
            continue
        if canonical in URL_FIELDS and canonical not in values:
            values[canonical] = target
        links.setdefault(canonical, target)


def _same_identity(values: dict[str, str], links: dict[str, str]) -> str | None:
    firm = _firm_text(values.get("url_2gis"))
    linked = _firm_text(links.get("url_2gis"))
    if firm and linked and firm != linked:
        return "2GIS hyperlink does not match the firm id"
    yandex = _org_text(values.get("url_яндекс"))
    linked_yandex = _org_text(links.get("url_яндекс"))
    if yandex and linked_yandex and yandex != linked_yandex:
        return "Yandex hyperlink does not match the organization id"
    return None


def _firm(cell) -> str | None:
    target = _http(cell)
    if target is None:
        return None
    match = FIRM_ID.search(target)
    return match.group(1) if match else None


def _firm_text(value: str | None) -> str | None:
    if not value:
        return None
    match = FIRM_ID.search(value)
    return match.group(1) if match else None


def _org_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"/org/[^/]+/(\d+)", value)
    return match.group(1) if match else None


def _http(cell) -> str | None:
    link = cell.hyperlink
    target = None if link is None else link.target
    if not target:
        return None
    text = str(target).strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return None


def _norm_url(value: str) -> str:
    return value.strip().rstrip("/")
