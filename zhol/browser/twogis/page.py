"""Cabinet page used by the 2GIS prepare step.

Saved HTML is the test stand-in. A live page uses the same control names and never clicks.
"""

from __future__ import annotations

from collections.abc import Mapping

from zhol.browser.cabinet.form import FixtureCabinet as CabinetFixture
from zhol.browser.cabinet.form import PlaywrightCabinet as CabinetPlaywright
from zhol.browser.twogis.spec import load_editor


def _submit_names() -> tuple[str, ...]:
    return load_editor().submit_names


class FixtureCabinet(CabinetFixture):
    def __init__(self, html: str, markers: Mapping[str, str]) -> None:
        super().__init__(html, markers, submit_names=_submit_names)


class PlaywrightCabinet(CabinetPlaywright):
    def __init__(self, page: object, markers: Mapping[str, str], *, close=None) -> None:
        super().__init__(page, markers, submit_names=_submit_names, close=close)
