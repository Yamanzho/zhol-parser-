#!/bin/bash
# Локальный запуск Жол. Не печатает секреты и не удаляет базу.
set -euo pipefail
cd "$(dirname "$0")"
LOG="${ZHOL_STARTUP_LOG:-${HOME}/Library/Logs/Zhol/startup.log}"
mkdir -p "$(dirname "$LOG")"

log_line() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG" || true
}

fail() {
  log_line "ERROR $*"
  printf '%s\n' "$*" >&2
  printf '%s\n' "Журнал запуска: $LOG" >&2
  printf '%s\n' "Нажмите Enter, чтобы закрыть окно."
  read -r _ || true
  exit 1
}

service_running() {
  local service="$1"
  local running
  running="$(compose --profile backup ps --status running --services 2>/dev/null || true)"
  printf '%s\n' "$running" | grep -qx "$service"
}

compose() {
  docker compose -f docker-compose.yml -f docker-compose.offline.yml "$@"
}

log_line "startup begin dir=${PWD}"

if [[ ! -f .env ]]; then
  fail "Файла .env нет. Сначала дважды щелкните «Установить Zhol.command». База не создавалась и не изменялась."
fi

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker не установлен. Установите Docker Desktop и запустите Жол еще раз."
fi

if ! docker info >/dev/null 2>&1; then
  fail "Docker Desktop не запущен. Откройте Docker Desktop и запустите Жол еще раз."
fi

export COMPOSE_PROJECT_NAME="$(basename "$PWD")"
if [[ ! -f docker-compose.yml || ! -f docker-compose.offline.yml ]]; then
  fail "Каталог ${PWD} не содержит файлы Compose проекта ${COMPOSE_PROJECT_NAME}."
fi
log_line "compose project=${COMPOSE_PROJECT_NAME}"

python3 - << 'PY' || fail "Не удалось подготовить локальный вход."
from pathlib import Path
import secrets
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
gate = ""
kept = []
for line in lines:
    if line.startswith("ZHOL_LOCAL_GATE="):
        gate = line.split("=", 1)[1].strip()
        continue
    kept.append(line)
if len(gate) < 32:
    gate = secrets.token_hex(32)
kept.append("ZHOL_LOCAL_GATE=" + gate)
path.write_text("\n".join(kept) + "\n", encoding="utf-8")
path.chmod(0o600)
print("local_gate=present")
PY

env_value() {
  python3 - "$1" << 'PY'
from pathlib import Path
import sys
key = sys.argv[1]
path = Path(".env")
if not path.exists():
    sys.exit(1)
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == key and value.strip():
        print(value.strip().strip('"').strip("'"), end="")
        sys.exit(0)
sys.exit(1)
PY
}

db_user="$(env_value POSTGRES_USER)" || fail "В .env нет POSTGRES_USER. Существующий пароль базы не менялся."
db_name="$(env_value POSTGRES_DB)" || fail "В .env нет POSTGRES_DB. Существующий пароль базы не менялся."
env_value POSTGRES_PASSWORD >/dev/null || fail "В .env нет POSTGRES_PASSWORD. Существующий пароль базы не менялся."
log_line "database settings present"

db_id="$(compose ps -a -q db 2>/dev/null || true)"
if [[ -n "$db_id" ]]; then
  live_project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$db_id" 2>/dev/null || true)"
  if [[ -n "$live_project" && "$live_project" != "$COMPOSE_PROJECT_NAME" ]]; then
    fail "Каталог ${PWD} относится к проекту ${COMPOSE_PROJECT_NAME}, а база запущена в проекте ${live_project}."
  fi
  log_line "database container kept"
  compose up -d --no-build --no-recreate db || fail "Не удалось запустить существующую базу. Том не удалялся."
else
  log_line "database container create"
  compose up -d --no-build db || fail "Не удалось создать базу. Том не удалялся."
fi

printf '%s\n' "Ждем базу."
ready=0
for _ in $(seq 1 60); do
  if compose exec -T db pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  fail "База не ответила. Данные не удалялись."
fi

stamp="$(python3 - << 'PY'
import hashlib
from pathlib import Path
paths = []
for folder in ("zhol", "web/src"):
    paths.extend(Path(folder).rglob("*"))
for name in (
    "web/index.html",
    "web/vite.config.ts",
    "web/Dockerfile",
    "web/package.json",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.offline.yml",
    "pyproject.toml",
):
    paths.append(Path(name))
digest = hashlib.sha256()
for path in sorted(item for item in paths if item.is_file() and "__pycache__" not in item.parts):
    digest.update(path.as_posix().encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
print(digest.hexdigest())
PY
)"

mkdir -p .cache
if [[ ! -f .cache/zhol-build-stamp || "$(cat .cache/zhol-build-stamp)" != "$stamp" ]]; then
  printf '%s\n' "Собираем образы Жол. Повторный запуск без изменений программы сборку пропустит."
  log_line "image build"
  compose build api web worker browser-worker || fail "Не удалось собрать образы."
else
  printf '%s\n' "Образы актуальны, сборка не нужна."
  log_line "image build skipped"
fi

# Проверка схемы новым образом до запуска служб, которые пишут в базу.
# Базу с данными этот файл не обновляет: для этого есть «Обновить базу Zhol.command».
printf '%s\n' "Проверяем схему базы."
set +e
compose run --rm --no-deps -T api python -m zhol.cli schema-check
schema_status=$?
set -e
log_line "schema check status=${schema_status}"
case "$schema_status" in
  0) ;;
  4)
    printf '%s\n' "Новая пустая база: создаем таблицы."
    compose run --rm --no-deps -T api alembic upgrade head || fail "Не удалось создать таблицы в пустой базе. Данные не удалялись."
    ;;
  5)
    fail "Службы Жол не запускались и не перезапускались, данные не менялись. Схема базы не подходит этой версии программы (подробности выше). Обновление здесь не поможет, нужна проверка человеком."
    ;;
  3)
    fail "Службы Жол не запускались и не перезапускались, данные не менялись. Схема базы не совпадает с программой (подробности выше). Чтобы обновить базу, дважды щелкните «Обновить базу Zhol.command»."
    ;;
  *)
    fail "Не удалось проверить схему базы. Службы Жол не запускались и не перезапускались."
    ;;
esac

# Профиль collector не включается: видимый Chrome ведет LaunchAgent.
# --no-deps не пересоздает уже работающую базу.
log_line "start api web worker browser-worker backup"
compose --profile backup up -d --no-build --no-deps api web worker browser-worker backup || fail "Не удалось запустить службы Жол."

for service in db api web worker browser-worker backup; do
  if ! service_running "$service"; then
    fail "Служба ${service} не работает. Том базы не удалялся."
  fi
done
collector_running="$(compose --profile collector ps --status running --services 2>/dev/null || true)"
if printf '%s\n' "$collector_running" | grep -qx collector; then
  fail "Сборщик Docker запущен. Он должен оставаться выключенным."
fi
log_line "services running"
printf '%s\n' "$stamp" > .cache/zhol-build-stamp

ports="$(python3 - << 'PY'
from pathlib import Path
api, web = "18080", "15173"
env = Path(".env")
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("API_PORT=") and line.split("=", 1)[1].strip().isdigit():
            api = line.split("=", 1)[1].strip()
        if line.startswith("WEB_PORT=") and line.split("=", 1)[1].strip().isdigit():
            web = line.split("=", 1)[1].strip()
print(api)
print(web)
PY
)"
API_PORT="$(printf '%s\n' "$ports" | sed -n '1p')"
WEB_PORT="$(printf '%s\n' "$ports" | sed -n '2p')"

printf '%s\n' "Ждем панель."
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null && curl -sf -o /dev/null "http://127.0.0.1:${WEB_PORT}/"; then
    printf '%s\n' "Жол запущен: http://127.0.0.1:${WEB_PORT}/"
    log_line "startup complete port=${WEB_PORT}"
    open "http://127.0.0.1:${WEB_PORT}/"
    exit 0
  fi
  sleep 1
done

fail "Панель не ответила. Проверьте Docker Desktop. Секреты в это окно не выводятся."
