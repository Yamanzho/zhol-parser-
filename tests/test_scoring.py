from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

from zhol.etalon import EtalonError, collected_at, legacy_discrepancies, read_rows, snapshot_values
from zhol.scoring import derive, load_scoring, score, to_score_row
from zhol.scoring.adapter import COLUMNS

ROOT = Path(__file__).resolve().parents[1]
ETALON = ROOT / "source" / "etalon-50.csv"
CONFIG = ROOT / "config" / "scoring.yaml"
if not ETALON.is_file():
    pytest.skip("Файл source/etalon-50.csv не входит в публичный репозиторий.", allow_module_level=True)


def _original():
    spec = importlib.util.spec_from_file_location("audit_card_reference", ROOT / "source" / "audit_card.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORIGINAL = _original()
CFG = load_scoring(CONFIG)
ROWS = read_rows(ETALON)


def _snapshots(row):
    seen = collected_at(row)
    return {p: {**v, "collected_at": seen} for p, v in snapshot_values(row).items()}


def test_port_matches_original_on_etalon_rows() -> None:
    for row in ROWS:
        assert score(row, CFG) == ORIGINAL.score(row), row["id_2gis"]


def test_original_matches_csv_expectations() -> None:
    for row in ROWS:
        s = ORIGINAL.score(row)
        assert [s["отзывы"], s["цены"], s["фото"], s["контакты"], s["итого"]] == [
            int(row[c]) for c in ("балл_отзывы", "балл_цены", "балл_фото", "балл_контакты", "балл_итого")
        ], row["id_2gis"]


POOLS = {
    "2гис_оценок": ["", "0", "9", "10", "49", "50", "199", "200", "1078"],
    "2гис_рейтинг": ["", "3.9", "4.0", "4.4", "4.5", "4.7", "4.8", "5.0", "4,8"],
    "2гис_доля_ответов": ["", "0", "0.01", "0.19", "0.2", "0.59", "0.6", "1", "45", "60%"],
    "дата_просмотра": ["2026-09-30", ""],
    "2гис_последний_отзыв": ["", "2026-08-31", "2026-08-30", "2026-09-30", "bad"],
    "яндекс_оценок": ["", "0", "19", "20"],
    "google_отзывов": ["", "0", "19", "20"],
    "2гис_цены": ["", "прайс компании", "фото прайса", "пусто"],
    "2гис_онлайн_запись": ["", "0", "1", "да"],
    "яндекс_цены": ["", "есть", "есть, давно не обновлялись", "нет"],
    "яндекс_новости": ["", "0", "1"],
    "2гис_фото": ["", "4", "5", "19", "20", "49", "50"],
    "яндекс_фото": ["", "0", "1", "9", "10", "29", "30"],
    "2гис_телефон": ["", "0", "1"],
    "2гис_whatsapp": ["", "0", "1"],
    "2гис_instagram": ["", "0", "1"],
    "2гис_ссылка": ["", "  ", "example.kz", "https://instagram.com/x", "t.me/x"],
    "яндекс_статус": ["", "найдена", "найдена, адрес другой", "не найдена"],
    "яндекс_владелец_следит": ["", "0", "1"],
    "google_статус": ["", "найдена", "найдена, адрес или филиал другой", "не найдена"],
    "google_права_заявлены": ["", "0", "1", "заявлены"],
    "расхождения": ["", " ", "в Яндексе другой адрес"],
}


def test_port_matches_original_on_boundary_rows() -> None:
    rng = random.Random(20260930)
    for _ in range(3000):
        row = {column: rng.choice(values) for column, values in POOLS.items()}
        assert score(row, CFG) == ORIGINAL.score(row), row


def test_thresholds_come_from_config() -> None:
    row = dict(ROWS[0])
    changed = {**CFG, "photos": {**CFG["photos"], "twogis_photos": [[10_000, 15]]}}
    assert score(row, changed)["фото"] < score(row, CFG)["фото"]


@pytest.mark.parametrize("row", ROWS, ids=[r["id_2gis"] for r in ROWS])
def test_adapter_reproduces_csv_score(row) -> None:
    snapshots = _snapshots(row)
    adapted = to_score_row(snapshots, derive(snapshots["2gis"]), legacy_discrepancies(row))
    assert score(adapted, CFG) == ORIGINAL.score(row)


def test_empty_cells_become_null_not_zero() -> None:
    nulls = zeros_from_empty = 0
    for row in ROWS:
        snapshots = snapshot_values(row)
        for platform, key, column in COLUMNS:
            value = snapshots[platform]["fields"][key]
            if row[column].strip() == "":
                nulls += 1
                assert value is None, (row["id_2gis"], column)
                zeros_from_empty += value in (0, False)
    assert nulls > 0 and zeros_from_empty == 0


def test_explicit_zero_stays_zero_and_null_maps_back_to_empty() -> None:
    row = next(r for r in ROWS if r["google_отзывов"] == "0" and r["google_рейтинг"] == "")
    snapshots = _snapshots(row)
    assert snapshots["google"]["fields"]["reviews_count"] == 0
    assert snapshots["google"]["fields"]["rating"] is None
    adapted = to_score_row(snapshots, derive(snapshots["2gis"]), [])
    assert (adapted["google_отзывов"], adapted["google_рейтинг"]) == ("0", "")


def test_derived_fields() -> None:
    row = ROWS[0]
    derived = derive(_snapshots(row)["2gis"])
    assert derived["reply_share"] == int(row["2гис_ответов"]) / int(row["2гис_показано_отзывов"])
    assert derived["fresh_review_days"] == 12
    assert derive({"fields": {"reviews_shown": 0, "replies_count": 0}, "collected_at": None}) == {
        "reply_share": 0,
        "fresh_review_days": None,
    }
    assert derive({"fields": {"reviews_shown": None, "replies_count": 3}, "collected_at": None})["reply_share"] is None


def test_legacy_discrepancies() -> None:
    with_text = next(r for r in ROWS if r["расхождения"].strip())
    without = next(r for r in ROWS if not r["расхождения"].strip())
    assert legacy_discrepancies(with_text) == [{"legacy_text": with_text["расхождения"].strip()}]
    assert legacy_discrepancies(without) == []


def test_every_snapshot_field_has_a_source() -> None:
    for platform, values in snapshot_values(ROWS[0]).items():
        assert set(values["fields"]) <= set(values["field_sources"]), platform
        assert all(s["source"] == "etalon_2026_09_30" for s in values["field_sources"].values())


def test_base_contacts_refused(tmp_path: Path) -> None:
    fake = tmp_path / "base-contacts.csv"
    fake.write_text("id_2gis\n1\n", encoding="utf-8")
    with pytest.raises(EtalonError, match="must not be imported"):
        read_rows(fake)


def test_wrong_layer_refused(tmp_path: Path) -> None:
    text = ETALON.read_text(encoding="utf-8-sig").replace("середина", "формула", 1)
    path = tmp_path / "etalon.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(EtalonError, match="layers"):
        read_rows(path)
