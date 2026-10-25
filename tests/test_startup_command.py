"""Launcher contract. The fake docker never talks to the running application."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "Запустить Zhol.command"
COMPOSE = ROOT / "docker-compose.yml"

FAKE_DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$ZHOL_FAKE_DOCKER_LOG"
cmd="$1"
shift
if [ "$cmd" = "info" ]; then
  exit 0
fi
if [ "$cmd" = "inspect" ]; then
  printf '%s\n' "$COMPOSE_PROJECT_NAME"
  exit 0
fi
if [ "$cmd" != "compose" ]; then
  echo "unexpected docker command: $cmd" >&2
  exit 1
fi
args="$*"
if [ "${ZHOL_FAKE_FAIL:-}" = "build" ] && printf '%s' "$args" | grep -q ' build '; then
  echo "build failed" >&2
  exit 1
fi
sub=""
for arg in "$@"; do
  case "$arg" in
    ps|up|build|exec|run) sub="$arg" ;;
  esac
done
if [ "$sub" = "run" ]; then
  if printf '%s' "$args" | grep -q 'schema-check'; then
    exit "${ZHOL_FAKE_SCHEMA:-0}"
  fi
  exit 0
fi
if [ "$sub" = "ps" ]; then
  if printf '%s' "$args" | grep -q -- '-q'; then
    if [ "${ZHOL_FAKE_DB:-present}" = "missing" ]; then
      exit 0
    fi
    printf '%s\n' db123
    exit 0
  fi
  if printf '%s' "$args" | grep -q collector; then
    exit 0
  fi
  if [ "${ZHOL_FAKE_PS:-running}" = "empty" ]; then
    exit 0
  fi
  printf '%s\n' db api web worker browser-worker backup
  exit 0
fi
exit 0
"""

FAKE_OK = "#!/bin/sh\nexit 0\n"


def _install_fakes(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    (bin_dir / "curl").write_text(FAKE_OK, encoding="utf-8")
    (bin_dir / "open").write_text(FAKE_OK, encoding="utf-8")
    for path in bin_dir.iterdir():
        path.chmod(0o755)


def _seed(root: Path) -> None:
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "docker-compose.offline.yml").write_text("services: {}\n", encoding="utf-8")
    gate = "a" * 32
    (root / ".env").write_text(
        "\n".join(
            [
                "POSTGRES_USER=launcher-check-user",
                "POSTGRES_PASSWORD=launcher-check-secret",
                "POSTGRES_DB=launcher-check-db",
                f"ZHOL_LOCAL_GATE={gate}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    shutil.copy(START, root / START.name)


def _run(root: Path, bin_dir: Path, log_path: Path, docker_log: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ZHOL_STARTUP_LOG"] = str(log_path)
    env["ZHOL_FAKE_DOCKER_LOG"] = str(docker_log)
    env.update(extra)
    return subprocess.run(
        ["/bin/bash", str(root / START.name)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_launcher_source_keeps_the_volume_and_names_the_services() -> None:
    text = START.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "down -v" not in text
    assert "volume rm" not in text
    assert "--profile collector" in text
    for line in text.splitlines():
        if " up " in line:
            assert "collector" not in line
    assert "--profile backup up -d --no-build --no-deps api web worker browser-worker backup" in text
    assert "up -d --no-build --no-recreate db" in text
    assert "COMPOSE_PROJECT_NAME" in text
    assert "startup.log" in text
    assert 'profiles: ["backup"]' in compose
    assert 'profiles: ["collector"]' in compose
    assert "127.0.0.1:${API_PORT:-18080}:8000" in compose
    assert "127.0.0.1:${WEB_PORT:-15173}:5173" in compose
    subprocess.run(["/bin/bash", "-n", str(START)], check=True)


def test_missing_database_setting_stops_before_compose(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fakes(bin_dir)
    root = tmp_path / "proj"
    root.mkdir()
    _seed(root)
    env_path = root / ".env"
    env_path.write_text("ZHOL_LOCAL_GATE=" + ("b" * 32) + "\n", encoding="utf-8")
    log_path = tmp_path / "startup.log"
    docker_log = tmp_path / "docker.log"
    result = _run(root, bin_dir, log_path, docker_log)
    assert result.returncode == 1
    assert "POSTGRES_USER" in result.stderr
    assert "startup.log" in result.stderr
    recorded = docker_log.read_text(encoding="utf-8")
    assert "compose" not in recorded
    assert "dir=" + str(root) in log_path.read_text(encoding="utf-8")


def test_existing_database_is_not_recreated_and_second_run_skips_build(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fakes(bin_dir)
    root = tmp_path / "parser-for-timur"
    root.mkdir()
    _seed(root)
    log_path = tmp_path / "startup.log"
    docker_log = tmp_path / "docker.log"
    first = _run(root, bin_dir, log_path, docker_log)
    assert first.returncode == 0, first.stderr
    second = _run(root, bin_dir, log_path, docker_log)
    assert second.returncode == 0, second.stderr
    recorded = docker_log.read_text(encoding="utf-8").splitlines()
    app_up = [line for line in recorded if "--profile backup up -d --no-build --no-deps api web worker browser-worker backup" in line]
    db_keep = [line for line in recorded if "up -d --no-build --no-recreate db" in line]
    builds = [line for line in recorded if " build api web worker browser-worker" in line]
    assert len(app_up) == 2
    assert len(db_keep) == 2
    assert len(builds) == 1
    assert all("collector" not in line or " ps " in line for line in recorded)
    assert all("down" not in line for line in recorded)
    assert "dir=" + str(root) in log_path.read_text(encoding="utf-8")


def test_build_failure_does_not_start_services(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fakes(bin_dir)
    root = tmp_path / "proj-build"
    root.mkdir()
    _seed(root)
    log_path = tmp_path / "startup.log"
    docker_log = tmp_path / "docker.log"
    result = _run(root, bin_dir, log_path, docker_log, ZHOL_FAKE_FAIL="build")
    assert result.returncode == 1
    assert "Не удалось собрать образы." in result.stderr
    recorded = docker_log.read_text(encoding="utf-8")
    assert " build api web worker browser-worker" in recorded
    assert "up -d --no-build --no-deps" not in recorded


def test_missing_database_container_is_created_without_no_recreate(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fakes(bin_dir)
    root = tmp_path / "proj-new"
    root.mkdir()
    _seed(root)
    log_path = tmp_path / "startup.log"
    docker_log = tmp_path / "docker.log"
    result = _run(root, bin_dir, log_path, docker_log, ZHOL_FAKE_DB="missing")
    assert result.returncode == 0, result.stderr
    recorded = docker_log.read_text(encoding="utf-8").splitlines()
    created = [line for line in recorded if line.endswith("up -d --no-build db")]
    assert len(created) == 1
    assert all("--no-recreate" not in line for line in created)


def test_a_service_that_stays_down_is_a_startup_failure(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fakes(bin_dir)
    root = tmp_path / "proj-down"
    root.mkdir()
    _seed(root)
    log_path = tmp_path / "startup.log"
    docker_log = tmp_path / "docker.log"
    result = _run(root, bin_dir, log_path, docker_log, ZHOL_FAKE_PS="empty")
    assert result.returncode == 1
    assert "Служба db не работает." in result.stderr
    assert "Жол запущен" not in result.stdout


APP_UP = "--profile backup up -d --no-build --no-deps api web worker browser-worker backup"


def _launch(tmp_path: Path, name: str, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fakes(bin_dir)
    root = tmp_path / name
    root.mkdir()
    _seed(root)
    docker_log = tmp_path / "docker.log"
    result = _run(root, bin_dir, tmp_path / "startup.log", docker_log, **extra)
    return result, docker_log.read_text(encoding="utf-8").splitlines()


def test_matching_schema_starts_services_without_migrating(tmp_path: Path) -> None:
    result, recorded = _launch(tmp_path, "proj-match", ZHOL_FAKE_SCHEMA="0")
    assert result.returncode == 0, result.stderr
    check = next(i for i, line in enumerate(recorded) if "run --rm --no-deps -T api python -m zhol.cli schema-check" in line)
    start = next(i for i, line in enumerate(recorded) if APP_UP in line)
    assert check < start
    assert all("alembic" not in line for line in recorded)


def test_outdated_schema_stops_before_any_writer_starts(tmp_path: Path) -> None:
    result, recorded = _launch(tmp_path, "proj-old", ZHOL_FAKE_SCHEMA="3")
    assert result.returncode == 1
    assert "Службы Жол не запускались и не перезапускались, данные не менялись." in result.stderr
    assert "Обновить базу Zhol.command" in result.stderr
    assert all(APP_UP not in line for line in recorded)
    assert all("alembic" not in line for line in recorded)


def test_newer_or_unknown_schema_stops_without_offering_upgrade(tmp_path: Path) -> None:
    result, recorded = _launch(tmp_path, "proj-newer", ZHOL_FAKE_SCHEMA="5")
    assert result.returncode == 1
    assert "нужна проверка человеком" in result.stderr
    assert "Обновить базу Zhol.command" not in result.stderr
    assert all(APP_UP not in line for line in recorded)
    assert all("alembic" not in line for line in recorded)


def test_unreadable_schema_stops_before_any_writer_starts(tmp_path: Path) -> None:
    result, recorded = _launch(tmp_path, "proj-unreadable", ZHOL_FAKE_SCHEMA="2")
    assert result.returncode == 1
    assert "Не удалось проверить схему базы." in result.stderr
    assert all(APP_UP not in line for line in recorded)
    assert all("alembic" not in line for line in recorded)


def test_empty_database_gets_tables_before_services_start(tmp_path: Path) -> None:
    result, recorded = _launch(tmp_path, "proj-empty", ZHOL_FAKE_SCHEMA="4")
    assert result.returncode == 0, result.stderr
    migrate = next(i for i, line in enumerate(recorded) if "run --rm --no-deps -T api alembic upgrade head" in line)
    start = next(i for i, line in enumerate(recorded) if APP_UP in line)
    assert migrate < start
    assert all("exec -T api alembic" not in line for line in recorded)
