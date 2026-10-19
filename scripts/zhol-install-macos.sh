#!/bin/bash
# First-time setup for this Mac. Does not print secrets and does not replace
# an existing .env or an existing database volume.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() {
  printf '%s\n' "$*" >&2
  if [[ -t 0 ]]; then
    printf '%s\n' "Нажмите Enter, чтобы закрыть окно."
    read -r _ || true
  fi
  exit 1
}

compose() {
  docker compose -f docker-compose.yml -f docker-compose.offline.yml "$@"
}

printf '%s\n' "Проверяем программы."
PYTHON=""
for candidate in python3.12 python3.13 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" - << 'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
  then
    PYTHON="$candidate"
    break
  fi
done
[[ -n "$PYTHON" ]] || fail "Нужен Python 3.12 или новее. Команда python3 на macOS может быть старше, тогда установите Python 3.12."
command -v docker >/dev/null 2>&1 || fail "Docker не установлен. Установите Docker Desktop и повторите установку."
if [[ "${ZHOL_INSTALL_SKIP_DOCKER:-}" != "1" ]]; then
  docker compose version >/dev/null 2>&1 || fail "Нужна команда docker compose из Docker Desktop."
  docker info >/dev/null 2>&1 || fail "Docker Desktop не запущен. Откройте Docker Desktop и повторите установку."
fi
chrome="${ZHOL_CHROME_BINARY:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
if [[ ! -x "$chrome" && ! -f "$chrome" ]]; then
  fail "Google Chrome не найден. Установите Chrome и повторите установку. Сборщик не использует личный профиль Chrome."
fi
[[ -f docker-compose.yml && -f docker-compose.offline.yml && -f .env.example ]] || fail "Каталог ${ROOT} не похож на проект Жол."

printf '%s\n' "Готовим локальный .env."
"$PYTHON" "$ROOT/scripts/zhol_write_env.py" "$ROOT" || fail "Не удалось подготовить .env. Существующий файл не переписывался, если он уже был."

if [[ "${ZHOL_INSTALL_SKIP_DOCKER:-}" == "1" ]]; then
  printf '%s\n' "Проверка Docker пропущена по запросу теста."
else
  export COMPOSE_PROJECT_NAME="$(basename "$PWD")"
  db_id="$(compose ps -a -q db 2>/dev/null || true)"
  if [[ -n "$db_id" ]]; then
    printf '%s\n' "Существующая база PostgreSQL сохранена. Том не пересоздавался."
  else
    printf '%s\n' "Создаем пустую базу PostgreSQL."
    compose up -d --no-build db || fail "Не удалось создать базу. Том не удалялся."
    db_user="$("$PYTHON" - << 'PY'
from pathlib import Path
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    if raw.startswith("POSTGRES_USER=") and raw.split("=", 1)[1].strip():
        print(raw.split("=", 1)[1].strip(), end="")
        break
PY
)"
    db_name="$("$PYTHON" - << 'PY'
from pathlib import Path
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    if raw.startswith("POSTGRES_DB=") and raw.split("=", 1)[1].strip():
        print(raw.split("=", 1)[1].strip(), end="")
        break
PY
)"
    ready=0
    for _ in $(seq 1 60); do
      if compose exec -T db pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
        ready=1
        break
      fi
      sleep 1
    done
    [[ "$ready" -eq 1 ]] || fail "Новая база не ответила. Том не удалялся."
    printf '%s\n' "Пустая база PostgreSQL создана. Миграции применяет первый запуск."
  fi
fi

printf '%s\n' "Устанавливаем один сборщик Chrome для текущего пользователя."
ZHOL_INSTALL_SKIP_LAUNCHD="${ZHOL_INSTALL_SKIP_LAUNCHD:-}" \
  /bin/bash "$ROOT/scripts/zhol-collector-macos.sh" install || fail "Сборщик не установлен. База не изменялась этой ошибкой."

printf '%s\n' "Установка закончена. Запуск: дважды щелкните «Запустить Zhol.command»."
printf '%s\n' "Панель откроется только на http://127.0.0.1 . Разрешения площадок в установщик не входят."
