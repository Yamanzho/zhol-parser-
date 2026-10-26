"""Test PostgreSQL containers leave no volumes or containers behind, and cleanup touches nothing it does not own."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import pg_resources
from tests.conftest import ROOT

CHILD_TEST = """
import os, time
from pathlib import Path
from sqlalchemy import create_engine, text

def test_child(postgres_url):
    engine = create_engine(postgres_url)
    with engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1
    engine.dispose()
    Path(os.environ["CHILD_PID_FILE"]).write_text(str(os.getpid()))
    mode = os.environ["CHILD_MODE"]
    if mode == "fail":
        assert False, "planned failure"
    if mode == "error":
        raise RuntimeError("planned error")
    if mode in ("sigterm", "sigkill"):
        time.sleep(120)
"""


def _volumes() -> set[str]:
    return set(subprocess.run(["docker", "volume", "ls", "-q"], check=True, capture_output=True, text=True).stdout.split())


def _exists(name: str) -> bool:
    return subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode == 0


def _child(tmp_path: Path, mode: str) -> tuple[int, int]:
    test_file = tmp_path / f"test_child_{mode}.py"
    test_file.write_text(CHILD_TEST, encoding="utf-8")
    pid_file = tmp_path / f"{mode}.pid"
    env = {**os.environ, "CHILD_MODE": mode, "CHILD_PID_FILE": str(pid_file), "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "--rootdir", str(tmp_path), str(test_file)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if mode in ("sigterm", "sigkill"):
        for _ in range(240):
            if pid_file.exists():
                break
            time.sleep(0.25)
        assert pid_file.exists(), "child did not reach the test body"
        proc.send_signal(signal.SIGTERM if mode == "sigterm" else signal.SIGKILL)
    proc.communicate(timeout=180)
    return proc.returncode, int(pid_file.read_text())


@pytest.mark.parametrize(("mode", "exit_code"), [("pass", 0), ("fail", 1), ("error", 1), ("sigterm", 2)])
def test_session_container_is_removed_without_volumes(tmp_path: Path, mode: str, exit_code: int) -> None:
    before = _volumes()
    code, child_pid = _child(tmp_path, mode)
    assert code == exit_code
    assert not _exists(pg_resources.container_name(child_pid))
    assert _volumes() - before == set()


def test_killed_session_leaves_no_volume_and_is_reaped(tmp_path: Path) -> None:
    before = _volumes()
    code, child_pid = _child(tmp_path, "sigkill")
    assert code == -signal.SIGKILL
    name = pg_resources.container_name(child_pid)
    assert _volumes() - before == set()
    assert name in pg_resources.reap_stale()
    assert not _exists(name)


def test_repeated_runs_do_not_accumulate_volumes(tmp_path: Path) -> None:
    before = _volumes()
    for run in range(3):
        run_dir = tmp_path / str(run)
        run_dir.mkdir()
        assert _child(run_dir, "pass")[0] == 0
        assert _volumes() - before == set()


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _sleeper(name: str, *labels: str) -> None:
    args = ["docker", "run", "-d", "--name", name, "--mount", "type=tmpfs,destination=/var/lib/postgresql/data"]
    for label in labels:
        args += ["--label", label]
    subprocess.run([*args, "--entrypoint", "sleep", pg_resources.IMAGE, "300"], check=True, capture_output=True)


def test_reaper_only_removes_finished_runs_it_owns() -> None:
    me, dead, host = os.getpid(), _dead_pid(), socket.gethostname()
    tag = f"{me}-{time.monotonic_ns()}"
    owned = lambda pid, h=host: (f"{pg_resources.LABEL}=postgres", f"{pg_resources.LABEL_PID}={pid}", f"{pg_resources.LABEL_HOST}={h}")
    names = {
        "dead": f"{pg_resources.NAME_PREFIX}dead-{tag}",
        "live": f"{pg_resources.NAME_PREFIX}live-{tag}",
        "other_host": f"{pg_resources.NAME_PREFIX}otherhost-{tag}",
        "unlabelled": f"{pg_resources.NAME_PREFIX}unlabelled-{tag}",
        "other_app": f"other-app-{tag}",
        "clean_dead": f"zholclean{dead}-db-{tag}",
        "clean_live": f"zholclean{me}-db-{tag}",
    }
    volumes = {"clean_dead": f"zholclean{dead}_pgdata", "clean_live": f"zholclean{me}_pgdata"}
    try:
        _sleeper(names["dead"], *owned(dead))
        _sleeper(names["live"], *owned(me))
        _sleeper(names["other_host"], *owned(dead, "another-host"))
        _sleeper(names["unlabelled"])
        _sleeper(names["other_app"], f"{pg_resources.LABEL_PID}={dead}")
        for key, pid in (("clean_dead", dead), ("clean_live", me)):
            project = f"com.docker.compose.project=zholclean{pid}"
            _sleeper(names[key], project)
            subprocess.run(["docker", "volume", "create", "--label", project, volumes[key]], check=True, capture_output=True)

        removed = pg_resources.reap_stale()

        assert names["dead"] in removed and not _exists(names["dead"])
        assert f"zholclean{dead}" in removed and not _exists(names["clean_dead"])
        assert volumes["clean_dead"] not in _volumes()
        for key in ("live", "other_host", "unlabelled", "other_app", "clean_live"):
            assert _exists(names[key]), key
        assert volumes["clean_live"] in _volumes()
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", *names.values()], capture_output=True)
        subprocess.run(["docker", "volume", "rm", *volumes.values()], capture_output=True)
