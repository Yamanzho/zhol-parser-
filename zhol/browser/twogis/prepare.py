"""Prepare approved 2GIS cabinet edits and stop before Save.

The public card is not published. This step does not create a listing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from zhol.browser.cabinet.prepare import PrepareReport, format_prepare, prepare_cabinet
from zhol.browser.twogis.page import FixtureCabinet
from zhol.browser.twogis.session import open_interactive
from zhol.browser.twogis.spec import load_editor
from zhol.fulfillment.adapters.twogis import TwoGisAdapter

__all__ = ["format_prepare", "prepare_2gis_listing"]


def prepare_2gis_listing(
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
    """Fill approved 2GIS fields and stop before Save. approve=True is refused."""
    return prepare_cabinet(
        session,
        order_id,
        page=page,
        evidence_dir=evidence_dir,
        platform="2gis",
        adapter=TwoGisAdapter(),
        editor=load_editor(),
        fixture_cls=FixtureCabinet,
        open_live=open_interactive,
        review_reason="2gis_listing_review",
        create_detail="2GIS prepare edits an existing organization",
        wait_seconds=wait_seconds,
        sleeper=sleeper,
        browser=browser,
        approve=approve,
    )
