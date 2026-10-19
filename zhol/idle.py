"""Idle entrypoint for worker, browser-worker, and scheduler until those services exist."""

from __future__ import annotations

import signal
import sys
import time


def main() -> int:
    stopped = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stopped:
        time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
