"""Prepare a new Google Business Profile listing and stop before Create.

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
from zhol.browser.google.page import FixtureCabinet
from zhol.browser.google.session import open_interactive
from zhol.fulfillment.adapters.google import GoogleBusinessAdapter

__all__ = ["format_prepare", "prepare_google_create"]


def prepare_google_create(
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
    """Fill a blank Google create form and stop before Create. approve=True is refused."""
    return prepare_create_listing(
        session,
        order_id,
        page=page,
        evidence_dir=evidence_dir,
        platform="google",
        adapter=GoogleBusinessAdapter(),
        form=load_create_form("google"),
        fixture_cls=FixtureCabinet,
        open_live=open_interactive,
        wait_seconds=wait_seconds,
        sleeper=sleeper,
        browser=browser,
        approve=approve,
    )
