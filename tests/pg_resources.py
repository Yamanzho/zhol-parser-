"""Disposable PostgreSQL containers for tests.

The data directory is a tmpfs mount. The postgres image declares a VOLUME there,
and without a mount at that path Docker creates an anonymous volume per run that
`docker rm -f` leaves behind.

Every container carries labels with the owning process and host. `reap_stale`
removes only labelled containers, and compose projects named `zholclean<pid>`,
whose owner process on this host is gone. Unlabelled containers, other
applications and live runs are never touched.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time

IMAGE = "postgres:16"
NAME_PREFIX = "zhol-s03-test-"
CLEAN_PROJECT = re.compile(r"zholclean(\d+)")
LABEL = "kz.duna.zhol.test"
LABEL_PID = "kz.duna.zhol.test.pid"
LABEL_HOST = "kz.duna.zhol.test.host"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def container_name(pid: int | None = None) -> str:
    return f"{NAME_PREFIX}{os.getpid() if pid is None else pid}"


def start(name: str) -> str:
    """Start the container and return its URL. On any failure the container is removed."""
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"{LABEL}=postgres",
        "--label",
        f"{LABEL_PID}={os.getpid()}",
        "--label",
        f"{LABEL_HOST}={socket.gethostname()}",
        "--mount",
        "type=tmpfs,destination=/var/lib/postgresql/data",
        "-e",
        "POSTGRES_USER=zhol",
        "-e",
        "POSTGRES_PASSWORD=zhol",
        "-e",
        "POSTGRES_DB=zhol_test",
        "-p",
        "127.0.0.1:0:5432",
        IMAGE,
    )
    try:
        for _ in range(40):
            ready = _docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "zhol", "-d", "zhol_test", check=False)
            if ready.returncode == 0:
                port = _docker("port", name, "5432").stdout.strip().rsplit(":", 1)[1]
                return f"postgresql+psycopg://zhol:zhol@127.0.0.1:{port}/zhol_test"
            time.sleep(0.25)
        raise RuntimeError("test PostgreSQL did not become ready")
    except BaseException:
        remove(name)
        raise


def remove(name: str) -> None:
    _docker("rm", "-f", "-v", name, check=False)


def reap_stale() -> list[str]:
    """Remove leftovers of finished runs on this host. Returns what was removed."""
    removed: list[str] = []
    host = socket.gethostname()
    listed = _docker(
        "ps", "-a", "--filter", f"label={LABEL}",
        "--format", f'{{{{.Names}}}}\t{{{{.Label "{LABEL_PID}"}}}}\t{{{{.Label "{LABEL_HOST}"}}}}',
        check=False,
    )
    for line in listed.stdout.splitlines():
        name, pid, owner_host = (line.split("\t") + ["", ""])[:3]
        if not name.startswith(NAME_PREFIX) or owner_host != host or not pid.isdigit():
            continue
        if not _alive(int(pid)):
            remove(name)
            removed.append(name)
    projects = set(_docker("ps", "-a", "--format", '{{.Label "com.docker.compose.project"}}', check=False).stdout.split())
    projects |= set(_docker("volume", "ls", "--format", '{{.Label "com.docker.compose.project"}}', check=False).stdout.split())
    for project in sorted(projects):
        match = CLEAN_PROJECT.fullmatch(project)
        if not match or _alive(int(match.group(1))):
            continue
        ids = _docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}", check=False).stdout.split()
        if ids:
            _docker("rm", "-f", "-v", *ids, check=False)
        volumes = _docker("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}", check=False)
        if volumes.stdout.split():
            _docker("volume", "rm", *volumes.stdout.split(), check=False)
        removed.append(project)
    return removed
