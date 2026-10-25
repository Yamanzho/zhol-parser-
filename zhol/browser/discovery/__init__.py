"""Saved-page discovery. Live catalog requests stay closed.

The engine is loaded on first use. `engine` imports `zhol.agents.scout.identity`,
which imports `zhol.browser.discovery.types`; an eager import here would make
that chain circular whenever identity is imported first.
"""

from typing import Any

from zhol.browser.discovery.types import DiscoveryRefused, RunControl, WalkResult

__all__ = ["DiscoveryRefused", "RunControl", "WalkResult", "walk_saved"]


def __getattr__(name: str) -> Any:
    if name == "walk_saved":
        from zhol.browser.discovery.engine import walk_saved

        return walk_saved
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
