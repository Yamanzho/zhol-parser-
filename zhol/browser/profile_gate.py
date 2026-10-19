"""One Chrome profile is used by one collector at a time.

This does not change how a page is read. If the profile is already open,
the job waits in the queue instead of starting a second Chrome window.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager

from zhol.jobs.queue import JobDeferred


@contextmanager
def exclusive_chrome_profile() -> Iterator[None]:
    import parser_service

    folder = parser_service.chrome_profile_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ".zhol-collector.lock"
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(handle)
        raise JobDeferred(15)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
