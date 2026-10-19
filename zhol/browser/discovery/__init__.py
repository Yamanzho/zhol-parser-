"""Saved-page discovery. Live catalog requests stay closed."""

from zhol.browser.discovery.engine import WalkResult, walk_saved
from zhol.browser.discovery.types import DiscoveryRefused, RunControl

__all__ = ["DiscoveryRefused", "RunControl", "WalkResult", "walk_saved"]
