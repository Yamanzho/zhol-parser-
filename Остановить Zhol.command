#!/bin/bash
# Останавливает только службы Жол. Том базы и файлы не удаляются.
set -euo pipefail
cd "$(dirname "$0")"

fail() {
  printf '%s\n' "$1" >&2
  printf '%s\n' "Нажмите Enter, чтобы закрыть окно."
  read -r _ || true
  exit 1
}

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  fail "Docker Desktop не запущен. Службы Жол уже не работают, либо Docker выключен."
fi

docker compose -f docker-compose.yml -f docker-compose.offline.yml --profile backup stop db api web worker browser-worker scheduler backup || fail "Не удалось остановить Жол."
printf '%s\n' "Жол остановлен. База и сохраненные файлы на месте."
