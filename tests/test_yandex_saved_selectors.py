"""Chromium reads previously saved Yandex HTML from a loopback fixture.

The pages are excerpts of rendered cards and a rendered search list.
This test does not open yandex.kz.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from browser_ops import eval_js
from zhol.agents.scout.maps_selenium import normalize_place
from zhol.browser.yandex import selenium as yandex_selenium

ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "tests" / "fixtures" / "maps-selenium" / "saved"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGES = {
    "/saved/search": SAVED / "search.html",
    "/saved/velara": SAVED / "velara.html",
    "/saved/badi": SAVED / "badi.html",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        file = PAGES.get(path)
        if file is None:
            self.send_error(404)
            return
        raw = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def origin():
    if not Path(CHROME).is_file() and not os.environ.get("ZHOL_CHROME_BINARY"):
        pytest.skip("Chrome is not installed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, name="yandex-saved", daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _chrome(profile: Path):
    options = Options()
    options.page_load_strategy = "eager"
    binary = os.environ.get("ZHOL_CHROME_BINARY", "").strip() or CHROME
    options.binary_location = binary
    profile.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "--host-resolver-rules="
        "MAP yandex.kz 0.0.0.0, MAP www.yandex.kz 0.0.0.0, MAP yandex.ru 0.0.0.0, "
        "MAP yandex.net 0.0.0.0, MAP avatars.mds.yandex.net 0.0.0.0, "
        "MAP maps.yastatic.net 0.0.0.0, MAP static-pano.maps.yandex.ru 0.0.0.0"
    )
    driver_path = os.environ.get("ZHOL_CHROMEDRIVER", "").strip()
    service = Service(executable_path=driver_path) if driver_path else None
    return webdriver.Chrome(options=options, service=service)


def _read(driver, ready: str, script: str):
    assert eval_js(driver, ready) is True
    data = eval_js(driver, script)
    assert data
    return data


def test_saved_search_and_cards_match_the_rendered_markup(origin: str, tmp_path: Path) -> None:
    driver = _chrome(tmp_path / "profile")
    try:
        driver.get(origin + "/saved/search")
        found = _read(driver, yandex_selenium.READY_SEARCH_JS, yandex_selenium.READ_SEARCH_JS)
        assert found["pageNumbers"] == []
        assert found["done"] is False
        assert found["duplicates"] == 0
        assert [(item["externalId"], item["listName"]) for item in found["items"]] == [
            ("138294498069", "Cicek Mangal"),
            ("1724669127", "Halyk Bank"),
            ("238443838116", "Радуга"),
        ]
        assert all(item["href"].startswith("/maps/org/") for item in found["items"])

        driver.get(origin + "/saved/velara")
        velara_raw = _read(driver, yandex_selenium.READY_CARD_JS, yandex_selenium.READ_CARD_JS)
        velara = normalize_place(
            velara_raw,
            platform="yandex",
            external_id="53907907781",
            url="https://yandex.kz/maps/org/velara_beauty/53907907781/",
            city="Алматы",
            query="Салон красоты",
        )
        assert velara is not None
        assert velara.name == "Velara beauty"
        assert velara.address == "Алматы, просп. Назарбаева, 226 этаж 1"
        assert velara.phones == ("77718086666",)
        assert velara.phone_display == ("+7 (771) 808-66-66",)
        assert velara.category == "Салон красоты"
        assert velara.website == "http://zapis.kz/salon/13073"
        assert velara.rating is None
        assert velara.review_count == 0

        driver.get(origin + "/saved/badi")
        badi_raw = _read(driver, yandex_selenium.READY_CARD_JS, yandex_selenium.READ_CARD_JS)
        badi = normalize_place(
            badi_raw,
            platform="yandex",
            external_id="166357199721",
            url="https://yandex.kz/maps/org/badi/166357199721/",
            city="Алматы",
            query="Салон красоты",
        )
        assert badi is not None
        assert badi.name == "Badi"
        assert badi.address == "Алматы, микрорайон Самал-2, 32"
        assert badi.phones == ("77019496008",)
        assert badi.phone_display == ("+7 (701) 949-60-08",)
        assert badi.category == "Салон красоты"
        assert badi.website == "https://zapis.kz/salon/437"
        assert badi.rating == 4.9
        assert badi.review_count == 87
        assert driver.current_url.startswith(origin)
    finally:
        driver.quit()
