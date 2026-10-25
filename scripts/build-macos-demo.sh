#!/bin/bash
# Собирает dist/Zhol Demo.app для macOS ARM64.
# Внутри: Python, демонстрационный сервер и собранная панель.
# Docker, PostgreSQL, ключи и Chrome не входят в приложение.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
  printf '%s\n' "Сборка приложения рассчитана на macOS ARM64. Сейчас архитектура: $ARCH." >&2
  exit 1
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' "Сборка приложения запускается на macOS." >&2
  exit 1
fi

PY="$ROOT/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  printf '%s\n' "Не найден $PY. Сначала установите Жол в .venv, затем повторите сборку." >&2
  exit 1
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  printf '%s\n' "Нужен Python 3.12 или новее в .venv." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  printf '%s\n' "Не найден npm. Он нужен только на машине, где собирают приложение, не на машине, где его открывают." >&2
  exit 1
fi

"$PY" -m pip install "pyinstaller>=6.11,<7"
printf '%s\n' "PyInstaller: $("$PY" -m PyInstaller --version)"

(cd "$ROOT/web" && npm ci && npm run build)
rm -rf "$ROOT/demo/static"
mkdir -p "$ROOT/demo/static"
cp -R "$ROOT/web/dist/." "$ROOT/demo/static/"
test -f "$ROOT/demo/static/index.html"

rm -rf "$ROOT/build/zhol-demo" "$ROOT/dist/Zhol Demo" "$ROOT/dist/Zhol Demo.app"
"$PY" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "Zhol Demo" \
  --osx-bundle-identifier "kz.duna.zhol.demo" \
  --target-arch arm64 \
  --distpath "$ROOT/dist" \
  --workpath "$ROOT/build/zhol-demo" \
  --specpath "$ROOT/packaging" \
  --add-data "$ROOT/demo/static:demo/static" \
  --hidden-import zhol.demo.server \
  --hidden-import zhol.demo.store \
  --hidden-import zhol.demo.catalog \
  --hidden-import zhol.integration.schema \
  "$ROOT/packaging/zhol_demo_main.py"

APP="$ROOT/dist/Zhol Demo.app"
test -d "$APP"
test -f "$APP/Contents/MacOS/Zhol Demo"
if ! find "$APP" -name 'libpython3*.dylib' -print -quit | grep -q libpython; then
  printf '%s\n' "В приложении нет встроенного Python." >&2
  exit 1
fi
INDEX="$(find "$APP" -path '*/demo/static/index.html' -print -quit)"
test -n "$INDEX"
printf '%s\n' "Собрано: $APP"
file "$APP/Contents/MacOS/Zhol Demo"
