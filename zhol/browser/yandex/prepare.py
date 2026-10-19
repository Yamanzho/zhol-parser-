"""Prepare approved Yandex Business edits and stop before Save.

The public card is not published. A later stage has to be approved by a person
before anything is submitted.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from zhol.browser.cabinet.prepare import PrepareReport, format_prepare, prepare_cabinet, wait_for_person
from zhol.browser.yandex.page import FixtureCabinet
from zhol.browser.yandex.session import open_interactive
from zhol.browser.yandex.spec import load_editor
from zhol.fulfillment.adapters.yandex import YandexBusinessAdapter

__all__ = ["PrepareReport", "format_prepare", "prepare_yandex_listing", "wait_for_person"]


def prepare_yandex_listing(
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
    """Fill approved Yandex fields and stop before Save. approve=True is refused."""
    return prepare_cabinet(
        session,
        order_id,
        page=page,
        evidence_dir=evidence_dir,
        platform="yandex",
        adapter=YandexBusinessAdapter(),
        editor=load_editor(),
        fixture_cls=FixtureCabinet,
        open_live=open_interactive,
        review_reason="yandex_listing_review",
        create_detail="Yandex prepare edits an existing organization",
        wait_seconds=wait_seconds,
        sleeper=sleeper,
        browser=browser,
        approve=approve,
    )
