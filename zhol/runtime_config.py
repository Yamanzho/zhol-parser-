"""Required process settings. Empty grants stay closed. Nothing here invents a password."""

from __future__ import annotations

import os
from collections.abc import Mapping


class DeploymentConfigError(RuntimeError):
    """A process started without the settings it needs."""


def validate_process_environment(env: Mapping[str, str] | None = None) -> None:
    """Fail startup when the database address is missing.

    Browser grants are optional. An empty grant does not authorize a site.
    This does not connect to the database and does not change a password.
    """
    source = os.environ if env is None else env
    database_url = str(source.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise DeploymentConfigError("DATABASE_URL is not set")
    if "://" not in database_url:
        raise DeploymentConfigError("DATABASE_URL is not a database address")
