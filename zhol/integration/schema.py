"""Shared job contract for the live API and the local demo.

This module stays on the standard library so the demo can import it without
PostgreSQL, Selenium, or a map site.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Iterable, Iterator
from xml.sax.saxutils import escape

API_VERSION = "v1"
PLATFORMS = ("2gis", "google", "yandex")
PLATFORM_LABEL = {"2gis": "2ГИС", "google": "Google Maps", "yandex": "Яндекс Карты"}
EXPORT_FORMATS = ("xlsx", "csv", "json")
COLLECTION_LIMIT_MIN = 1
COLLECTION_LIMIT_MAX = 500
COLLECTION_LIMIT_DEFAULT = 20
PAGE_LIMIT_MAX = 100
PAGE_OFFSET_MAX = 1_000_000
EXPORT_ROW_CAP = 5000
RESULT_COLUMNS = (
    ("name", "Название"),
    ("phone", "Телефон"),
    ("address", "Адрес"),
    ("category", "Категория"),
    ("city", "Город"),
    ("platform_label", "Площадка"),
    ("source_label", "Источник"),
)

LIVE_EXPORT_NOTES = (
    "Файл содержит только компании выбранного задания или текущего фильтра.",
    "Пустая ячейка значит, что поле не было видно. Ноль туда не подставляется.",
    "Яндекс Карты в этой выгрузке есть, если они были собраны. Старая книга рассылки по-прежнему без листа Яндекса.",
    "Площадка может быть закрыта разрешением или правилами сервиса. Тогда задание не создаётся и внешние сайты не открываются.",
)
DEMO_EXPORT_NOTES = (
    "Это демонстрационные компании. Они не записаны в рабочую базу и не получены с карт.",
    "Пустая ячейка значит, что в примере этого поля нет.",
    "Живой сбор Яндекс Карт и Google Maps в рабочем режиме может быть закрыт. В демонстрации площадки только имитируются.",
)


class IntegrationError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def parse_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    page_limit = 50 if limit is None else limit
    page_offset = 0 if offset is None else offset
    if isinstance(page_limit, bool) or not isinstance(page_limit, int) or page_limit < 1 or page_limit > PAGE_LIMIT_MAX:
        raise IntegrationError(422, "validation_error", "Размер страницы должен быть от 1 до 100.")
    if isinstance(page_offset, bool) or not isinstance(page_offset, int) or page_offset < 0 or page_offset > PAGE_OFFSET_MAX:
        raise IntegrationError(422, "validation_error", "Смещение страницы вне допустимого диапазона.")
    return page_limit, page_offset


def page_body(items: list[dict], total: int, limit: int, offset: int) -> dict:
    next_offset = offset + limit if offset + limit < total else None
    return {"items": items, "total": total, "limit": limit, "offset": offset, "next_offset": next_offset}


def collection_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < COLLECTION_LIMIT_MIN or value > COLLECTION_LIMIT_MAX:
        raise IntegrationError(
            422,
            "validation_error",
            f"Число компаний должно быть от {COLLECTION_LIMIT_MIN} до {COLLECTION_LIMIT_MAX}.",
        )
    return value


def normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > 80 or any(not (char.isalnum() or char in "-_.") for char in text):
        raise IntegrationError(
            422,
            "validation_error",
            "Ключ идемпотентности: до 80 символов, латиница, цифры, точка, дефис или подчёркивание.",
        )
    return text


def blank(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or text == "Нет данных":
        return None
    return text


def company_record(
    *,
    company_id: int,
    name: str,
    phone: str | None,
    address: str | None,
    category: str | None,
    city: str | None,
    platform: str,
    source: str | None,
    source_label: str | None = None,
    review_needed: bool = False,
    review_note: str | None = None,
) -> dict:
    label = PLATFORM_LABEL.get(platform, "Не указана")
    return {
        "id": company_id,
        "name": name,
        "phone": blank(phone),
        "address": blank(address),
        "category": blank(category),
        "city": blank(city),
        "platform": platform,
        "platform_label": label,
        "source": blank(source),
        "source_label": source_label or label,
        "review_needed": review_needed,
        "review_note": blank(review_note),
    }


def controls_for(status: str) -> dict[str, bool]:
    return {
        "pause": status in {"queued", "running"},
        "resume": status == "paused",
        "stop": status in {"queued", "running", "paused", "awaiting_manual_verification"},
    }


def job_document(body: dict) -> dict:
    job_id = int(body["id"])
    return {
        "id": job_id,
        "status": body["status"],
        "platform": body.get("platform"),
        "platform_label": PLATFORM_LABEL.get(str(body.get("platform") or ""), None),
        "city": body.get("city"),
        "query": body.get("query"),
        "limit": body.get("limit"),
        "collected": int(body.get("collected") or 0),
        "progress": body.get("progress"),
        "message": body.get("message"),
        "error": body.get("error"),
        "duplicate": bool(body.get("duplicate")),
        "batch_id": body.get("batch_id"),
        "controls": controls_for(str(body["status"])),
        "results_url": f"/api/v1/jobs/{job_id}/results",
        "export_url": f"/api/v1/jobs/{job_id}/export",
    }


def export_bytes(fmt: str, rows: Iterable[dict], *, notes: tuple[str, ...] | list[str]) -> tuple[bytes, str, str]:
    if fmt not in EXPORT_FORMATS:
        raise IntegrationError(400, "rejected", "Формат выгрузки: xlsx, csv или json.")
    materialized = list(rows)
    if len(materialized) > EXPORT_ROW_CAP:
        raise IntegrationError(
            400,
            "rejected",
            f"В выгрузке больше {EXPORT_ROW_CAP} строк. Сузьте фильтр или выберите одно задание.",
        )
    if fmt == "csv":
        return _csv(materialized), "text/csv; charset=utf-8", "zhol-companies.csv"
    if fmt == "json":
        import json

        payload = {"items": [_export_item(row) for row in materialized], "total": len(materialized), "notes": list(notes)}
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return raw, "application/json; charset=utf-8", "zhol-companies.json"
    return _xlsx(materialized, notes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "zhol-companies.xlsx"


def iter_capped(rows: Iterator[dict]) -> Iterator[dict]:
    count = 0
    for row in rows:
        count += 1
        if count > EXPORT_ROW_CAP:
            raise IntegrationError(
                400,
                "rejected",
                f"В выгрузке больше {EXPORT_ROW_CAP} строк. Сузьте фильтр или выберите одно задание.",
            )
        yield row


def _export_item(row: dict) -> dict:
    return {key: row.get(key) for key, _title in RESULT_COLUMNS}


def _csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([title for _key, title in RESULT_COLUMNS])
    for row in rows:
        writer.writerow(["" if row.get(key) is None else row.get(key) for key, _title in RESULT_COLUMNS])
    return buffer.getvalue().encode("utf-8-sig")


def _xlsx(rows: list[dict], notes: tuple[str, ...] | list[str]) -> bytes:
    sheet_rows = [[title for _key, title in RESULT_COLUMNS]]
    for row in rows:
        sheet_rows.append(["" if row.get(key) is None else str(row.get(key)) for key, _title in RESULT_COLUMNS])
    note_rows = [["Пояснение"]]
    for note in notes:
        note_rows.append([note])
    payload = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _root_rels(),
        "xl/workbook.xml": _workbook(),
        "xl/_rels/workbook.xml.rels": _workbook_rels(),
        "xl/styles.xml": _styles(),
        "xl/worksheets/sheet1.xml": _sheet(sheet_rows),
        "xl/worksheets/sheet2.xml": _sheet(note_rows),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as book:
        for name, xml in payload.items():
            book.writestr(name, xml)
    return buffer.getvalue()


def _sheet(rows: list[list[str]]) -> str:
    body = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>"]
    body.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    for index, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row, start=1):
            ref = f"{_column_name(column)}{index}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        body.append(f'<row r="{index}">{"".join(cells)}</row>')
    body.append("</sheetData></worksheet>")
    return "".join(body)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""


def _root_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""


def _workbook() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Компании" sheetId="1" r:id="rId1"/>
    <sheet name="Пояснение" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>
"""


def _workbook_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""


def _styles() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>
"""
