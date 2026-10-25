#!/bin/bash
# Explicit database upgrade for this installation. Normal startup never migrates a database with data.
# Order: build the api image, check the revision, stop every writer (including the macOS collector
# of this checkout), verify no client is connected, write a private pre-upgrade dump, migrate, check again.
# Does not print passwords. Does not delete, overwrite, or move existing backups.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
YES=0
[[ "${1:-}" == "--yes" ]] && YES=1

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

compose() {
  docker compose -f docker-compose.yml -f docker-compose.offline.yml "$@"
}

env_value() {
  python3 - "$1" << 'PY'
from pathlib import Path
import sys
key = sys.argv[1]
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
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

[[ -f .env ]] || fail "Файла .env нет. База не изменялась."
command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 || fail "Docker Desktop не запущен. База не изменялась."
export COMPOSE_PROJECT_NAME="$(basename "$PWD")"
db_user="$(env_value POSTGRES_USER)" || fail "В .env нет POSTGRES_USER. База не изменялась."
db_name="$(env_value POSTGRES_DB)" || fail "В .env нет POSTGRES_DB. База не изменялась."
[[ -n "$(compose ps -q db 2>/dev/null)" ]] || fail "Контейнер базы проекта ${COMPOSE_PROJECT_NAME} не запущен. База не изменялась."

printf '%s\n' "Собираем образ программы для миграции."
compose build api >/dev/null || fail "Не удалось собрать образ. База не изменялась."

set +e
check="$(compose run --rm --no-deps -T api python -m zhol.cli schema-check 2>&1)"
status=$?
set -e
case "$status" in
  0) printf '%s\n' "Схема базы совместима. Обновление не нужно."; exit 0 ;;
  3|4) ;;
  5) printf '%s\n' "$check" | grep -v '^ Container' >&2 || true
     fail "Эта команда не обновляет такую базу. Службы не останавливались, база не изменялась." ;;
  *) fail "Не удалось прочитать ревизию базы. База не изменялась." ;;
esac
revision="$(compose exec -T db psql -U "$db_user" -d "$db_name" -XAtc "SELECT coalesce((SELECT string_agg(version_num, '-') FROM alembic_version), 'none')" 2>/dev/null || echo unknown)"
revision="${revision//[^a-z0-9-]/}"
printf '%s\n' "Ревизия базы сейчас: ${revision}. Будет обновлена до версии этой программы."

if [[ "$YES" -ne 1 ]]; then
  printf '%s\n' "Будут остановлены API, панель, воркеры, сборщик и резервное копирование, затем сделана копия базы и выполнена миграция."
  printf '%s' "Наберите ОБНОВИТЬ и нажмите Enter: "
  read -r answer || answer=""
  [[ "$answer" == "ОБНОВИТЬ" ]] || fail "Отменено. База не изменялась."
fi

printf '%s\n' "Останавливаем всех, кто пишет в базу."
plist="${HOME}/Library/LaunchAgents/kz.duna.zhol.collector.plist"
if [[ -f "$plist" ]] && grep -qF "${ROOT}/scripts/zhol-collector-macos.sh" "$plist"; then
  "${ROOT}/scripts/zhol-collector-macos.sh" stop || true
  host_collector=1
else
  host_collector=0
fi
compose --profile backup --profile collector stop api web worker browser-worker scheduler collector backup >/dev/null 2>&1 || true

clients=""
for _ in $(seq 1 30); do
  clients="$(compose exec -T db psql -U "$db_user" -d "$db_name" -XAtc \
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'")"
  [[ "$clients" == "0" ]] && break
  sleep 1
done
if [[ "$clients" != "0" ]]; then
  compose exec -T db psql -U "$db_user" -d "$db_name" -XAtc \
    "SELECT coalesce(application_name, '') || ' ' || coalesce(client_addr::text, 'local') || ' ' || count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend' GROUP BY application_name, client_addr" >&2 || true
  fail "К базе ещё подключены клиенты (${clients}). Миграция не выполнялась, службы Жол остановлены. Остановите эти подключения и повторите обновление."
fi

umask 077
backup_dir="${ROOT}/data/backups/pre-upgrade"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
file="${backup_dir}/pre-upgrade-${revision}-$(date -u +%Y%m%dT%H%M%SZ).dump"
[[ ! -e "$file" ]] || fail "Файл ${file} уже существует. Миграция не выполнялась."
printf '%s\n' "Копия базы: ${file}"
(set -o noclobber; compose exec -T db pg_dump -U "$db_user" -d "$db_name" -Fc --serializable-deferrable > "$file") \
  || fail "Копия не создана. Миграция не выполнялась."
chmod 400 "$file"
(cd "$backup_dir" && shasum -a 256 "$(basename "$file")" > "$(basename "$file").sha256" && chmod 400 "$(basename "$file").sha256")
tables="$(compose exec -T db pg_restore --list < "$file" | grep -c 'TABLE DATA' || true)"
[[ "${tables:-0}" -gt 0 || "$status" -eq 4 ]] || fail "Копия не читается pg_restore. Миграция не выполнялась."
printf '%s\n' "Копия проверена: таблиц с данными ${tables}."

printf '%s\n' "Миграция."
compose run --rm --no-deps -T api alembic upgrade head || fail "Миграция не выполнена. PostgreSQL откатил её целиком. Копия: ${file}"
compose run --rm --no-deps -T api python -m zhol.cli schema-check || fail "После миграции схема не совпала. Копия: ${file}"

printf '%s\n' "База обновлена. Копия до обновления: ${file}"
printf '%s\n' "Запустите Жол: дважды щелкните «Запустить Zhol.command»."
if [[ "$host_collector" -eq 1 ]]; then
  printf '%s\n' "Затем включите сборщик: scripts/zhol-collector-macos.sh start"
fi
