from __future__ import annotations

from pathlib import Path

from zhol.agents.scout.pilot import run_manual
from zhol.browser.page import AccessRestricted, PageDocument
from zhol.browser.twogis.access import (
    ACCESS_BLOCKED,
    ACCESS_DENIED,
    CARD_VISIBLE,
    NAVIGATION_ERROR,
    ROBOT_CHECK,
    classify_access,
    diagnose_saved,
    extraction_allowed,
    outcome_for,
)

ROBOT_PAGE = (
    "Мы заметили подозрительную активность с вашего IP адреса. "
    "Чтобы подтвердить, что вы не робот, пройдите проверку."
)


def test_robot_check_is_not_a_company_card() -> None:
    access = classify_access(http_status=200, url="https://captcha.2gis.ru/", text=ROBOT_PAGE)
    assert access == ROBOT_CHECK
    assert extraction_allowed(access) is False
    assert outcome_for(access) == ACCESS_BLOCKED


def test_access_statuses() -> None:
    assert classify_access(http_status=403, url="https://2gis.kz/almaty/firm/1", text="Доступ ограничен") == ACCESS_DENIED
    assert classify_access(http_status=None, url="https://2gis.kz/almaty/firm/1", text="", navigation_error="timeout") == NAVIGATION_ERROR
    assert classify_access(http_status=200, url="https://2gis.kz/almaty/firm/1", text="") == NAVIGATION_ERROR
    card = classify_access(
        http_status=200,
        url="https://2gis.kz/almaty/firm/70000001066660465",
        text="Montebello\n4.9\n1078 оценок",
    )
    assert card == CARD_VISIBLE
    assert extraction_allowed(card) is True


def test_saved_robot_page_is_diagnosed_without_extraction(tmp_path: Path) -> None:
    folder = tmp_path / "70000001066660465"
    folder.mkdir()
    (folder / "text.txt").write_text(ROBOT_PAGE, encoding="utf-8")
    (folder / "screenshot.png").write_bytes(b"png")
    record = diagnose_saved(folder, http_status=200, url="https://captcha.2gis.ru/")
    assert record["access"] == ROBOT_CHECK
    assert record["extracted"] is False
    assert (folder / "access.json").is_file()
    assert (folder / "screenshot.png").read_bytes() == b"png"


def test_manual_pilot_stops_when_the_robot_check_remains(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    saved = previous / "70000001066660465"
    saved.mkdir(parents=True)
    (saved / "text.txt").write_text(ROBOT_PAGE, encoding="utf-8")

    def opener(url: str, folder: Path, spec: dict, *, headed: bool, pause_seconds: float):
        assert headed is True
        assert pause_seconds > 0
        assert url.endswith("/70000001066660465")
        raise AccessRestricted(
            "access=robot_check outcome=access_blocked",
            screenshot=str(folder / "screenshot.png"),
            url="https://captcha.2gis.ru/",
            access=ROBOT_CHECK,
            outcome=ACCESS_BLOCKED,
        )

    report = tmp_path / "report.md"
    code = run_manual(
        ["70000001066660465"],
        output=tmp_path / "manual",
        report_path=report,
        previous=previous,
        opener=opener,
    )
    text = report.read_text(encoding="utf-8")
    assert code == 2
    assert "robot_check" in text
    assert "access_blocked" in text
    assert "не читались" in text
    assert (saved / "access.json").is_file()


def test_manual_pilot_extracts_only_a_visible_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("zhol.agents.scout.pilot._store", lambda *_args, **_kwargs: "снимок не записан")
    def opener(url: str, folder: Path, spec: dict, *, headed: bool, pause_seconds: float):
        folder.mkdir(parents=True, exist_ok=True)
        from zhol.browser.fixtures import LoadedPage

        return LoadedPage(
            url=url,
            html="<html>Montebello</html>",
            raw_path=str(folder / "page.html"),
            file_name="page.html",
            screenshot_path=str(folder / "screenshot.png"),
            kind="card",
            document=PageDocument(
                page="card",
                platform="2gis",
                external_id="70000001066660465",
                fields={"name": "Montebello", "rating": "4.9"},
                hrefs={},
            ),
        )

    report = tmp_path / "report.md"
    code = run_manual(
        ["70000001066660465"],
        output=tmp_path / "manual",
        report_path=report,
        previous=tmp_path / "missing",
        opener=opener,
    )
    text = report.read_text(encoding="utf-8")
    assert code == 0
    assert "Доступ: card_visible" in text
    assert "Итог: collected" in text
