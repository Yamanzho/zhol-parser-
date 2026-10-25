"""Export stays bounded. Rows past the cap are not read into a list."""

from __future__ import annotations

import json

import pytest

from zhol.integration.schema import EXPORT_ROW_CAP, IntegrationError, ensure_export_size, export_bytes


def _row(number: int) -> dict[str, object]:
    return {
        "name": f"Салон {number}",
        "phone": None,
        "address": "Абая 10",
        "category": "салон красоты",
        "city": "Алматы",
        "platform_label": "2ГИС",
        "source_label": "2ГИС",
    }


def test_a_long_iterator_stops_at_the_cap() -> None:
    seen = {"count": 0}

    def rows():
        for number in range(1, 20_000):
            seen["count"] += 1
            yield _row(number)

    with pytest.raises(IntegrationError) as caught:
        export_bytes("csv", rows(), notes=("пояснение",))
    assert caught.value.status == 400
    assert str(EXPORT_ROW_CAP) in caught.value.message
    assert seen["count"] == EXPORT_ROW_CAP + 1


def test_an_oversized_list_is_refused_before_a_file_is_built() -> None:
    seen = {"count": 0}

    class _List(list):
        def __iter__(self):
            seen["count"] += 1
            return super().__iter__()

    rows = _List(_row(number) for number in range(EXPORT_ROW_CAP + 1))
    with pytest.raises(IntegrationError):
        export_bytes("json", rows, notes=())
    assert seen["count"] == 0


def test_rows_within_the_cap_stream_to_every_format() -> None:
    def rows():
        yield _row(1)
        yield _row(2)

    csv_bytes, _media, name = export_bytes("csv", rows(), notes=("пояснение",))
    assert name.endswith(".csv")
    assert "Салон 1" in csv_bytes.decode("utf-8-sig")
    raw, _media, json_name = export_bytes("json", rows(), notes=("пояснение",))
    body = json.loads(raw.decode("utf-8"))
    assert json_name.endswith(".json")
    assert body["total"] == 2
    assert body["notes"] == ["пояснение"]
    sheet, media, book_name = export_bytes("xlsx", rows(), notes=("пояснение",))
    assert media.endswith("sheet")
    assert book_name.endswith(".xlsx")
    assert sheet[:2] == b"PK"
    ensure_export_size(EXPORT_ROW_CAP)
    with pytest.raises(IntegrationError):
        ensure_export_size(EXPORT_ROW_CAP + 1)
