"""Saved-page browser collector.

Live website adapters are not registered. 2GIS, Yandex, and Google stay on
saved HTML, and a live source is refused before a page is opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zhol.browser.discovery import google as google_adapter
from zhol.browser.discovery import twogis as twogis_adapter
from zhol.browser.discovery import yandex as yandex_adapter
from zhol.browser.discovery.engine import walk_saved
from zhol.browser.discovery.session import LocalHtmlPage
from zhol.browser.discovery.types import Branch, DiscoveryRefused, RunControl, WalkResult
from zhol.export.mailing import _key

_ADAPTERS = {
    "2gis": twogis_adapter,
    "yandex": yandex_adapter,
    "google": google_adapter,
}
_SAVED_SOURCES = frozenset({"fixture", "manual"})
PANEL_NOTE = (
    "Режим браузера показан и выключен: устное разрешение 2ГИС не подтверждено. "
    "Нужно письменное разрешение на автоматический сбор, хранение и коммерческое использование. "
    "Проверка «не робот» не обходится."
)


@dataclass(frozen=True)
class CollectedCompany:
    external_id: str | None
    source: str
    name: str | None
    city: str | None
    address: str | None
    category: str | None
    phone: str | None
    whatsapp: str | None
    instagram: str | None
    website: str | None
    rating: float | None
    review_count: int | None
    collected_at: str | None
    source_url: str | None


@dataclass
class CollectionResult:
    companies: list[CollectedCompany]
    checkpoint: dict
    status: str
    processed: int
    duplicates: int
    errors: list[str]
    unmatched: list[dict]
    walk: WalkResult


def panel_browser_modes() -> list[dict]:
    """The admin form lists each browser collector.

    Live 2GIS stays disabled until the written grant is set.
    Yandex and Google stay disabled: their terms do not allow this collection.
    A localhost or local HTML source does not grant those sites.
    """
    from zhol.agents.scout.browser_2gis import browser_mode as twogis_mode
    from zhol.agents.scout.browser_google import browser_mode as google_mode
    from zhol.agents.scout.browser_yandex import browser_mode as yandex_mode

    return [twogis_mode(), yandex_mode(), google_mode()]


class SavedHtmlCollector:
    """Reads a local search directory through the same page calls Playwright uses."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def collect(
        self,
        *,
        city: str,
        category: str,
        platforms: tuple[str, ...],
        source_mode: str,
        evidence_dir: str | Path,
        query: str | None = None,
        limit: int | None = None,
        control: RunControl | None = None,
        page: Any = None,
        resume_state: dict | None = None,
    ) -> CollectionResult:
        _authorize(platforms, source_mode)
        if limit is not None and limit < 1:
            raise DiscoveryRefused("limit must be a positive number")
        if query and _key(query) != _key(category):
            raise DiscoveryRefused("saved pages do not contain this query")
        owned = page is None
        session = page if page is not None else LocalHtmlPage()
        try:
            walked = walk_saved(
                self.root,
                city=city,
                category=category,
                platforms=platforms,
                source=source_mode,
                evidence_dir=evidence_dir,
                control=control,
                page=session,
                limit=limit,
                resume_state=resume_state,
            )
        finally:
            if owned:
                session.close()
        return _result(walked, platforms, source_mode)


def _authorize(platforms: tuple[str, ...], source_mode: str) -> None:
    if not platforms:
        raise DiscoveryRefused("platforms is empty")
    for name in platforms:
        adapter = _ADAPTERS.get(name)
        if adapter is None:
            raise DiscoveryRefused(f"unsupported platform {name}")
        if source_mode not in _SAVED_SOURCES:
            adapter.refuse_live(source_mode)


def _result(walked: WalkResult, platforms: tuple[str, ...], source_mode: str) -> CollectionResult:
    primary = platforms[0]
    companies = [_company(branch, primary, source_mode) for branch in walked.branches if _external(branch)]
    errors: list[str] = []
    for item in walked.progress.get("blocked") or []:
        errors.append(f"{item.get('platform')}: {item.get('access')}")
    for path in walked.progress.get("missing") or []:
        errors.append(f"missing page: {path}")
    unmatched = [item for item in walked.progress.get("unmatched") or [] if isinstance(item, dict)]
    return CollectionResult(
        companies=companies,
        checkpoint=dict(walked.progress),
        status=walked.status,
        processed=len(companies),
        duplicates=int(walked.progress.get("duplicates") or 0),
        errors=errors,
        unmatched=unmatched,
        walk=walked,
    )


def _external(branch: Branch) -> str | None:
    if branch.firm_id:
        return branch.firm_id
    for evidence in branch.platforms.values():
        if evidence.external_id:
            return evidence.external_id
    return None


def _company(branch: Branch, platform: str, source_mode: str) -> CollectedCompany:
    evidence = branch.platforms.get(platform)
    if evidence is None and branch.platforms:
        evidence = next(iter(branch.platforms.values()))
    return CollectedCompany(
        external_id=_external(branch),
        source="saved_pages" if source_mode == "fixture" else "manual_browser",
        name=branch.name,
        city=branch.city,
        address=branch.address,
        category=branch.category,
        phone=branch.phones[0] if branch.phones else None,
        whatsapp=branch.whatsapp,
        instagram=branch.instagram,
        website=branch.website,
        rating=branch.rating,
        review_count=branch.review_count,
        collected_at=None if evidence is None else evidence.seen_at,
        source_url=None if evidence is None else evidence.url,
    )
