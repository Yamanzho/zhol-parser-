from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from zhol.cli import main
from zhol.db.models import Audit, CardSnapshot, Company, CompanyPlatformId
from zhol.etalon import import_etalon, score_check

ROOT = Path(__file__).resolve().parents[1]
ETALON = ROOT / "source" / "etalon-50.csv"
if not ETALON.is_file():
    pytest.skip("Файл source/etalon-50.csv не входит в публичный репозиторий.", allow_module_level=True)


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False, future=True)


def _counts(factory) -> tuple[int, int, int, int]:
    with factory() as s:
        return tuple(s.scalar(select(func.count()).select_from(m)) for m in (Company, CardSnapshot, Audit, CompanyPlatformId))


def test_import_is_idempotent(factory) -> None:
    with factory() as s, s.begin():
        first = import_etalon(s, ETALON)
    assert (first.companies, first.snapshots, first.audits) == (50, 150, 50)
    counts = _counts(factory)
    with factory() as s, s.begin():
        second = import_etalon(s, ETALON)
    assert (second.companies, second.snapshots, second.audits) == (0, 0, 0)
    assert _counts(factory) == counts
    assert counts[:3] == (50, 150, 50)


def test_imported_rows_follow_dictionary(factory) -> None:
    with factory() as s, s.begin():
        import_etalon(s, ETALON)
    with factory() as s:
        company = s.scalar(select(Company).where(Company.id_2gis == "70000001066660465"))
        assert (company.source, company.name, company.niche, company.city) == ("etalon_2026_09_30", "Montebello", "салон", "Алматы")
        snaps = {x.platform: x for x in s.scalars(select(CardSnapshot).where(CardSnapshot.company_id == company.id))}
        assert {x.status for x in snaps.values()} == {"verified"}
        assert {x.collected_by for x in snaps.values()} == {"manual_import"}
        assert {x.purpose for x in snaps.values()} == {"prospect"}
        assert snaps["2gis"].fields["prices"] == "empty" and snaps["2gis"].fields["is_ad"] is True
        assert snaps["google"].match_status == "not_found" and snaps["google"].fields["rating"] is None
        assert snaps["yandex"].url.endswith("/113888513635/")
        yandex_id = s.get(CompanyPlatformId, (company.id, "yandex"))
        assert yandex_id.external_id == "113888513635"
        audit = s.scalar(select(Audit).where(Audit.company_id == company.id))
        assert audit.score_total == 72 and audit.discrepancies == [] and audit.status == "draft"
        assert sorted(audit.snapshot_ids) == sorted(x.id for x in snaps.values())


def test_nulls_stored_and_no_empty_became_zero(factory) -> None:
    with factory() as s, s.begin():
        import_etalon(s, ETALON)
    with factory() as s:
        nulls = s.scalar(
            text(
                "select count(*) from card_snapshots, jsonb_each(fields) "
                "where collected_by = 'manual_import' and value = 'null'::jsonb"
            )
        )
        legacy = s.scalar(text("select count(*) from audits where discrepancies -> 0 ? 'legacy_text'"))
    assert nulls > 0
    assert legacy == 16


def test_score_check_matches_all_50(factory) -> None:
    with factory() as s, s.begin():
        import_etalon(s, ETALON)
        results = score_check(s, ETALON)
    assert len(results) == 50
    assert [r.id_2gis for r in results if not r.ok] == []


def test_score_check_cli(factory, capsys, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["score-check"], factory=factory) == 0
    assert main(["score-check"], factory=factory) == 0
    out = capsys.readouterr().out
    assert out.count("Совпало 50 из 50") == 2
    assert "компаний +50" in out and "компаний +0" in out


def test_score_check_reports_mismatch(factory, capsys, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    assert main(["import-etalon"], factory=factory) == 0
    with factory() as s, s.begin():
        s.execute(
            text(
                "update card_snapshots set fields = jsonb_set(fields, '{photos_count}', '0') "
                "where platform = '2gis' and company_id = (select id from companies where id_2gis = '70000001066660465')"
            )
        )
    assert main(["score-check"], factory=factory) == 1
    out = capsys.readouterr().out
    assert "Совпало 49 из 50" in out and "70000001066660465" in out


def test_import_refuses_base_contacts(factory, tmp_path, capsys) -> None:
    path = tmp_path / "base-contacts.csv"
    path.write_text("id_2gis\n", encoding="utf-8")
    assert main(["import-etalon", "--csv", str(path)], factory=factory) == 2
    assert "must not be imported" in capsys.readouterr().err
    assert _counts(factory)[0] == 0
