#!/bin/bash
# Учебный Жол на этом компьютере. Без Docker, базы и карт.
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [ -x "$ROOT/.venv/bin/python3" ]; then
  PY="$ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  osascript -e 'display alert "Жол" message "Не найден Python 3.12. Демонстрация не запущена: без Python она не может открыть локальную страницу."' >/dev/null 2>&1 || true
  printf '%s\n' "Не найден Python. Демонстрация не запущена."
  exit 1
fi

printf '%s\n' "Демонстрационный режим. Карты и рабочая база не используются."
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  osascript -e 'display alert "Жол" message "Нужен Python 3.12 или новее. Демонстрация не запущена."' >/dev/null 2>&1 || true
  printf '%s\n' "Нужен Python 3.12 или новее. Демонстрация не запущена."
  exit 1
fi
exec "$PY" -m zhol.cli demo --port 8765 --open
