#!/bin/bash
# Единая точка развёртывания Жол на macOS.
#   scripts/zhol-deploy-macos.sh check                 проверка и план, ничего не меняет
#   scripts/zhol-deploy-macos.sh deploy [--yes] [--expect-commit SHA]
#   scripts/zhol-deploy-macos.sh rollback [--yes]      вернуть образы прошлого развёртывания
# Всегда использует docker-compose.yml и docker-compose.offline.yml. Не запускает миграции,
# не создаёт задачи сбора, не удаляет данные, копии и образы, не делает git push.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON=""
for candidate in /usr/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [[ -z "$PYTHON" ]]; then
  printf '%s\n' "Ошибка: нужен python3 версии 3.9 или новее. Службы не перезапускались." >&2
  exit 1
fi

exec "$PYTHON" "$ROOT/scripts/zhol_deploy.py" "$@"
