"""Prepare a new 2GIS listing and stop before Create.

The public card is not created. A later stage has to be approved by a person
before anything is submitted.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from zhol.browser.cabinet.create import prepare_create_listing
from zhol.browser.cabinet.editor import load_create_form
from zhol.browser.cabinet.prepare import PrepareReport, format_prepare
from zhol.browser.twogis.page import FixtureCabinet
from zhol.browser.twogis.session import open_interactive
from zhol.fulfillment.adapters.twogis import TwoGisAdapter

__all__ = ["format_prepare", "prepare_2gis_create"]


def prepare_2gis_create(
    session: Session,
    order_id: int,
    *,
    page: Any,
    evidence_dir: str | Path,
    wait_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
    browser: Any = None,
    approve: bool = False,
) -> PrepareReport:
    """Fill a blank 2GIS create form and stop before Create. approve=True is refused."""
    return prepare_create_listing(
        session,
        order_id,
        page=page,
        evidence_dir=evidence_dir,
        platform="2gis",
        adapter=TwoGisAdapter(),
        form=load_create_form("2gis"),
        fixture_cls=FixtureCabinet,
        open_live=open_interactive,
        wait_seconds=wait_seconds,
        sleeper=sleeper,
        browser=browser,
        approve=approve,
    )
