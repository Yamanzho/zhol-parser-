import json

import pytest

from zhol.browser.maps import capture
from zhol.browser.page import UnexpectedPage

PLACE = "https://www.google.com/maps/place/Dance+Studio+29/@43.25,76.93,17z"
SPEC = {"platforms": {"google": {"live": {"timeout_ms": 10}}, "yandex": {"live": {"timeout_ms": 10}}}}


class _Page:
    def __init__(self, url):
        self.url = url


def _browser(url):
    browser = capture.MapsBrowser(SPEC)
    browser._page = _Page(url)
    browser._status = 200
    return browser


def test_google_card_does_not_reload_the_place_opened_by_search(monkeypatch, tmp_path):
    gone = []
    monkeypatch.setattr(capture, "_goto", lambda *args: gone.append(args) or 200)
    monkeypatch.setattr(capture, "_loaded_card", lambda page, folder, spec, platform, status: (platform, status))
    assert _browser(PLACE).card("google", "", PLACE, tmp_path / "card") == ("google", 200)
    assert gone == []
    assert (tmp_path / "card").is_dir()


def test_google_card_opens_a_different_place(monkeypatch, tmp_path):
    gone = []
    monkeypatch.setattr(capture, "_goto", lambda page, url, folder, spec: gone.append(url) or 200)
    monkeypatch.setattr(capture, "_loaded_card", lambda page, folder, spec, platform, status: status)
    _browser("https://www.google.com/maps/search/x").card("google", "", PLACE, tmp_path / "card")
    assert gone == [PLACE]


def test_yandex_card_still_navigates(monkeypatch, tmp_path):
    gone = []
    monkeypatch.setattr(capture, "_goto", lambda page, url, folder, spec: gone.append(url) or 200)
    monkeypatch.setattr(capture, "_loaded_card", lambda page, folder, spec, platform, status: status)
    url = "https://yandex.kz/maps/org/x/1/"
    _browser(url).card("yandex", "", url, tmp_path / "card")
    assert gone == [url]


class _Failing:
    url = PLACE

    def locator(self, _selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs):
        raise RuntimeError("Execution context was destroyed\nmore detail")

    def screenshot(self, **_kwargs):
        pass


def test_card_failure_keeps_the_original_exception(tmp_path):
    with pytest.raises(UnexpectedPage) as caught:
        capture._loaded_card(_Failing(), tmp_path, {"timeout_ms": 10}, "google", 200)
    assert "RuntimeError: Execution context was destroyed" in str(caught.value)
    saved = json.loads((tmp_path / "access.json").read_text(encoding="utf-8"))
    assert saved["access"] == "navigation_error"
    assert saved["error"] == "RuntimeError: Execution context was destroyed"
