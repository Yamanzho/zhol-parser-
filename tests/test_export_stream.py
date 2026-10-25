"""Exports stream past the old 5000-row cap. A failed write removes its temporary file."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from zhol.integration.schema import XLSX_ROW_LIMIT, IntegrationError, export_bytes, export_file


def _row(number: int, name: str | None = None) -> dict[str, object]:
    return {
        "name": name if name is not None else f"Салон {number}",
        "phone": None,
        "address": "Абая 10",
        "category": "салон красоты",
        "city": "Алматы",
        "platform_label": "2ГИС",
        "source_label": "2ГИС",
    }


def _files() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("zhol-export-*"))


def test_csv_and_json_accept_more_than_5000_rows() -> None:
    seen = {"count": 0}

    def rows():
        for number in range(1, 6001):
            seen["count"] += 1
            yield _row(number)

    csv_bytes, _media, name = export_bytes("csv", rows(), notes=("пояснение",))
    assert name.endswith(".csv")
    assert seen["count"] == 6000
    text = csv_bytes.decode("utf-8-sig")
    assert text.startswith("Название")
    assert "Салон 6000" in text
    seen["count"] = 0
    raw, _media, json_name = export_bytes("json", rows(), notes=("пояснение",))
    body = json.loads(raw.decode("utf-8"))
    assert json_name.endswith(".json")
    assert body["total"] == 6000
    assert seen["count"] == 6000


def test_formula_text_is_escaped_in_csv_and_xlsx() -> None:
    def rows():
        yield _row(1, "=1+1")

    csv_bytes, _media, _name = export_bytes("csv", rows(), notes=())
    assert "'=1+1" in csv_bytes.decode("utf-8-sig")
    sheet, _media, book = export_bytes("xlsx", rows(), notes=())
    assert book.endswith(".xlsx")
    assert sheet[:2] == b"PK"
    import io
    import zipfile

    xml = zipfile.ZipFile(io.BytesIO(sheet)).read("xl/worksheets/sheet1.xml")
    assert b"'=1+1" in xml


def test_an_empty_export_has_headers_and_no_rows() -> None:
    csv_bytes, _media, _name = export_bytes("csv", iter(()), notes=())
    assert csv_bytes.decode("utf-8-sig").splitlines() == ["Название,Телефон,Адрес,Категория,Город,Площадка,Источник"]
    raw, _media, _json_name = export_bytes("json", iter(()), notes=("пояснение",))
    body = json.loads(raw.decode("utf-8"))
    assert body["items"] == []
    assert body["total"] == 0


def test_a_failed_export_removes_the_temporary_file() -> None:
    before = _files()

    def rows():
        yield _row(1)
        raise RuntimeError("database closed")

    with pytest.raises(RuntimeError, match="database closed"):
        export_file("csv", rows(), notes=())
    assert _files() == before


def test_xlsx_stops_at_the_spreadsheet_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("zhol.integration.schema.XLSX_ROW_LIMIT", 3)
    with pytest.raises(IntegrationError) as caught:
        export_bytes("xlsx", (_row(number) for number in range(1, 6)), notes=())
    assert caught.value.status == 400
    assert "Excel" in caught.value.message
    assert XLSX_ROW_LIMIT == 1_048_576
