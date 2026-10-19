"""Discovery request, hits, and run control. No network types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _optional_rating(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    return None


def _optional_count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


class DiscoveryRefused(Exception):
    """Automated catalog access is not permitted, or the saved page left its directory."""


@dataclass
class RunControl:
    """Pause or stop between pages. The caller sets the flags; the engine only reads them."""

    pause_after_pages: int | None = None
    stop_after_pages: int | None = None
    pages_seen: int = 0
    _paused: bool = False
    _stopped: bool = False

    def pause(self) -> None:
        self._paused = True

    def stop(self) -> None:
        self._stopped = True

    def after_page(self) -> str:
        self.pages_seen += 1
        if self._stopped or (self.stop_after_pages is not None and self.pages_seen >= self.stop_after_pages):
            self._stopped = True
            return "stop"
        if self._paused or (self.pause_after_pages is not None and self.pages_seen >= self.pause_after_pages):
            self._paused = True
            return "pause"
        return "continue"


@dataclass
class PlatformEvidence:
    platform: str
    url: str | None
    external_id: str | None
    screenshot_path: str | None
    seen_at: str
    source_path: str
    # True when this listing's phone overlapped the branch it was attached to.
    # The origin listing is true because its own external id was read from the page.
    phone_confirmed: bool = False


@dataclass
class Branch:
    name: str | None
    category: str | None
    subcategory: str | None
    sector: str | None
    address: str | None
    city: str | None
    phones: tuple[str, ...] = ()
    phone_display: tuple[str, ...] = ()
    whatsapp: str | None = None
    instagram: str | None = None
    website: str | None = None
    email: str | None = None
    firm_id: str | None = None
    phone_conflict: bool = False
    alternatives: list[dict] = field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    platforms: dict[str, PlatformEvidence] = field(default_factory=dict)

    def as_progress(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "subcategory": self.subcategory,
            "sector": self.sector,
            "address": self.address,
            "city": self.city,
            "phones": list(self.phones),
            "phone_display": list(self.phone_display),
            "whatsapp": self.whatsapp,
            "instagram": self.instagram,
            "website": self.website,
            "email": self.email,
            "firm_id": self.firm_id,
            "phone_conflict": self.phone_conflict,
            "alternatives": list(self.alternatives),
            "rating": self.rating,
            "review_count": self.review_count,
            "platforms": {
                name: {
                    "platform": item.platform,
                    "url": item.url,
                    "external_id": item.external_id,
                    "screenshot_path": item.screenshot_path,
                    "seen_at": item.seen_at,
                    "source_path": item.source_path,
                    "phone_confirmed": item.phone_confirmed,
                }
                for name, item in self.platforms.items()
            },
        }

    @classmethod
    def from_progress(cls, raw: dict) -> Branch:
        platforms = {}
        for name, item in (raw.get("platforms") or {}).items():
            platforms[name] = PlatformEvidence(
                platform=str(item.get("platform") or name),
                url=item.get("url"),
                external_id=item.get("external_id"),
                screenshot_path=item.get("screenshot_path"),
                seen_at=str(item.get("seen_at") or ""),
                source_path=str(item.get("source_path") or ""),
                phone_confirmed=bool(item.get("phone_confirmed")),
            )
        phones = tuple(str(item) for item in raw.get("phones") or [])
        phone_display = tuple(str(item) for item in raw.get("phone_display") or [])
        alternatives = [item for item in raw.get("alternatives") or [] if isinstance(item, dict)]
        return cls(
            name=raw.get("name"),
            category=raw.get("category"),
            subcategory=raw.get("subcategory"),
            sector=raw.get("sector"),
            address=raw.get("address"),
            city=raw.get("city"),
            phones=phones,
            phone_display=phone_display,
            whatsapp=raw.get("whatsapp"),
            instagram=raw.get("instagram"),
            website=raw.get("website"),
            email=raw.get("email"),
            firm_id=raw.get("firm_id"),
            phone_conflict=bool(raw.get("phone_conflict")),
            alternatives=alternatives,
            rating=_optional_rating(raw.get("rating")),
            review_count=_optional_count(raw.get("review_count")),
            platforms=platforms,
        )


@dataclass
class WalkResult:
    branches: list[Branch]
    progress: dict
    status: str
    evidence_dir: Path
