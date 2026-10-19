"""Checks that a public checkout can be installed without this machine's data."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from scripts.zhol_write_env import GRANT_KEYS, write_env

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {
    ".env",
    "etalon-50.csv",
    "base-contacts.csv",
}
SECRET_MARKERS = (
    b"-----BEGIN ",
    b"sk-ant-",
    b"sk-proj-",
    b"written-browser-retention-local",
    b"written-browser-retention-commercial",
)


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True, **kwargs)  # type: ignore[arg-type]
    if completed.returncode != 0:
        raise AssertionError(completed.stdout[-1500:] + completed.stderr[-1500:])
    return completed


def _env_map(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key] = value
    return values


def test_example_has_empty_grants_and_install_creates_unique_env(tmp_path: Path) -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (*GRANT_KEYS, "POSTGRES_PASSWORD", "ZHOL_ADMIN_TOKEN", "ZHOL_LOCAL_GATE"):
        assert f"{key}=\n" in example or example.endswith(f"{key}=")
    first = tmp_path / "first.env"
    assert write_env(first, ROOT / ".env.example") == "created"
    created = _env_map(first)
    for key in GRANT_KEYS:
        assert created[key] == ""
    assert created["POSTGRES_USER"] == "zhol"
    assert created["POSTGRES_DB"] == "zhol"
    assert len(created["POSTGRES_PASSWORD"]) >= 24
    assert len(created["ZHOL_ADMIN_TOKEN"]) == 64
    assert len(created["ZHOL_LOCAL_GATE"]) == 64
    second_dir = tmp_path / "again"
    second_dir.mkdir()
    shutil.copy(first, second_dir / ".env")
    before = (second_dir / ".env").read_bytes()
    assert write_env(second_dir / ".env", ROOT / ".env.example") == "kept"
    assert (second_dir / ".env").read_bytes() == before


def test_install_uses_one_launch_agent_and_does_not_touch_live_home(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    for name in ("zhol-install-macos.sh", "zhol-collector-macos.sh", "zhol_write_env.py"):
        shutil.copy(ROOT / "scripts" / name, tree / "scripts" / name)
    shutil.copy(ROOT / ".env.example", tree / ".env.example")
    shutil.copy(ROOT / "docker-compose.yml", tree / "docker-compose.yml")
    shutil.copy(ROOT / "docker-compose.offline.yml", tree / "docker-compose.offline.yml")
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ZHOL_INSTALL_SKIP_DOCKER"] = "1"
    env["ZHOL_INSTALL_SKIP_LAUNCHD"] = "1"
    completed = _run(["/bin/bash", str(tree / "scripts" / "zhol-install-macos.sh")], cwd=tree, env=env)
    assert "env=created" in completed.stdout
    values = _env_map(tree / ".env")
    for key in GRANT_KEYS:
        assert values[key] == ""
    again = _run(["/bin/bash", str(tree / "scripts" / "zhol-install-macos.sh")], cwd=tree, env=env)
    assert "env=kept" in again.stdout
    assert _env_map(tree / ".env")["POSTGRES_PASSWORD"] == values["POSTGRES_PASSWORD"]
    plist = home / "Library" / "LaunchAgents" / "kz.duna.zhol.collector.plist"
    body = plist.read_text(encoding="utf-8")
    assert body.count("zhol-collector-macos.sh") == 1
    rendered = _run(
        [
            "docker",
            "compose",
            "--env-file",
            str(tree / ".env"),
            "-f",
            str(tree / "docker-compose.yml"),
            "-f",
            str(tree / "docker-compose.offline.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=tree,
    )
    import json

    document = json.loads(rendered.stdout)
    assert "collector" not in document["services"]
    for service in document["services"].values():
        for binding in service.get("ports") or []:
            published = str(binding.get("published", ""))
            host_ip = str(binding.get("host_ip", ""))
            if published:
                assert host_ip == "127.0.0.1"
    live_plist = Path.home() / "Library" / "LaunchAgents" / "kz.duna.zhol.collector.plist"
    if live_plist.is_file():
        assert str(tree) not in live_plist.read_text(encoding="utf-8")


def test_shell_scripts_parse() -> None:
    paths = list((ROOT / "scripts").glob("*.sh"))
    paths.extend(ROOT.glob("*.command"))
    assert paths
    for path in paths:
        _run(["bash", "-n", str(path)])


def test_publish_set_has_no_secrets() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--cached"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    paths = [ROOT / line for line in listed.stdout.splitlines() if line]
    assert paths
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if path.name in FORBIDDEN_NAMES or relative.startswith("data/") and path.name != ".gitkeep":
            offenders.append(relative)
            continue
        if path.suffix in {".xlsx", ".xls", ".sql", ".dump"}:
            offenders.append(relative)
            continue
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        blob = path.read_bytes()
        if b"\0" in blob[:512]:
            continue
        for marker in SECRET_MARKERS:
            if marker in blob and path.name != "test_public_release.py":
                if marker.startswith(b"written-browser-retention") and path.suffix in {".md", ".py", ".example", ".yml", ".yaml", ".sh"}:
                    if b"GRANT=written-browser-retention" in blob or b"GRANT: written-browser-retention" in blob:
                        offenders.append(relative)
                    continue
                offenders.append(f"{relative}:{marker.decode()}")
    assert offenders == []


def _developer_password() -> str:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return ""
    return _env_map(env_file).get("POSTGRES_PASSWORD", "")


def test_fresh_postgres_is_empty_and_separate(tmp_path: Path) -> None:
    env_file = tmp_path / "install.env"
    write_env(env_file, ROOT / ".env.example")
    values = _env_map(env_file)
    override = tmp_path / "ports.yml"
    override.write_text("services:\n  db:\n    ports:\n      - \"127.0.0.1::5432\"\n", encoding="utf-8")
    project = f"zholclean{os.getpid()}"
    base = [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(override),
    ]
    try:
        _run([*base, "up", "-d", "--wait", "db"], cwd=ROOT)
        container = _run([*base, "ps", "-q", "db"], cwd=ROOT).stdout.strip()
        assert container
        inspected = subprocess.run(
            ["docker", "inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            check=True,
            text=True,
            capture_output=True,
        )
        found = ""
        for line in inspected.stdout.splitlines():
            if line.startswith("POSTGRES_PASSWORD="):
                found = line.split("=", 1)[1]
        matches_throwaway = found == values["POSTGRES_PASSWORD"]
        assert matches_throwaway, "throwaway database did not receive its own password"
        developer = _developer_password()
        reused = bool(developer) and found == developer
        assert not reused, "throwaway database reused the developer password"
        port_line = _run([*base, "port", "db", "5432"], cwd=ROOT).stdout.strip()
        host_port = port_line.rsplit(":", 1)[1]
        url = f"postgresql+psycopg://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}@127.0.0.1:{host_port}/{values['POSTGRES_DB']}"
        alembic = ROOT / ".venv" / "bin" / "alembic"
        assert alembic.is_file()
        subprocess.run(
            [str(alembic), "upgrade", "head"],
            cwd=ROOT,
            check=True,
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        engine = create_engine(url)
        with engine.connect() as connection:
            count = connection.execute(text("select count(*) from companies")).scalar()
        engine.dispose()
        assert count == 0
        digest = hashlib.sha256(found.encode()).hexdigest()
        assert len(digest) == 64
    finally:
        subprocess.run([*base, "down", "-v"], cwd=ROOT, check=False, capture_output=True, text=True)


def test_grant_in_example_is_refused(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("ZHOL_2GIS_BROWSER_GRANT=written-browser-retention-local\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        write_env(tmp_path / ".env", example)
    assert not (tmp_path / ".env").exists()
