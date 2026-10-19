"""Liveness check for a worker that writes ZHOL_WORKER_HEARTBEAT."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    raw = os.environ.get("ZHOL_WORKER_HEARTBEAT", "").strip()
    if not raw:
        print("ZHOL_WORKER_HEARTBEAT is unset", file=sys.stderr)
        return 1
    path = Path(raw)
    if not path.is_file():
        print(f"heartbeat missing: {path}", file=sys.stderr)
        return 1
    age = time.time() - path.stat().st_mtime
    if age > 90:
        print(f"heartbeat stale: {age:.0f}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
