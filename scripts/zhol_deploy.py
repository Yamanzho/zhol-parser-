#!/usr/bin/env python3
"""Single deployment entry for Zhol on macOS. Started by scripts/zhol-deploy-macos.sh.

Commands:
  check     read-only preflight and plan; nothing is built, stopped or restarted
  deploy    preflight, build, probe new images, replace only changed services, verify
  rollback  return the services to the images saved by the last deployment

Invariants enforced here:
  - Compose always gets docker-compose.yml and docker-compose.offline.yml; COMPOSE_FILE is ignored.
  - Database migrations are never run. A schema mismatch stops the deployment.
  - The database container, its volume and data/backups are never touched.
  - No job is created and the Docker collector profile is never started.
  - Images are never removed. Nothing is pushed or fetched from git.
  - Secret values from .env are never printed or written to the log.

Standard library only: macOS /usr/bin/python3 is 3.9.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.offline.yml")
OVERRIDE = COMPOSE_FILES[1]
OFFLINE_CONFIG = "tests/golden/review-engine/config"

APP_SERVICES = ("api", "worker", "browser-worker", "scheduler")
WEB_SERVICES = ("web",)
REQUIRED_SERVICES = ("api", "web", "worker", "browser-worker")
OPTIONAL_SERVICES = ("scheduler", "backup")
GROUPS = {"app": APP_SERVICES, "web": WEB_SERVICES}
GROUP_INPUTS = {
    "app": ["Dockerfile", ".dockerignore", "pyproject.toml", "zhol", "alembic.ini", "alembic", "config", "prompts", "council"],
    "web": ["web/Dockerfile", "web/.dockerignore", "web/package.json", "web/package-lock.json", "web/index.html",
            "web/tsconfig.json", "web/vite.config.ts", "web/src"],
}
RUNTIME_INPUTS = list(COMPOSE_FILES) + [OFFLINE_CONFIG, "tests/fixtures"]

REQUIRED_ENV = (
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "ZHOL_LOCAL_GATE", "ZHOL_ADMIN_TOKEN",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
)
GRANT_KEYS = ("ZHOL_2GIS_BROWSER_GRANT", "ZHOL_YANDEX_BROWSER_GRANT", "ZHOL_GOOGLE_BROWSER_GRANT")
GRANT_VALUES = ("written-browser-retention-local", "written-browser-retention-commercial")
SECRET_MARKERS = ("PASSWORD", "TOKEN", "SECRET", "KEY", "GATE", "DATABASE_URL")

COLLECTOR_LABEL = "kz.duna.zhol.collector"
CONFIRM_WORD = "РАЗВЕРНУТЬ"

EXIT_OK = 0
EXIT_PREFLIGHT = 1
EXIT_USAGE = 2
EXIT_ROLLED_BACK = 3
EXIT_NEEDS_HUMAN = 4

ACTIVE_JOBS_SQL = (
    "SELECT 'running_jobs', count(*) FROM jobs WHERE status = 'running' "
    "UNION ALL SELECT 'pending_collection_jobs', count(*) FROM jobs "
    "WHERE queue IN ('collector', 'browser') AND status IN ('queued', 'failed') "
    "UNION ALL SELECT 'running_batches', count(*) FROM collection_batches WHERE status = 'running' "
    "UNION ALL SELECT 'items_in_progress', count(*) FROM collection_batch_items WHERE status = 'in_progress'"
)
REVISION_SQL = "SELECT coalesce((SELECT string_agg(version_num, ',' ORDER BY version_num) FROM alembic_version), '')"


class DeployError(Exception):
    def __init__(self, message: str, code: int = EXIT_PREFLIGHT) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Log:
    def __init__(self) -> None:
        folder = Path(os.environ.get("ZHOL_DEPLOY_LOG_DIR") or Path.home() / "Library" / "Logs" / "Zhol")
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        self.path = folder / f"deploy-{stamp}-{os.getpid()}.log"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._file = os.fdopen(fd, "a", encoding="utf-8")
        self.secrets: List[str] = []

    def redact(self, text: str) -> str:
        for value in sorted(self.secrets, key=len, reverse=True):
            if value:
                text = text.replace(value, "***")
        return text

    def _write(self, level: str, text: str) -> None:
        line = f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {level} {self.redact(text)}"
        self._file.write(line + "\n")
        self._file.flush()

    def info(self, text: str) -> None:
        self._write("INFO", text)
        print(self.redact(text), flush=True)

    def warn(self, text: str) -> None:
        self._write("WARN", text)
        print("Внимание: " + self.redact(text), flush=True)

    def error(self, text: str) -> None:
        self._write("ERROR", text)
        print("Ошибка: " + self.redact(text), file=sys.stderr, flush=True)

    def detail(self, text: str) -> None:
        self._write("DEBUG", text)


class Deployer:
    def __init__(self, args: argparse.Namespace, log: Log) -> None:
        self.args = args
        self.log = log
        self.project = os.environ.get("ZHOL_DEPLOY_PROJECT") or ROOT.name
        self.state_dir = ROOT / "data" / "deploy"
        self.env: Dict[str, str] = {}
        self.commit = ""
        self.containers: Dict[str, Dict[str, Any]] = {}
        self.expected_hash: Dict[str, str] = {}
        self.deploy_services: List[str] = []
        self.fingerprints: Dict[str, str] = {}
        self.state: Dict[str, Any] = {}
        self.collector_was_running = False
        self.collector_stopped = False
        self.version = ""
        self.db_revision = ""
        self.health_timeout = _float_env("ZHOL_DEPLOY_HEALTH_TIMEOUT", 120)
        self.settle = _float_env("ZHOL_DEPLOY_SETTLE_SECONDS", 10)
        self.min_free_gb = _float_env("ZHOL_DEPLOY_MIN_FREE_GB", 5)

    # ---------- process helpers ----------

    def _proc_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        for key in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_PATH_SEPARATOR", "COMPOSE_PROJECT_NAME"):
            env.pop(key, None)
        env["COMPOSE_PROJECT_NAME"] = self.project
        return env

    def run(self, cmd: Sequence[str], *, quiet_output: bool = False, timeout: Optional[float] = None,
            stream: bool = False) -> subprocess.CompletedProcess:
        self.log.detail("$ " + " ".join(cmd))
        try:
            if stream:
                proc = subprocess.Popen(list(cmd), cwd=ROOT, env=self._proc_env(), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                lines: List[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line)
                    self.log.detail(line.rstrip("\n"))
                proc.wait()
                return subprocess.CompletedProcess(list(cmd), proc.returncode, "".join(lines), "")
            result = subprocess.run(list(cmd), cwd=ROOT, env=self._proc_env(), capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except FileNotFoundError:
            return subprocess.CompletedProcess(list(cmd), 127, "", f"{cmd[0]}: not found")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(list(cmd), 124, "", "timeout")
        self.log.detail(f"exit={result.returncode}")
        if not quiet_output:
            for stream_text in (result.stdout, result.stderr):
                if stream_text.strip():
                    self.log.detail(stream_text.strip()[-4000:])
        return result

    def compose(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        base = ["docker", "compose", "--project-name", self.project, "--project-directory", str(ROOT)]
        for name in COMPOSE_FILES:
            base += ["-f", str(ROOT / name)]
        return self.run(base + list(args), **kwargs)

    # ---------- preflight ----------

    def check_tools(self) -> None:
        if shutil.which("docker") is None:
            raise DeployError("Docker не установлен. Службы не перезапускались.")
        if self.run(["docker", "info"], quiet_output=True, timeout=60).returncode != 0:
            raise DeployError("Docker Desktop не запущен или не отвечает. Службы не перезапускались.")
        if self.run(["docker", "compose", "version"], timeout=30).returncode != 0:
            raise DeployError("Нет команды docker compose. Службы не перезапускались.")
        if os.environ.get("COMPOSE_FILE"):
            self.log.warn("переменная COMPOSE_FILE задана в окружении и проигнорирована: используются только оба файла проекта.")

    def check_files(self) -> None:
        for name in COMPOSE_FILES:
            if not (ROOT / name).is_file():
                raise DeployError(
                    f"Нет файла {name} в {ROOT}. Без {OVERRIDE} API не стартует (нет ZHOL_CONFIG_DIR и локального входа), "
                    "так уже был простой API около 2,5 минуты. Развёртывание остановлено, службы не перезапускались."
                )
        if not (ROOT / OFFLINE_CONFIG / "models.yaml").is_file():
            raise DeployError(f"Нет {OFFLINE_CONFIG}/models.yaml, который подключает {OVERRIDE}. Службы не перезапускались.")
        for extra in ("docker-compose.override.yml", "compose.override.yml"):
            if (ROOT / extra).exists():
                self.log.warn(f"файл {extra} найден и не используется: скрипт передаёт Compose только два файла проекта.")
        self.log.info(f"Compose: {COMPOSE_FILES[0]} + {COMPOSE_FILES[1]}, проект {self.project}.")

    def check_git(self) -> None:
        head = self.run(["git", "rev-parse", "HEAD"])
        if head.returncode != 0:
            raise DeployError("Каталог не является git-репозиторием или в нём нет коммитов. Службы не перезапускались.")
        self.commit = head.stdout.strip()
        expect = (self.args.expect_commit or "").strip()
        if expect and not self.commit.startswith(expect):
            raise DeployError(
                f"Текущий коммит {self.commit[:12]} не совпадает с разрешённым {expect[:12]}. "
                "Скрипт не переключает коммиты сам. Службы не перезапускались."
            )
        inputs = sorted({p for paths in GROUP_INPUTS.values() for p in paths} | set(RUNTIME_INPUTS))
        dirty = self.run(["git", "status", "--porcelain", "--untracked-files=all", "--", *inputs])
        if dirty.returncode != 0:
            raise DeployError("Не удалось прочитать состояние git. Службы не перезапускались.")
        changed = [line[3:] for line in dirty.stdout.splitlines() if line.strip()]
        if changed:
            shown = ", ".join(changed[:8]) + (" и ещё " + str(len(changed) - 8) if len(changed) > 8 else "")
            raise DeployError(
                "В файлах, из которых собираются образы, есть незакоммиченные изменения: " + shown + ". "
                "Сборка из такого каталога невоспроизводима. Закоммитьте или уберите изменения. Службы не перезапускались."
            )
        other = self.run(["git", "status", "--porcelain"])
        if other.stdout.strip():
            self.log.warn("в каталоге есть незакоммиченные файлы вне сборки (документы или тесты). На образы они не влияют.")
        for group, paths in GROUP_INPUTS.items():
            tree = self.run(["git", "ls-tree", "-r", self.commit, "--", *paths], quiet_output=True)
            self.fingerprints[group] = hashlib.sha256(tree.stdout.encode()).hexdigest()
        self.log.info(f"Коммит для развёртывания: {self.commit}.")

    def check_env(self) -> None:
        path = ROOT / ".env"
        if not path.is_file():
            raise DeployError("Файла .env нет. Развёртывание остановлено, службы не перезапускались.")
        if path.stat().st_mode & 0o077:
            self.log.warn(".env доступен другим пользователям. Рекомендуется chmod 600 .env.")
        env: Dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
        self.env = env
        # Short values such as "offline" would mask ordinary words; secrets are never printed in the first place.
        self.log.secrets = [v for k, v in env.items() if any(m in k for m in SECRET_MARKERS) and len(v) >= 8]
        missing = [k for k in REQUIRED_ENV if not env.get(k)]
        problems = ["не задана " + k for k in missing]
        if env.get("ZHOL_LOCAL_GATE") and len(env["ZHOL_LOCAL_GATE"]) < 32:
            problems.append("ZHOL_LOCAL_GATE короче 32 символов")
        for key in GRANT_KEYS:
            if env.get(key) and env[key] not in GRANT_VALUES:
                problems.append(f"{key} задан не одним из двух допустимых текстов")
        for key in ("API_PORT", "WEB_PORT"):
            if env.get(key) and not env[key].isdigit():
                problems.append(f"{key} не число")
        if problems:
            raise DeployError(
                "Не хватает обязательных настроек в .env: " + "; ".join(problems) + ". Значения не выводятся. "
                "Развёртывание остановлено, службы не перезапускались."
            )
        shown = ", ".join(f"{k}: задано" for k in REQUIRED_ENV)
        self.log.info(f"Настройки .env: {shown}.")

    def port(self, key: str, default: str) -> str:
        value = self.env.get(key, "")
        return value if value.isdigit() else default

    def check_compose_config(self) -> None:
        quiet = self.compose("--profile", "backup", "config", "--quiet")
        if quiet.returncode != 0:
            reason = (quiet.stderr or quiet.stdout).strip().splitlines()[-1:] or ["неизвестная ошибка"]
            raise DeployError("Итоговая конфигурация Compose не собирается: " + reason[0] + ". Службы не перезапускались.")
        result = self.compose("--profile", "backup", "config", "--format", "json", quiet_output=True)
        try:
            config = json.loads(result.stdout)
        except ValueError:
            raise DeployError("Compose не выдал конфигурацию в JSON. Службы не перезапускались.")
        problems = validate_effective_config(config)
        if problems:
            raise DeployError(
                "Итоговая конфигурация Compose неполная: " + "; ".join(problems) + f". Проверьте {OVERRIDE}. "
                "Службы не перезапускались."
            )
        hashes = self.compose("--profile", "backup", "config", "--hash", "*")
        if hashes.returncode != 0:
            raise DeployError("Не удалось получить хеши конфигурации служб. Службы не перезапускались.")
        for line in hashes.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                self.expected_hash[parts[0]] = parts[1]
        self.log.info("Итоговая конфигурация Compose проверена: override подключён, порты только на 127.0.0.1.")

    def inspect_containers(self) -> Dict[str, Dict[str, Any]]:
        ids = self.compose("--profile", "backup", "--profile", "collector", "ps", "-a", "-q")
        found: Dict[str, Dict[str, Any]] = {}
        id_list = [line.strip() for line in ids.stdout.splitlines() if line.strip()]
        if ids.returncode != 0 or not id_list:
            return found
        raw = self.run(["docker", "inspect", *id_list], quiet_output=True)
        try:
            items = json.loads(raw.stdout or "[]")
        except ValueError:
            items = []
        for item in items:
            labels = (item.get("Config") or {}).get("Labels") or {}
            service = labels.get("com.docker.compose.service", "")
            if not service or labels.get("com.docker.compose.oneoff") == "True":
                continue
            state = item.get("State") or {}
            found[service] = {
                "id": item.get("Id", ""),
                "image": item.get("Image", ""),
                "running": state.get("Status") == "running",
                "health": ((state.get("Health") or {}).get("Status")) or "",
                "restarts": int(item.get("RestartCount") or 0),
                "hash": labels.get("com.docker.compose.config-hash", ""),
                "files": labels.get("com.docker.compose.project.config_files", ""),
            }
        return found

    def check_containers(self) -> None:
        self.containers = self.inspect_containers()
        db = self.containers.get("db")
        if not db or not db["running"]:
            raise DeployError("Контейнер базы не запущен. Скрипт базу не запускает и не пересоздаёт. Службы не перезапускались.")
        if db["health"] and db["health"] != "healthy":
            raise DeployError(f"PostgreSQL в состоянии «{db['health']}». Развёртывание остановлено, службы не перезапускались.")
        if self.containers.get("collector", {}).get("running"):
            raise DeployError("Запущен сборщик Docker (профиль collector). Он должен быть выключен. Службы не перезапускались.")
        running = [s for s, c in self.containers.items() if c["running"]]
        self.deploy_services = list(REQUIRED_SERVICES) + [s for s in OPTIONAL_SERVICES if s in running]
        for service in self.deploy_services:
            info = self.containers.get(service)
            if info and info["running"] and OVERRIDE not in info["files"]:
                self.log.warn(f"служба {service} сейчас запущена без {OVERRIDE}. Она будет пересоздана с обоими файлами.")
        self.log.info("Работают службы: " + (", ".join(sorted(running)) or "нет") + ".")

    def psql(self, sql: str) -> subprocess.CompletedProcess:
        return self.compose("exec", "-T", "db", "psql", "-U", self.env["POSTGRES_USER"], "-d", self.env["POSTGRES_DB"],
                            "-XAtc", sql, timeout=60)

    def check_database(self) -> None:
        ready = self.compose("exec", "-T", "db", "pg_isready", "-U", self.env["POSTGRES_USER"], "-d",
                             self.env["POSTGRES_DB"], timeout=30)
        if ready.returncode != 0:
            raise DeployError("PostgreSQL не принимает подключения (pg_isready). Службы не перезапускались.")
        revision = self.psql(REVISION_SQL)
        if revision.returncode != 0:
            raise DeployError("Не удалось прочитать ревизию Alembic из базы. Службы не перезапускались.")
        current = re.sub(r"[^a-z0-9,]", "", revision.stdout.strip())
        if not current:
            raise DeployError("В базе нет отметки ревизии Alembic. Нужна проверка человеком. Службы не перезапускались.")
        self.db_revision = current
        self.log.info(f"PostgreSQL отвечает, ревизия Alembic: {current}.")
        self.check_active_jobs()

    def check_active_jobs(self) -> None:
        result = self.psql(ACTIVE_JOBS_SQL)
        if result.returncode != 0:
            raise DeployError("Не удалось проверить активные задачи сбора. Службы не перезапускались.")
        counts = parse_counts(result.stdout)
        labels = {
            "running_jobs": "задач в работе",
            "pending_collection_jobs": "задач сбора в очереди",
            "running_batches": "пакетов сбора в статусе running",
            "items_in_progress": "компаний собирается сейчас",
        }
        active = {k: v for k, v in counts.items() if v > 0}
        if active:
            shown = ", ".join(f"{labels.get(k, k)}: {v}" for k, v in active.items())
            raise DeployError(
                "Идёт сбор или есть задачи в очереди (" + shown + "). Дождитесь окончания или поставьте пакет на паузу "
                "в панели. Скрипт сам задачи не останавливает и не запускает. Службы не перезапускались."
            )
        self.log.info("Активных задач сбора нет.")

    def check_disk(self) -> None:
        need = self.min_free_gb * 1024 * 1024
        host_free = shutil.disk_usage(ROOT).free / 1024
        if host_free < need:
            raise DeployError(
                f"На диске Mac свободно {host_free / 1024 / 1024:.1f} ГБ, нужно не меньше {self.min_free_gb:.0f} ГБ. "
                "Скрипт ничего не удаляет сам. Службы не перезапускались."
            )
        df = self.compose("exec", "-T", "db", "df", "-Pk", "/var/lib/postgresql/data", timeout=30)
        vm_free = parse_df_available(df.stdout)
        if vm_free is None:
            raise DeployError("Не удалось узнать свободное место в Docker. Службы не перезапускались.")
        if vm_free < need:
            raise DeployError(
                f"В диске Docker свободно {vm_free / 1024 / 1024:.1f} ГБ, нужно не меньше {self.min_free_gb:.0f} ГБ. "
                "Образы и данные не удалялись. Службы не перезапускались."
            )
        self.log.info(f"Свободно: Mac {host_free / 1024 / 1024:.1f} ГБ, Docker {vm_free / 1024 / 1024:.1f} ГБ.")

    # ---------- macOS collector ----------

    def collector_plist(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{COLLECTOR_LABEL}.plist"

    def collector_ours(self) -> bool:
        plist = self.collector_plist()
        if not plist.is_file():
            return False
        return f"{ROOT}/scripts/zhol-collector-macos.sh" in plist.read_text(encoding="utf-8", errors="replace")

    def collector_running(self) -> bool:
        result = self.run(["launchctl", "print", f"gui/{os.getuid()}/{COLLECTOR_LABEL}"], quiet_output=True)
        return result.returncode == 0 and re.search(r"^\s*state = running", result.stdout, re.M) is not None

    def check_collector(self) -> None:
        if not self.collector_plist().is_file():
            self.log.info("Сборщик macOS не установлен.")
            return
        if not self.collector_ours():
            self.log.warn("сборщик macOS установлен из другого каталога. Скрипт его не трогает.")
            return
        self.collector_was_running = self.collector_running()
        self.log.info("Сборщик macOS: " + ("работает" if self.collector_was_running else "остановлен") + ".")
        if self.collector_was_running:
            grant = self.env.get("ZHOL_2GIS_BROWSER_GRANT", "")
            if grant not in GRANT_VALUES:
                raise DeployError(
                    "Сборщик macOS работает, но в .env нет допустимого ZHOL_2GIS_BROWSER_GRANT: после перезапуска "
                    "он бы не стартовал. Службы не перезапускались."
                )

    def collector_script(self, command: str) -> bool:
        result = self.run(["/bin/bash", str(ROOT / "scripts" / "zhol-collector-macos.sh"), command], timeout=90)
        return result.returncode == 0

    def stop_collector(self) -> None:
        self.log.info("Останавливаем сборщик macOS на время замены служб.")
        if not self.collector_script("stop"):
            raise DeployError("Не удалось остановить сборщик macOS. Службы не перезапускались.")
        self.collector_stopped = True

    def restore_collector(self) -> None:
        if not self.collector_stopped:
            return
        if self.collector_script("start"):
            self.log.info("Сборщик macOS снова запущен, как был до развёртывания.")
        else:
            self.log.error("сборщик macOS не запустился. Запустите вручную: scripts/zhol-collector-macos.sh start")
        self.collector_stopped = False

    # ---------- state, images ----------

    def image_name(self, service: str) -> str:
        return f"{self.project}-{service}"

    def image_id(self, ref: str) -> str:
        result = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], quiet_output=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    def load_state(self) -> None:
        path = self.state_dir / "state.json"
        if path.is_file():
            try:
                self.state = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                self.log.warn("файл data/deploy/state.json повреждён; считаем, что прошлого развёртывания этим скриптом не было.")
                self.state = {}

    def write_json(self, path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def builds_needed(self) -> List[str]:
        groups = []
        for group, services in GROUPS.items():
            wanted = [s for s in services if s in self.deploy_services]
            if not wanted:
                continue
            reasons = []
            if not self.state:
                reasons.append("нет записи о прошлом развёртывании этим скриптом")
            elif (self.state.get("fingerprints") or {}).get(group) != self.fingerprints[group]:
                reasons.append("изменились файлы сборки")
            for service in wanted:
                current = self.image_id(self.image_name(service))
                if not current:
                    reasons.append(f"нет образа {service}")
                elif self.state and (self.state.get("images") or {}).get(service) != current:
                    reasons.append(f"образ {service} собран не этим скриптом")
            if reasons:
                groups.append(group)
                self.log.info(f"Сборка {group} ({', '.join(wanted)}): нужна, " + "; ".join(sorted(set(reasons))) + ".")
            else:
                self.log.info(f"Сборка {group} ({', '.join(wanted)}): не нужна, файлы и образы не менялись.")
        return groups

    def recreate_needed(self) -> List[str]:
        services = []
        for service in self.deploy_services:
            info = self.containers.get(service)
            reasons = []
            if not info or not info["running"]:
                reasons.append("не запущена")
            else:
                if service in APP_SERVICES + WEB_SERVICES:
                    target = self.image_id(self.image_name(service))
                    if target and info["image"] != target:
                        reasons.append("новый образ")
                if self.expected_hash.get(service) and info["hash"] != self.expected_hash[service]:
                    reasons.append("изменилась конфигурация")
                if OVERRIDE not in info["files"]:
                    reasons.append(f"запущена без {OVERRIDE}")
            if reasons:
                services.append(service)
                self.log.info(f"Служба {service}: будет пересоздана ({', '.join(reasons)}).")
        return services

    def prepare_rollback(self, stamp: str) -> Path:
        services: Dict[str, Any] = {}
        for service in self.deploy_services:
            info = self.containers.get(service)
            if not info or not info["running"] or service not in APP_SERVICES + WEB_SERVICES:
                continue
            tag = f"zhol-rollback/{self.image_name(service)}:{stamp}"
            if self.run(["docker", "tag", info["image"], tag]).returncode != 0:
                raise DeployError(f"Не удалось сохранить текущий образ {service} для отката. Службы не перезапускались.")
            services[service] = {"image": info["image"], "tag": tag}
        manifest = {
            "stamp": stamp,
            "previous_commit": self.state.get("commit", "unknown"),
            "target_commit": self.commit,
            "project": self.project,
            "services": services,
        }
        path = self.state_dir / "rollback" / f"{stamp}.json"
        self.write_json(path, manifest)
        self.log.info(f"Текущие образы сохранены для отката: {', '.join(sorted(services)) or 'нет'}. Запись: {path}.")
        return path

    def build(self, groups: List[str]) -> None:
        services = [s for g in groups for s in GROUPS[g] if s in self.deploy_services]
        self.log.info("Собираем образы, работающие службы не трогаем: " + ", ".join(services) + ".")
        result = self.compose("build", *services, stream=True)
        if result.returncode != 0:
            tail = [line for line in result.stdout.splitlines() if line.strip()][-5:]
            for line in tail:
                self.log.error(line)
            raise DeployError(
                "Сборка образов не удалась. Работающие службы не останавливались и не перезапускались. "
                f"Подробности в журнале: {self.log.path}"
            )
        for service in services:
            image = self.image_id(self.image_name(service))
            if image:
                self.run(["docker", "tag", image, f"zhol-release/{self.image_name(service)}:{self.commit[:12]}"])

    # ---------- compatibility ----------

    def probe(self, service: str, startup: bool) -> Dict[str, Any]:
        cmd = ["run", "--rm", "--no-deps", "-T", service, "python", "-m", "zhol.deploy_probe"]
        if startup:
            cmd.append("--startup")
        result = self.compose(*cmd, timeout=300)
        report = last_json_line(result.stdout)
        if result.returncode != 0 or report is None:
            reason = (report or {}).get("startup_error") or "проба не выполнилась"
            raise DeployError(
                f"Новый образ {service} не проходит проверку запуска: {reason}. Работающие службы не перезапускались."
            )
        return report

    def check_compatibility(self) -> List[str]:
        expected_version = pyproject_version(ROOT / "pyproject.toml")
        web_version = package_version(ROOT / "web" / "package.json")
        reports: Dict[str, Dict[str, Any]] = {}
        for service in [s for s in APP_SERVICES if s in self.deploy_services]:
            reports[service] = self.probe(service, startup=(service == "api"))
        problems = []
        api = reports["api"]
        for service, report in reports.items():
            if report.get("code") != api.get("code"):
                problems.append(f"код {service} отличается от кода api")
            if report.get("heads") != api.get("heads"):
                problems.append(f"ревизия миграций {service} отличается от api")
            if report.get("dist_version") != expected_version:
                problems.append(f"версия {service} {report.get('dist_version')} вместо {expected_version}")
        if api.get("app_version") != expected_version:
            problems.append(f"версия приложения API {api.get('app_version')} вместо {expected_version}")
        if web_version != expected_version:
            problems.append(f"версия панели {web_version} вместо {expected_version}")
        heads = ",".join(api.get("heads") or [])
        if heads != self.db_revision:
            problems.append(f"база на ревизии {self.db_revision}, новой программе нужна {heads}")
        web_image = self.compose("run", "--rm", "--no-deps", "-T", "web", "node", "-p",
                                 "require('./package.json').version", timeout=120)
        image_web_version = (web_image.stdout.strip().splitlines() or [""])[-1].strip()
        if web_image.returncode != 0 or image_web_version != web_version:
            problems.append(f"образ панели содержит версию {image_web_version or 'неизвестно'} вместо {web_version}")
        if self.collector_was_running:
            host = self.run([str(ROOT / ".venv" / "bin" / "python"), "-m", "zhol.deploy_probe"], timeout=120)
            host_report = last_json_line(host.stdout)
            if host.returncode != 0 or host_report is None:
                problems.append("проба сборщика macOS (.venv) не выполнилась")
            else:
                if host_report.get("code") != api.get("code"):
                    problems.append("код сборщика macOS отличается от кода api")
                if host_report.get("heads") != api.get("heads"):
                    problems.append("ревизия миграций сборщика macOS отличается от api")
                if host_report.get("dist_version") != expected_version:
                    self.log.warn(
                        f"метаданные .venv говорят {host_report.get('dist_version')}, код совпадает. "
                        "Обновите их: .venv/bin/python -m pip install -e ."
                    )
        missing = missing_frontend_routes(ROOT / "web" / "src", api.get("paths") or [])
        if missing:
            problems.append("панель вызывает пути, которых нет в новом API: " + ", ".join(missing[:6]))
        if problems:
            raise DeployError(
                "Версии служб несовместимы: " + "; ".join(problems) + ". Миграции не запускались, "
                "работающие службы не перезапускались."
            )
        self.version = expected_version
        self.log.info(f"Совместимость подтверждена: api, воркеры, панель и сборщик на {expected_version}, ревизия {heads}.")
        return list(api.get("paths") or [])

    def check_schema(self) -> None:
        result = self.compose("run", "--rm", "--no-deps", "-T", "api", "python", "-m", "zhol.cli", "schema-check",
                              timeout=300)
        messages = {
            0: "",
            3: "база старее программы. Обновите её отдельно: «Обновить базу Zhol.command». Этот скрипт миграции не запускает",
            4: "база пустая. Развёртывание на пустую базу не выполняется",
            5: "ревизия базы неизвестна программе, нужна проверка человеком",
        }
        if result.returncode != 0:
            reason = messages.get(result.returncode, "схему базы не удалось прочитать")
            raise DeployError(f"Проверка схемы новым образом: {reason}. Работающие службы не перезапускались.")
        self.log.info("Схема базы совпадает с новой программой.")

    # ---------- replace and verify ----------

    def up(self, services: List[str]) -> bool:
        profiles = ["--profile", "backup"] if "backup" in services else []
        result = self.compose(*profiles, "up", "-d", "--no-build", "--no-deps", *services, stream=True)
        return result.returncode == 0

    def http(self, url: str) -> tuple:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except (urllib.error.URLError, OSError, ValueError):
            return 0, ""

    def wait_health(self, url: str) -> bool:
        deadline = time.monotonic() + self.health_timeout
        while True:
            status, body = self.http(url)
            if status == 200:
                try:
                    if json.loads(body).get("status") == "ok":
                        return True
                except ValueError:
                    pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)

    def verify(self, services: List[str], api_paths: Optional[List[str]]) -> List[str]:
        api = f"http://127.0.0.1:{self.port('API_PORT', '18080')}"
        web = f"http://127.0.0.1:{self.port('WEB_PORT', '15173')}"
        problems = []
        if not self.wait_health(api + "/health"):
            problems.append("API не ответил на /health")
        before = self.inspect_containers()
        if self.settle > 0:
            time.sleep(self.settle)
        after = self.inspect_containers()
        for service in services:
            info = after.get(service)
            if not info or not info["running"]:
                problems.append(f"служба {service} не работает")
            elif info["health"] == "unhealthy":
                problems.append(f"служба {service} unhealthy")
            elif before.get(service) and info["restarts"] > before[service]["restarts"]:
                problems.append(f"служба {service} перезапускается")
        status, body = self.http(web + "/")
        if status != 200 or 'id="root"' not in body:
            problems.append("панель не отдаёт главную страницу")
        if not self.wait_health(web + "/health"):
            problems.append("панель не достаёт до API через прокси")
        if api_paths is not None:
            status, body = self.http(api + "/openapi.json")
            try:
                spec = json.loads(body) if status == 200 else {}
            except ValueError:
                spec = {}
            live_version = (spec.get("info") or {}).get("version")
            if live_version != self.version:
                problems.append(f"API отвечает версией {live_version}, ожидалась {self.version}")
            missing = missing_frontend_routes(ROOT / "web" / "src", list((spec.get("paths") or {}).keys()))
            if missing:
                problems.append("панель вызывает пути, которых нет в работающем API: " + ", ".join(missing[:6]))
        return problems

    def restore_tags(self, manifest: Dict[str, Any]) -> None:
        """Point <project>-<service> back at the running images so a later plain start does not pick up
        half-built or rejected images. The new images keep their zhol-release tags."""
        for service, item in (manifest.get("services") or {}).items():
            if self.image_id(self.image_name(service)) != item["image"]:
                self.run(["docker", "tag", item["image"], self.image_name(service)])

    def rollback_to(self, manifest: Dict[str, Any], services: List[str]) -> bool:
        saved = manifest.get("services") or {}
        targets = [s for s in services if s in saved]
        if not targets:
            return False
        for service in targets:
            if self.run(["docker", "tag", saved[service]["image"], self.image_name(service)]).returncode != 0:
                self.log.error(f"не удалось вернуть образ {service} ({saved[service]['tag']}).")
                return False
        self.log.info("Откат: возвращаем прежние образы для " + ", ".join(targets) + ".")
        if not self.up(targets):
            return False
        self.version = ""
        problems = self.verify(targets, None)
        for problem in problems:
            self.log.error("после отката: " + problem)
        return not problems

    # ---------- commands ----------

    def preflight(self) -> None:
        self.check_tools()
        self.check_files()
        self.check_git()
        self.check_env()
        self.check_compose_config()
        self.check_containers()
        self.check_database()
        self.check_disk()
        self.check_collector()
        self.load_state()

    def cmd_check(self) -> int:
        self.preflight()
        groups = self.builds_needed()
        recreate = self.recreate_needed()
        if not groups and not recreate:
            self.log.info("Проверка пройдена. Развёртывание не требуется: всё уже соответствует коммиту.")
        else:
            self.log.info("Проверка пройдена. Сборка: " + (", ".join(groups) or "не нужна") +
                          ". Список пересоздаваемых служб уточнится после сборки.")
        self.log.info("Ничего не собиралось, не останавливалось и не перезапускалось.")
        return EXIT_OK

    def confirm(self, text: str) -> None:
        if self.args.yes:
            return
        if not sys.stdin.isatty():
            raise DeployError("Нужно подтверждение: запустите в терминале или добавьте --yes. Службы не перезапускались.")
        print(text)
        answer = input(f"Наберите {CONFIRM_WORD} и нажмите Enter: ").strip()
        if answer != CONFIRM_WORD:
            raise DeployError("Отменено. Ничего не менялось.")

    def cmd_deploy(self) -> int:
        self.preflight()
        groups = self.builds_needed()
        if not groups and not self.recreate_needed():
            if self.state.get("commit") != self.commit:
                self.save_state()
                self.log.info("Изменения коммита не затрагивают образы и настройки служб. Службы не перезапускались.")
            else:
                self.log.info(f"Коммит {self.commit[:12]} уже развёрнут, службы соответствуют ему. Ничего не менялось.")
            return EXIT_OK
        self.confirm(
            f"Будет развёрнут коммит {self.commit[:12]}. Сборка: {', '.join(groups) or 'не нужна'}. "
            "Миграции не выполняются, база и копии не трогаются."
        )
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = self.prepare_rollback(stamp)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            if groups:
                self.build(groups)
            self.check_schema()
            api_paths = self.check_compatibility()
            recreate = self.recreate_needed()
            if not recreate:
                self.save_state()
                self.log.info("Новые образы совпали с работающими. Службы не перезапускались.")
                return EXIT_OK
            self.check_active_jobs()
            if self.collector_was_running and any(s in APP_SERVICES for s in recreate):
                self.stop_collector()
                self.check_active_jobs()
        except DeployError:
            self.restore_tags(manifest)
            self.restore_collector()
            raise
        self.log.info("Заменяем службы: " + ", ".join(recreate) + ". База не перезапускается.")
        started = self.up(recreate)
        problems = [] if started else ["docker compose up завершился с ошибкой"]
        if started:
            problems = self.verify(recreate, api_paths)
        if not problems:
            self.save_state()
            self.restore_collector()
            self.log.info(f"Развёртывание {self.commit[:12]} завершено. Журнал: {self.log.path}")
            return EXIT_OK
        for problem in problems:
            self.log.error(problem)
        if self.rollback_to(manifest, recreate):
            msg = (f"Новая версия не прошла проверку после запуска. Выполнен откат на прежние образы ({manifest_path.name}), "
                   "API отвечает. Миграции не запускались, данные не менялись.")
            if self.collector_stopped:
                msg += " Сборщик macOS оставлен остановленным: код каталога новее отката. После разбора: " \
                       "scripts/zhol-collector-macos.sh start"
            raise DeployError(msg, EXIT_ROLLED_BACK)
        raise DeployError(
            "Новая версия не прошла проверку, и автоматический откат не помог. Нужен человек. "
            f"Сохранённые образы: {manifest_path}. Команда отката: scripts/zhol-deploy-macos.sh rollback. "
            "База и копии не трогались.", EXIT_NEEDS_HUMAN)

    def cmd_rollback(self) -> int:
        self.check_tools()
        self.check_files()
        self.check_env()
        self.check_compose_config()
        self.check_containers()
        self.check_database()
        self.check_collector()
        folder = self.state_dir / "rollback"
        manifests = sorted(folder.glob("*.json")) if folder.is_dir() else []
        if not manifests:
            raise DeployError("Нет сохранённых образов для отката (data/deploy/rollback). Ничего не менялось.")
        manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
        saved = manifest.get("services") or {}
        missing = [s for s, item in saved.items() if not self.image_id(item["image"])]
        if missing:
            raise DeployError("Сохранённые образы не найдены: " + ", ".join(missing) + ". Ничего не менялось.")
        self.confirm(f"Будут возвращены образы от {manifest.get('stamp')} (коммит {manifest.get('previous_commit')}).")
        current = {s: self.image_id(self.image_name(s)) for s in saved}
        for service, item in saved.items():
            self.run(["docker", "tag", item["image"], self.image_name(service)])
        try:
            self.check_schema()
        except DeployError:
            for service, image in current.items():
                if image:
                    self.run(["docker", "tag", image, self.image_name(service)])
            raise
        if self.collector_was_running:
            self.stop_collector()
        if not self.up(list(saved)):
            raise DeployError("docker compose up при откате завершился с ошибкой. Нужен человек.", EXIT_NEEDS_HUMAN)
        self.version = ""
        problems = self.verify(list(saved), None)
        if problems:
            raise DeployError("После отката: " + "; ".join(problems) + ". Нужен человек.", EXIT_NEEDS_HUMAN)
        self.log.info(f"Откат выполнен на образы {manifests[-1].name}. Миграции не запускались.")
        if self.collector_stopped:
            self.log.info("Сборщик macOS оставлен остановленным: код каталога новее отката. "
                          "Включите после проверки: scripts/zhol-collector-macos.sh start")
        return EXIT_OK

    def save_state(self) -> None:
        images = {s: self.image_id(self.image_name(s)) for s in self.deploy_services if s in APP_SERVICES + WEB_SERVICES}
        state = {
            "commit": self.commit,
            "version": self.version or self.state.get("version", ""),
            "deployed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "services": self.deploy_services,
            "fingerprints": self.fingerprints,
            "images": images,
        }
        self.write_json(self.state_dir / "state.json", state)
        history = self.state_dir / "history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False) + "\n")


# ---------- pure helpers (tested directly) ----------

def validate_effective_config(config: Dict[str, Any]) -> List[str]:
    services = config.get("services") or {}
    problems = []
    for name in ("db", "api", "web", "worker", "browser-worker", "backup"):
        if name not in services:
            problems.append(f"нет службы {name}")
    if "collector" in services:
        problems.append("служба collector попала в конфигурацию без профиля")
    api = services.get("api") or {}
    api_env = api.get("environment") or {}
    if api_env.get("ZHOL_CONFIG_DIR") != "/offline-config":
        problems.append("у api нет ZHOL_CONFIG_DIR=/offline-config")
    if str(api_env.get("ZHOL_LOCAL_ACCESS")) != "1":
        problems.append("у api нет ZHOL_LOCAL_ACCESS=1")
    if not api_env.get("ZHOL_LOCAL_GATE"):
        problems.append("у api пустой ZHOL_LOCAL_GATE")
    if not any(_target(v) == "/offline-config" for v in api.get("volumes") or []):
        problems.append("у api не смонтирован /offline-config")
    web_env = (services.get("web") or {}).get("environment") or {}
    if not web_env.get("ZHOL_LOCAL_GATE"):
        problems.append("у web пустой ZHOL_LOCAL_GATE")
    worker_targets = {_target(v) for v in (services.get("worker") or {}).get("volumes") or []}
    for target in ("/app/tests/fixtures", "/app/data/admin"):
        if target not in worker_targets:
            problems.append(f"у worker не смонтирован {target}")
    for name in ("api", "worker", "browser-worker", "scheduler"):
        if name in services and not ((services[name].get("environment") or {}).get("DATABASE_URL")):
            problems.append(f"у {name} нет DATABASE_URL")
    db_volumes = (services.get("db") or {}).get("volumes") or []
    if not any(isinstance(v, dict) and v.get("type") == "volume" and v.get("source") == "pgdata" for v in db_volumes):
        problems.append("база не на томе pgdata")
    for name, service in services.items():
        for port in service.get("ports") or []:
            if isinstance(port, dict) and port.get("host_ip") != "127.0.0.1":
                problems.append(f"порт службы {name} открыт не только на 127.0.0.1")
    return problems


def _target(volume: Any) -> str:
    if isinstance(volume, dict):
        return str(volume.get("target", ""))
    parts = str(volume).split(":")
    return parts[1] if len(parts) > 1 else ""


def parse_counts(text: str) -> Dict[str, int]:
    counts = {}
    for line in text.splitlines():
        if "|" in line:
            key, value = line.split("|", 1)
            try:
                counts[key.strip()] = int(value.strip())
            except ValueError:
                continue
    return counts


def parse_df_available(text: str) -> Optional[int]:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    try:
        return int(fields[3])
    except (IndexError, ValueError):
        return None


def last_json_line(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def pyproject_version(path: Path) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def package_version(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8")).get("version", ""))


ROUTE_RE = re.compile(r"""["`](/(?:api/v1/[^"`\s]*|health))["`]""")


def frontend_routes(src: Path) -> List[str]:
    routes = set()
    for path in sorted(list(src.rglob("*.ts")) + list(src.rglob("*.tsx"))):
        for match in ROUTE_RE.finditer(path.read_text(encoding="utf-8")):
            route = re.sub(r"\$\{[^}]*\}", "{}", match.group(1)).split("?", 1)[0].rstrip("/")
            routes.add(route or "/")
    return sorted(routes)


def missing_frontend_routes(src: Path, api_paths: List[str]) -> List[str]:
    templates = [p.rstrip("/").split("/") for p in api_paths]

    def matches(route: str) -> bool:
        parts = route.split("/")
        for template in templates:
            if len(template) != len(parts):
                continue
            if all(a == b or "{" in a or "{" in b for a, b in zip(template, parts)):
                return True
        return False

    return [r for r in frontend_routes(src) if not matches(r)]


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                pid = int(self.path.read_text().strip() or "0")
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError):
                self.path.unlink()
                return self.__enter__()
            except PermissionError:
                pass
            raise DeployError("Другое развёртывание уже идёт. Дождитесь его окончания. Ничего не менялось.")
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="zhol-deploy-macos.sh", description="Безопасное развёртывание Жол на macOS")
    parser.add_argument("command", choices=("check", "deploy", "rollback"))
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    parser.add_argument("--expect-commit", default="", help="развернуть, только если HEAD равен этому коммиту")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    log = Log()
    log.info(f"Жол: {args.command}, каталог {ROOT}. Журнал: {log.path}")
    deployer = Deployer(args, log)
    try:
        if args.command == "check":
            return deployer.cmd_check()
        with Lock(deployer.state_dir / ".deploy.lock"):
            if args.command == "deploy":
                return deployer.cmd_deploy()
            return deployer.cmd_rollback()
    except DeployError as exc:
        if exc.code == EXIT_PREFLIGHT:
            deployer.restore_collector()
        log.error(exc.message)
        log.info(f"Журнал: {log.path}")
        return exc.code
    except KeyboardInterrupt:
        log.error("прервано пользователем. Проверьте состояние: scripts/zhol-deploy-macos.sh check")
        return EXIT_NEEDS_HUMAN


if __name__ == "__main__":
    raise SystemExit(main())
