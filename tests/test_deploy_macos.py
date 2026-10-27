"""Deployment entry contract. A fake docker and launchctl keep all state in a JSON file;
nothing here talks to the running application, Docker Desktop or launchd."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts" / "zhol-deploy-macos.sh"
DEPLOY_PY = ROOT / "scripts" / "zhol_deploy.py"
COLLECTOR_SH = ROOT / "scripts" / "zhol-collector-macos.sh"

SECRET = "deploy-check-secret-value"
GATE = "g" * 64
ADMIN = "t" * 64
OLD_APP = "sha256:old-app"
OLD_WEB = "sha256:old-web"
NEW_APP = "sha256:built-app"
NEW_WEB = "sha256:built-web"

FAKE_DOCKER = r'''#!__PYTHON__
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["ZHOL_FAKE_STATE"])
state = json.loads(state_path.read_text())
argv = sys.argv[1:]
with open(os.environ["ZHOL_FAKE_DOCKER_LOG"], "a") as log:
    log.write(" ".join(argv) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def resolve(ref):
    if ref in state["images"]:
        return state["images"][ref]
    if ref in state["images"].values():
        return ref
    return None

cmd = argv[0]
if cmd == "info":
    sys.exit(0)
if cmd == "inspect":
    out = []
    for cid in argv[1:]:
        svc = cid[2:]
        c = state["containers"][svc]
        out.append({
            "Id": cid,
            "Image": c["image"],
            "RestartCount": c.get("restarts", 0),
            "State": {"Status": "running" if c["running"] else "exited",
                      **({"Health": {"Status": c["health"]}} if c.get("health") else {})},
            "Config": {"Labels": {"com.docker.compose.service": svc,
                                  "com.docker.compose.config-hash": c["hash"],
                                  "com.docker.compose.project.config_files": c["files"]}},
        })
    print(json.dumps(out))
    sys.exit(0)
if cmd == "image":
    ref = argv[-1]
    found = resolve(ref)
    if not found:
        sys.exit(1)
    print(found)
    sys.exit(0)
if cmd == "tag":
    found = resolve(argv[1])
    if not found:
        sys.exit(1)
    state["images"][argv[2]] = found
    save()
    sys.exit(0)
if cmd != "compose":
    print("unexpected docker command", cmd, file=sys.stderr)
    sys.exit(1)

rest = argv[1:]
files, project = [], ""
while rest and rest[0].startswith("-"):
    flag = rest[0]
    if flag in ("--project-name", "--project-directory", "-f", "--profile"):
        if flag == "-f":
            files.append(rest[1])
        if flag == "--project-name":
            project = rest[1]
        rest = rest[2:]
    else:
        rest = rest[1:]
sub, args = (rest[0], rest[1:]) if rest else ("", [])
if sub == "version":
    print("Docker Compose version fake")
    sys.exit(0)
offline = next((f for f in files if f.endswith("docker-compose.offline.yml")), None)
has_override = bool(offline) and Path(offline).is_file() and "ZHOL_CONFIG_DIR" in Path(offline).read_text()
both = ",".join(files)

if sub == "config":
    if "--quiet" in args:
        sys.exit(0)
    if "--hash" in args:
        for svc, value in state["hashes"].items():
            print(svc, value + ("" if has_override else "-nooverride"))
        sys.exit(0)
    url = {"DATABASE_URL": "postgresql+psycopg://u:" + os.environ["ZHOL_FAKE_SECRET"] + "@db:5432/d"}
    api_env = dict(url)
    web_env = {"ZHOL_API_ORIGIN": "http://api:8000"}
    api_vol, worker_vol = [], []
    if has_override:
        api_env.update({"ZHOL_CONFIG_DIR": "/offline-config", "ZHOL_LOCAL_ACCESS": "1", "ZHOL_LOCAL_GATE": "x" * 64})
        web_env["ZHOL_LOCAL_GATE"] = "x" * 64
        api_vol = [{"type": "bind", "source": "/x/tests/golden/review-engine/config", "target": "/offline-config"}]
        worker_vol = [{"type": "bind", "source": "/x/f", "target": "/app/tests/fixtures"},
                      {"type": "bind", "source": "/x/a", "target": "/app/data/admin"}]
    print(json.dumps({"services": {
        "db": {"volumes": [{"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"}]},
        "api": {"environment": api_env, "volumes": api_vol, "ports": [{"host_ip": "127.0.0.1", "target": 8000}]},
        "web": {"environment": web_env, "ports": [{"host_ip": "127.0.0.1", "target": 5173}]},
        "worker": {"environment": dict(url), "volumes": worker_vol},
        "browser-worker": {"environment": dict(url)},
        "scheduler": {"environment": dict(url)},
        "backup": {},
    }}))
    sys.exit(0)
if sub == "ps":
    for svc in state["containers"]:
        print("c-" + svc)
    sys.exit(0)
if sub == "exec":
    joined = " ".join(args)
    if "pg_isready" in joined:
        sys.exit(0)
    if "alembic_version" in joined:
        print(state["revision"])
        sys.exit(0)
    if "FROM jobs" in joined:
        print(state["active"])
        sys.exit(0)
    if " df " in " " + joined + " ":
        print("Filesystem 1024-blocks Used Available Capacity Mounted")
        print("/dev/vdb1 100000000 1 %d 1%% /var/lib/postgresql/data" % state["df_avail"])
        sys.exit(0)
    sys.exit(1)
if sub == "build":
    if state.get("fail_build"):
        print("ERROR: failed to solve: pip install exited 1", file=sys.stderr)
        sys.exit(1)
    for svc in [a for a in args if not a.startswith("-")]:
        state["images"][project + "-" + svc] = state["build_ids"]["web" if svc == "web" else "app"]
    save()
    sys.exit(0)
if sub == "run":
    joined = " ".join(args)
    svc = [a for a in args if not a.startswith("-")][0]
    if "schema-check" in joined:
        sys.exit(state.get("schema_exit", 0))
    if "deploy_probe" in joined:
        report = {"dist_version": "0.2.0", "code": "code-1", "heads": [state["revision"]]}
        report.update(state.get("probe_overrides", {}).get(svc, {}))
        if "--startup" in joined:
            report.update({"app_version": "0.2.0", "paths": ["/api/v1/meta", "/api/v1/jobs/{job_id}", "/health"]})
        print(json.dumps(report))
        sys.exit(0)
    if svc == "web":
        print(state.get("web_version", "0.2.0"))
        sys.exit(0)
    sys.exit(1)
if sub == "up":
    for svc in [a for a in args if not a.startswith("-")]:
        c = state["containers"].setdefault(svc, {"restarts": 0})
        image = state["images"].get(project + "-" + svc)
        if image:
            c["image"] = image
        c["hash"] = state["hashes"][svc] + ("" if has_override else "-nooverride")
        c["files"] = both
        c["running"] = True
    save()
    sys.exit(0)
print("unexpected compose command", sub, file=sys.stderr)
sys.exit(1)
'''

FAKE_LAUNCHCTL = r'''#!__PYTHON__
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["ZHOL_FAKE_STATE"])
state = json.loads(state_path.read_text())
argv = sys.argv[1:]
with open(os.environ["ZHOL_FAKE_DOCKER_LOG"], "a") as log:
    log.write("launchctl " + " ".join(argv) + "\n")
cmd = argv[0]
if cmd == "print":
    if state.get("collector") == "running":
        print("\tstate = running\n\tpid = 4242")
        sys.exit(0)
    sys.exit(113)
if cmd == "bootout":
    state["collector"] = "stopped"
elif cmd in ("bootstrap", "kickstart"):
    state["collector"] = "running"
state_path.write_text(json.dumps(state))
sys.exit(0)
'''

FAKE_VENV_PYTHON = r'''#!__PYTHON__
import json, os
from pathlib import Path
state = json.loads(Path(os.environ["ZHOL_FAKE_STATE"]).read_text())
report = {"dist_version": "0.1.0", "code": "code-1", "heads": [state["revision"]]}
report.update(state.get("probe_overrides", {}).get("host", {}))
print(json.dumps(report))
'''

WEB_SRC = """export const routes = ["/api/v1/meta", "/health"];
export const job = (id: number) => `/api/v1/jobs/${id}?refresh=${Date.now()}`;
"""

OFFLINE = """services:
  api:
    environment:
      ZHOL_CONFIG_DIR: /offline-config
      ZHOL_LOCAL_ACCESS: "1"
"""


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "proj"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.state_path = tmp / "state.json"
        self.docker_log = tmp / "docker.log"
        self.log_dir = tmp / "logs"
        self.server: ThreadingHTTPServer | None = None

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def update(self, **values: Any) -> None:
        data = self.state
        data.update(values)
        self.state_path.write_text(json.dumps(data))

    def calls(self) -> list[str]:
        return self.docker_log.read_text().splitlines() if self.docker_log.exists() else []

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "HOME": str(self.home),
                "ZHOL_FAKE_STATE": str(self.state_path),
                "ZHOL_FAKE_DOCKER_LOG": str(self.docker_log),
                "ZHOL_FAKE_SECRET": SECRET,
                "ZHOL_DEPLOY_LOG_DIR": str(self.log_dir),
                "ZHOL_DEPLOY_HEALTH_TIMEOUT": "2",
                "ZHOL_DEPLOY_SETTLE_SECONDS": "0",
                "ZHOL_DEPLOY_MIN_FREE_GB": "0.001",
            }
        )
        env.pop("COMPOSE_FILE", None)
        env.update(extra)
        return subprocess.run(
            ["/bin/bash", str(self.root / "scripts" / "zhol-deploy-macos.sh"), *args],
            cwd=self.root, env=env, capture_output=True, text=True, check=False, timeout=120,
        )

    def logs(self) -> str:
        return "\n".join(p.read_text() for p in sorted(self.log_dir.glob("*.log")))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=root,
                   check=True, capture_output=True)


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("__PYTHON__", sys.executable), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def _start_server(env: Env) -> int:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            state = env.state
            api_image = state["containers"].get("api", {}).get("image")
            if self.path == "/health":
                if api_image in state.get("unhealthy_images", []):
                    self._send(503, "{}")
                else:
                    self._send(200, json.dumps({"status": "ok"}))
            elif self.path == "/openapi.json":
                self._send(200, json.dumps({"info": {"version": "0.2.0"},
                                            "paths": {"/api/v1/meta": {}, "/api/v1/jobs/{job_id}": {}, "/health": {}}}))
            elif self.path == "/":
                self._send(200, '<div id="root"></div>')
            else:
                self._send(404, "{}")

        def _send(self, code: int, body: str) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env.server = server
    return server.server_address[1]


@pytest.fixture
def deploy_env(tmp_path: Path):
    env = Env(tmp_path)
    for name, text in (("docker", FAKE_DOCKER), ("launchctl", FAKE_LAUNCHCTL)):
        _write(env.bin / name, text, 0o755)
    env.home.mkdir()
    root = env.root
    root.mkdir()
    port = _start_server(env)
    _write(root / "docker-compose.yml", "services: {}\n")
    _write(root / "docker-compose.offline.yml", OFFLINE)
    _write(root / "tests/golden/review-engine/config/models.yaml", "models: {}\n")
    _write(root / "pyproject.toml", '[project]\nname = "zhol"\nversion = "0.2.0"\n')
    _write(root / "Dockerfile", "FROM scratch\n")
    _write(root / "zhol/__init__.py", "")
    _write(root / "web/package.json", json.dumps({"name": "zhol-web", "version": "0.2.0"}))
    _write(root / "web/src/api.ts", WEB_SRC)
    _write(root / ".gitignore", ".env\ndata/\n.venv/\n")
    (root / "scripts").mkdir()
    for script in (DEPLOY_SH, DEPLOY_PY, COLLECTOR_SH):
        shutil.copy(script, root / "scripts" / script.name)
    _write(
        root / ".env",
        "\n".join(
            [
                "POSTGRES_USER=deploy-user",
                f"POSTGRES_PASSWORD={SECRET}",
                "POSTGRES_DB=deploy-db",
                f"ZHOL_LOCAL_GATE={GATE}",
                f"ZHOL_ADMIN_TOKEN={ADMIN}",
                "ANTHROPIC_API_KEY=offline",
                "OPENAI_API_KEY=offline",
                "GOOGLE_API_KEY=offline",
                "ZHOL_2GIS_BROWSER_GRANT=" + "written-browser-retention-" + "local",
                f"API_PORT={port}",
                f"WEB_PORT={port}",
                "",
            ]
        ),
        0o600,
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "release")
    both = f"{root}/docker-compose.yml,{root}/docker-compose.offline.yml"

    def container(image: str, svc: str) -> dict[str, Any]:
        return {"image": image, "hash": "h-" + svc, "files": both, "running": True, "restarts": 0}

    state = {
        "images": {"proj-api": OLD_APP, "proj-worker": OLD_APP, "proj-browser-worker": OLD_APP, "proj-web": OLD_WEB},
        "containers": {
            "db": {**container("sha256:pg", "db"), "health": "healthy", "files": f"{root}/docker-compose.yml"},
            "api": container(OLD_APP, "api"),
            "worker": container(OLD_APP, "worker"),
            "browser-worker": container(OLD_APP, "browser-worker"),
            "web": container(OLD_WEB, "web"),
            "backup": container("sha256:pg", "backup"),
        },
        "hashes": {s: "h-" + s for s in ("db", "api", "worker", "browser-worker", "web", "backup", "scheduler")},
        "build_ids": {"app": NEW_APP, "web": NEW_WEB},
        "revision": "b6d2f4a81c30",
        "active": "running_jobs|0\npending_collection_jobs|0\nrunning_batches|0\nitems_in_progress|0",
        "df_avail": 50_000_000,
        "collector": "absent",
    }
    env.state_path.write_text(json.dumps(state))
    yield env
    if env.server:
        env.server.shutdown()


def _install_collector(env: Env, running: bool) -> None:
    plist = env.home / "Library/LaunchAgents/kz.duna.zhol.collector.plist"
    _write(plist, f"<string>{env.root}/scripts/zhol-collector-macos.sh</string>\n")
    _write(env.root / ".venv/bin/python", FAKE_VENV_PYTHON, 0o755)
    env.update(collector="running" if running else "stopped")


def _changing(calls: list[str]) -> list[str]:
    words = (" build", " up ", "tag ", "launchctl bootout", "launchctl bootstrap", "launchctl kickstart")
    return [c for c in calls if any(w in " " + c for w in words)]


# ---------- the seven required scenarios ----------


def test_missing_compose_override_file_stops_before_any_change(deploy_env: Env) -> None:
    (deploy_env.root / "docker-compose.offline.yml").unlink()
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "docker-compose.offline.yml" in result.stderr
    assert "службы не перезапускались" in result.stderr
    assert all(" compose " not in " " + c or "compose version" in c for c in deploy_env.calls())
    assert _changing(deploy_env.calls()) == []


def test_override_without_offline_settings_is_rejected(deploy_env: Env) -> None:
    (deploy_env.root / "docker-compose.offline.yml").write_text("services: {}\n")
    _git(deploy_env.root, "commit", "-qam", "empty override")
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "ZHOL_CONFIG_DIR" in result.stderr
    assert _changing(deploy_env.calls()) == []


def test_missing_required_configuration_names_keys_without_values(deploy_env: Env) -> None:
    env_file = deploy_env.root / ".env"
    lines = [line for line in env_file.read_text().splitlines() if not line.startswith(("ZHOL_ADMIN_TOKEN", "ZHOL_LOCAL_GATE"))]
    env_file.write_text("\n".join(lines) + "\n")
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "ZHOL_ADMIN_TOKEN" in result.stderr and "ZHOL_LOCAL_GATE" in result.stderr
    assert SECRET not in result.stdout + result.stderr + deploy_env.logs()
    assert all(" compose " not in " " + c or "compose version" in c for c in deploy_env.calls())


def test_failed_image_build_leaves_running_services_and_tags_untouched(deploy_env: Env) -> None:
    deploy_env.update(fail_build=True)
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "Сборка образов не удалась" in result.stderr
    assert "не останавливались и не перезапускались" in result.stderr
    calls = deploy_env.calls()
    assert not [c for c in calls if " up " in c]
    state = deploy_env.state
    assert state["containers"]["api"]["image"] == OLD_APP
    assert state["images"]["proj-api"] == OLD_APP
    assert not (deploy_env.root / "data/deploy/state.json").exists()


def test_failed_api_health_check_rolls_back_to_saved_images(deploy_env: Env) -> None:
    deploy_env.update(unhealthy_images=[NEW_APP])
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "API не ответил на /health" in result.stderr
    assert "Выполнен откат" in result.stderr
    ups = [c for c in deploy_env.calls() if " up " in c]
    assert len(ups) == 2
    state = deploy_env.state
    for svc in ("api", "worker", "browser-worker"):
        assert state["containers"][svc]["image"] == OLD_APP
    assert state["containers"]["web"]["image"] == OLD_WEB
    assert state["images"]["zhol-release/proj-api:" + _head(deploy_env)[:12]] == NEW_APP
    assert not (deploy_env.root / "data/deploy/state.json").exists()


def test_active_collection_job_blocks_deployment(deploy_env: Env) -> None:
    deploy_env.update(active="running_jobs|1\npending_collection_jobs|2\nrunning_batches|1\nitems_in_progress|1")
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "Идёт сбор" in result.stderr
    assert "задач в работе: 1" in result.stderr
    assert _changing(deploy_env.calls()) == []


def test_rollback_is_prepared_before_build_and_can_be_used(deploy_env: Env) -> None:
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = deploy_env.calls()
    first_rollback_tag = next(i for i, c in enumerate(calls) if c.startswith("tag ") and "zhol-rollback/" in c)
    first_build = next(i for i, c in enumerate(calls) if " build " in c)
    first_up = next(i for i, c in enumerate(calls) if " up " in c)
    assert first_rollback_tag < first_build < first_up
    manifests = list((deploy_env.root / "data/deploy/rollback").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["services"]["api"]["image"] == OLD_APP
    assert manifest["services"]["web"]["image"] == OLD_WEB
    assert manifest["target_commit"] == _head(deploy_env)
    assert deploy_env.state["containers"]["api"]["image"] == NEW_APP

    back = deploy_env.run("rollback", "--yes")
    assert back.returncode == 0, back.stdout + back.stderr
    state = deploy_env.state
    assert state["containers"]["api"]["image"] == OLD_APP
    assert state["containers"]["web"]["image"] == OLD_WEB


def test_repeated_deployment_of_the_same_commit_changes_nothing(deploy_env: Env) -> None:
    first = deploy_env.run("deploy", "--yes")
    assert first.returncode == 0, first.stdout + first.stderr
    count = len(deploy_env.calls())
    second = deploy_env.run("deploy", "--yes")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "уже развёрнут" in second.stdout
    assert _changing(deploy_env.calls()[count:]) == []
    assert len(list((deploy_env.root / "data/deploy/rollback").glob("*.json"))) == 1


# ---------- further guarantees ----------


def test_every_compose_call_uses_both_files_and_nothing_destructive(deploy_env: Env) -> None:
    deploy_env.update(unhealthy_images=[NEW_APP])
    deploy_env.run("deploy", "--yes")
    calls = deploy_env.calls()
    compose = [c for c in calls if c.startswith("compose ") and "compose version" not in c]
    assert compose
    for line in compose:
        assert "docker-compose.yml" in line and "docker-compose.offline.yml" in line
    for line in calls:
        for banned in (" down", "rmi", "prune", "volume rm", "alembic upgrade", "alembic downgrade",
                       "alembic stamp", " push", " pull"):
            assert banned not in " " + line, line
        if " up " in line:
            assert " db" not in line.split(" up ", 1)[1] and "collector" not in line
    text = DEPLOY_PY.read_text(encoding="utf-8")
    for banned in ("alembic upgrade", '"down"', '"rmi"', "prune", '"push"', "method=", "Request("):
        assert banned not in text


def test_secrets_never_reach_output_or_log(deploy_env: Env) -> None:
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 0, result.stderr
    everything = result.stdout + result.stderr + deploy_env.logs()
    for value in (SECRET, GATE, ADMIN):
        assert value not in everything
    assert "POSTGRES_PASSWORD: задано" in result.stdout


def test_check_is_read_only(deploy_env: Env) -> None:
    result = deploy_env.run("check")
    assert result.returncode == 0, result.stderr
    assert "Ничего не собиралось" in result.stdout
    assert _changing(deploy_env.calls()) == []
    assert not (deploy_env.root / "data/deploy/state.json").exists()


def test_uncommitted_build_input_is_refused(deploy_env: Env) -> None:
    (deploy_env.root / "zhol/new_module.py").write_text("x = 1\n")
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "zhol/new_module.py" in result.stderr
    assert _changing(deploy_env.calls()) == []


def test_unexpected_commit_is_refused(deploy_env: Env) -> None:
    result = deploy_env.run("deploy", "--yes", "--expect-commit", "8962fbceedd8")
    assert result.returncode == 1
    assert "не совпадает с разрешённым" in result.stderr
    assert _changing(deploy_env.calls()) == []


def test_low_disk_space_stops_before_build(deploy_env: Env) -> None:
    deploy_env.update(df_avail=1000)
    result = deploy_env.run("deploy", "--yes", ZHOL_DEPLOY_MIN_FREE_GB="1")
    assert result.returncode == 1
    assert "В диске Docker свободно" in result.stderr
    assert _changing(deploy_env.calls()) == []


def test_schema_mismatch_stops_without_migrating(deploy_env: Env) -> None:
    deploy_env.update(schema_exit=3)
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "Обновить базу Zhol.command" in result.stderr
    calls = deploy_env.calls()
    assert not [c for c in calls if " up " in c or "alembic upgrade" in c]
    assert deploy_env.state["images"]["proj-api"] == OLD_APP


def test_incompatible_worker_code_stops_before_replacing(deploy_env: Env) -> None:
    deploy_env.update(probe_overrides={"worker": {"code": "code-2"}})
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "код worker отличается от кода api" in result.stderr
    assert not [c for c in deploy_env.calls() if " up " in c]


def test_database_revision_must_match_new_code(deploy_env: Env) -> None:
    deploy_env.update(probe_overrides={s: {"heads": ["ffff0000aaaa"]} for s in ("api", "worker", "browser-worker")})
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 1
    assert "база на ревизии b6d2f4a81c30" in result.stderr
    assert not [c for c in deploy_env.calls() if " up " in c]


def test_running_collector_is_stopped_and_restored(deploy_env: Env) -> None:
    _install_collector(deploy_env, running=True)
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = deploy_env.calls()
    stop = next(i for i, c in enumerate(calls) if c.startswith("launchctl bootout"))
    up = next(i for i, c in enumerate(calls) if " up " in c)
    start = next(i for i, c in enumerate(calls) if c.startswith(("launchctl bootstrap", "launchctl kickstart")))
    assert stop < up < start
    assert deploy_env.state["collector"] == "running"
    assert "pip install -e" in result.stdout


def test_stopped_collector_stays_stopped(deploy_env: Env) -> None:
    _install_collector(deploy_env, running=False)
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [c for c in deploy_env.calls() if c.startswith(("launchctl bootout", "launchctl bootstrap", "launchctl kickstart"))]
    assert deploy_env.state["collector"] == "stopped"


def test_collector_stays_stopped_after_automatic_rollback(deploy_env: Env) -> None:
    _install_collector(deploy_env, running=True)
    deploy_env.update(unhealthy_images=[NEW_APP])
    result = deploy_env.run("deploy", "--yes")
    assert result.returncode == 3
    assert "Сборщик macOS оставлен остановленным" in result.stderr
    assert deploy_env.state["collector"] == "stopped"


def test_service_started_without_override_is_recreated(deploy_env: Env) -> None:
    first = deploy_env.run("deploy", "--yes")
    assert first.returncode == 0
    state = deploy_env.state
    state["containers"]["api"]["files"] = f"{deploy_env.root}/docker-compose.yml"
    deploy_env.state_path.write_text(json.dumps(state))
    count = len(deploy_env.calls())
    second = deploy_env.run("deploy", "--yes")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "запущена без docker-compose.offline.yml" in second.stdout
    ups = [c for c in deploy_env.calls()[count:] if " up " in c]
    assert len(ups) == 1 and ups[0].endswith(" api")


def test_wrapper_syntax() -> None:
    subprocess.run(["/bin/bash", "-n", str(DEPLOY_SH)], check=True)


# ---------- pure helpers ----------


def _module():
    spec = importlib.util.spec_from_file_location("zhol_deploy", DEPLOY_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_frontend_routes_match_the_real_api() -> None:
    from zhol.api.main import app

    deploy = _module()
    assert deploy.missing_frontend_routes(ROOT / "web" / "src", list(app.openapi()["paths"])) == []
    assert deploy.missing_frontend_routes(ROOT / "web" / "src", ["/health"]) != []


def test_real_compose_files_pass_the_effective_config_check() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    env = dict(os.environ, POSTGRES_USER="u", POSTGRES_PASSWORD="p", POSTGRES_DB="d", ZHOL_LOCAL_GATE="x" * 64)
    env.pop("COMPOSE_FILE", None)
    cmd = ["docker", "compose", "--project-name", "zhol-deploy-test", "--env-file", os.devnull,
           "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.offline.yml"),
           "--profile", "backup", "config", "--format", "json"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("docker compose config is not available here")
    deploy = _module()
    assert deploy.validate_effective_config(json.loads(result.stdout)) == []
    base_only = cmd[:]
    i = base_only.index(str(ROOT / "docker-compose.offline.yml"))
    del base_only[i - 1 : i + 1]
    result = subprocess.run(base_only, env=env, capture_output=True, text=True, check=False)
    problems = deploy.validate_effective_config(json.loads(result.stdout))
    assert any("ZHOL_CONFIG_DIR" in p for p in problems)


def test_probe_reports_code_and_heads() -> None:
    from zhol import deploy_probe

    report_heads = deploy_probe.alembic_heads()
    assert report_heads
    assert len(deploy_probe.code_digest()) == 64


def _head(env: Env) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.root, capture_output=True, text=True,
                          check=True).stdout.strip()
