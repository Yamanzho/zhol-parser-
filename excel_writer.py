"""Запись карточек в xlsx без базы данных.

Пока идёт сбор, файл называется result.partial.xlsx в папке назначения.
Каждые 15 успешных строк он перезаписывается. В конце и при остановке
файл переименовывается в выбранный .xlsx.

Лист «Результат» содержит только пользовательские колонки.
Лист «_meta» скрыт и хранит branch_id для дедупликации.
Лист «_queries» скрыт и хранит, какие запросы дошли до подтверждённого конца выдачи.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from openpyxl import Workbook, load_workbook

log = logging.getLogger("gis.excel")

CITY_NAME = "Алматы"

FLUSH_EVERY = 5
PARTIAL_NAME = "result.partial.xlsx"
RESULT_SHEET = "Результат"
META_SHEET = "_meta"
QUERY_SHEET = "_queries"
QUERY_HEADERS = [
    "query",
    "status",
    "link_collection_complete",
    "stop_reason",
    "finished_at",
]
HEADERS = [
    "Название",
    "Телефон",
    "WhatsApp",
    "2GIS",
    "Instagram",
    "Город",
    "Адрес",
]
FIRM_ID = re.compile(r"/firm/(\d+)")


class ExcelWriteError(Exception):
    """Файл Excel нельзя прочитать или записать."""


def _unique_join(values) -> str:
    if values is None or values == "":
        return ""
    if isinstance(values, str):
        parts = [part.strip() for part in values.split(";")]
    else:
        parts = [str(value).strip() for value in values if value]
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return "; ".join(unique)


def _public_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    return text.split("?", 1)[0].split("#", 1)[0]


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value)


def log_hint() -> str:
    return "logs/collector.log"


def _display_row(record: dict) -> list[str]:
    return [
        record.get("name") or "",
        _unique_join(record.get("phones")),
        _unique_join(record.get("whatsapp")),
        _public_url(record.get("two_gis_url") or ""),
        _unique_join(record.get("instagram")),
        CITY_NAME,
        record.get("address") or "",
    ]


class ExcelWriter:
    def __init__(self, final_path: Path, resume: bool = False):
        self.final_path = Path(final_path)
        if self.final_path.suffix.lower() != ".xlsx":
            raise ExcelWriteError("Нужен файл с расширением .xlsx.")
        if not self.final_path.parent.is_dir():
            raise ExcelWriteError(f"Папка недоступна: {self.final_path.parent}")
        self.partial_path = self.final_path.parent / PARTIAL_NAME
        self.rows: list[dict] = []
        self.seen: set[str] = set()
        self.queries: dict[str, dict] = {}
        self._since_flush = 0
        if resume:
            source = self._newer_existing()
            if source is not None:
                self._load_existing(source)

    def _load_existing(self, path: Path) -> None:
        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
        except OSError as exc:
            log.exception("Не прочитан %s", path)
            raise ExcelWriteError(
                "Не удалось прочитать существующий Excel. Закройте файл, если он открыт. "
                f"Подробности в {log_hint()}."
            ) from exc
        try:
            if RESULT_SHEET not in workbook.sheetnames:
                raise ExcelWriteError(
                    "Этот файл нельзя дописать: нет листа «Результат». Запустите без --resume."
                )
            sheet = workbook[RESULT_SHEET]
            table = list(sheet.iter_rows(values_only=True))
            if not table or list(table[0][: len(HEADERS)]) != HEADERS:
                raise ExcelWriteError(
                    "Этот файл нельзя дописать: колонки листа «Результат» другие. Запустите без --resume."
                )
            data_rows = [row for row in table[1:] if not _blank_row(row)]
            branch_ids = _branch_ids(workbook, data_rows)
            for cells, branch_id in zip(data_rows, branch_ids):
                if branch_id in self.seen:
                    continue
                self.seen.add(branch_id)
                self.rows.append(
                    {
                        "name": _cell(cells[0]),
                        "phones": _cell(cells[1]),
                        "whatsapp": _cell(cells[2]),
                        "two_gis_url": _cell(cells[3]),
                        "instagram": _cell(cells[4]),
                        "requested_city": _cell(cells[5]),
                        "address": _cell(cells[6]),
                        "branch_id": branch_id,
                    }
                )
            self.queries = _query_rows(workbook)
        except ExcelWriteError:
            raise
        except (OSError, ValueError, IndexError) as exc:
            log.exception("Файл %s не прочитан", path)
            raise ExcelWriteError(
                f"Не удалось прочитать существующий Excel. Подробности в {log_hint()}."
            ) from exc
        finally:
            workbook.close()

    def add(self, record: dict) -> bool:
        branch_id = str(record.get("branch_id") or "")
        if not branch_id or branch_id in self.seen:
            return False
        self.seen.add(branch_id)
        self.rows.append(record)
        self._since_flush += 1
        if self._since_flush >= FLUSH_EVERY:
            self.persist()
        return True

    def completed_queries(self) -> set[str]:
        return {
            name
            for name, item in self.queries.items()
            if item.get("status") == "complete" and item.get("link_collection_complete")
        }

    def set_query(
        self,
        query: str,
        *,
        status: str,
        link_collection_complete: bool,
        stop_reason: str,
        finished_at: str,
    ) -> None:
        self.queries[query] = {
            "status": status,
            "link_collection_complete": bool(link_collection_complete),
            "stop_reason": stop_reason or "",
            "finished_at": finished_at or "",
        }

    def _newer_existing(self) -> Path | None:
        candidates = [path for path in (self.partial_path, self.final_path) if path.exists()]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def flush(self) -> None:
        if not self.rows and not self.queries:
            self._since_flush = 0
            return
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = RESULT_SHEET
        sheet.append(HEADERS)
        meta = workbook.create_sheet(META_SHEET)
        meta.sheet_state = "hidden"
        meta.append(["branch_id"])
        for record in self.rows:
            sheet.append(_display_row(record))
            meta.append([str(record.get("branch_id") or "")])
        queries = workbook.create_sheet(QUERY_SHEET)
        queries.sheet_state = "hidden"
        queries.append(QUERY_HEADERS)
        for name in sorted(self.queries):
            item = self.queries[name]
            queries.append(
                [
                    name,
                    item.get("status") or "",
                    "yes" if item.get("link_collection_complete") else "no",
                    item.get("stop_reason") or "",
                    item.get("finished_at") or "",
                ]
            )
        for column in ("B", "C", "E"):
            for cell in sheet[column]:
                cell.number_format = "@"
        for cell in meta["A"]:
            cell.number_format = "@"
        for cell in queries["A"]:
            cell.number_format = "@"
        try:
            workbook.save(self.partial_path)
        except OSError as exc:
            log.exception("Не записан %s", self.partial_path)
            raise ExcelWriteError(
                "Не удалось записать Excel. Закройте файл, если он открыт. "
                f"Подробности в {log_hint()}."
            ) from exc
        self._since_flush = 0

    def persist(self) -> Path | None:
        if not self.rows and not self.queries:
            if self.partial_path.exists():
                try:
                    self.partial_path.unlink()
                except OSError as exc:
                    log.warning("Не удалён пустой временный файл: %s", exc)
            return None
        self.flush()
        try:
            os.replace(self.partial_path, self.final_path)
        except OSError as exc:
            log.exception("Не переименован %s -> %s", self.partial_path, self.final_path)
            raise ExcelWriteError(
                f"Данные сохранены во временный файл {self.partial_path.name}. "
                "Закройте Excel и запустите сохранение ещё раз. "
                f"Подробности в {log_hint()}."
            ) from exc
        return self.final_path

    def finish(self) -> Path | None:
        return self.persist()


def _blank_row(row) -> bool:
    return all(cell is None or str(cell).strip() == "" for cell in row[: len(HEADERS)])


def _branch_ids(workbook, data_rows: list) -> list[str]:
    if META_SHEET in workbook.sheetnames:
        meta_rows = list(workbook[META_SHEET].iter_rows(values_only=True))
        ids = [str(row[0]) for row in meta_rows[1:] if row and row[0] is not None and str(row[0]).strip()]
        if len(ids) != len(data_rows):
            raise ExcelWriteError(
                "Этот файл нельзя дописать: служебный лист не совпадает со строками результата."
            )
        return ids
    ids = []
    for cells in data_rows:
        url = _cell(cells[3]) if len(cells) > 3 else ""
        match = FIRM_ID.search(url)
        if not match:
            raise ExcelWriteError(
                "Этот файл нельзя дописать: нет служебного листа _meta. Запустите без --resume."
            )
        ids.append(match.group(1))
    return ids


def _query_rows(workbook) -> dict[str, dict]:
    if QUERY_SHEET not in workbook.sheetnames:
        return {}
    table = list(workbook[QUERY_SHEET].iter_rows(values_only=True))
    found: dict[str, dict] = {}
    for row in table[1:]:
        if not row or row[0] is None or not str(row[0]).strip():
            continue
        name = str(row[0]).strip()
        complete_text = str(row[2] or "").strip().casefold() if len(row) > 2 else ""
        found[name] = {
            "status": str(row[1] or "").strip() if len(row) > 1 else "",
            "link_collection_complete": complete_text in {"yes", "true", "1", "да"},
            "stop_reason": str(row[3] or "") if len(row) > 3 else "",
            "finished_at": str(row[4] or "") if len(row) > 4 else "",
        }
    return found
