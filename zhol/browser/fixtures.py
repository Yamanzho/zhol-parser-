from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zhol.browser.evidence import screenshot_state, usable_screenshot
from zhol.browser.page import PageDocument, PageNotLoaded

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "pages"


@dataclass(frozen=True)
class LoadedPage:
    url: str
    html: str
    raw_path: str
    file_name: str
    screenshot_path: str | None
    kind: str
    document: PageDocument | None = None


class FixtureStore:
    """Reads saved public pages. It does not open a network connection."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or DEFAULT_FIXTURES).resolve()
        self.screenshot_file = self.root / "blank.png"
        self.screenshot_state = screenshot_state(self.screenshot_file)
        # Optional card evidence. A missing or unreadable file is not a path we keep.
        self.screenshot = self.screenshot_file if self.screenshot_state == "present" else None

    def read(self, platform: str, file_name: str, url: str, *, kind: str) -> LoadedPage:
        path = self.root / platform / file_name
        if not path.is_file():
            raise PageNotLoaded(str(path))
        return LoadedPage(
            url=url,
            html=path.read_text(encoding="utf-8"),
            raw_path=str(path),
            file_name=file_name,
            screenshot_path=usable_screenshot(self.screenshot),
            kind=kind,
        )

    def card(self, platform: str, external_id: str, url: str) -> LoadedPage:
        return self.read(platform, f"{external_id}.html", url, kind="card")

    def search(self, platform: str, id_2gis: str, url: str) -> LoadedPage:
        return self.read(platform, f"{id_2gis}.search.html", url, kind="search")
