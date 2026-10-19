#!/bin/bash
# Первая установка на этом Mac. Не печатает пароли и не заменяет существующую базу.
set -euo pipefail
cd "$(dirname "$0")"
exec /bin/bash "$PWD/scripts/zhol-install-macos.sh"
