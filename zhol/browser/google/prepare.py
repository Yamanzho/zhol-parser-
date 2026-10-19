"""Prepare approved Google Business Profile edits and stop before Save.

The public card is not published. This step does not create a listing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from zhol.browser.cabinet.prepare import PrepareReport, format_prepare, prepare_cabinet
from zhol.browser.google.page import FixtureCabinet
from zhol.browser.google.session import open_interactive
from zhol.browser.google.spec import load_editor
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter

__all__ = ["format_prepare", "prepare_google_listing"]


def prepare_google_listing(
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
    """Fill approved Google fields and stop before Save. approve=True is refused."""
    return prepare_cabinet(
        session,
        order_id,
        page=page,
        evidence_dir=evidence_dir,
        platform="google",
        adapter=GoogleBusinessAdapter(),
        editor=load_editor(),
        fixture_cls=FixtureCabinet,
        open_live=open_interactive,
        review_reason="google_listing_review",
        create_detail="Google prepare edits an existing organization",
        wait_seconds=wait_seconds,
        sleeper=sleeper,
        browser=browser,
        approve=approve,
    )
