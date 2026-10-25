#!/bin/bash
# Обновление схемы базы после установки новой версии. Останавливает службы и сборщик, делает копию базы.
set -euo pipefail
cd "$(dirname "$0")"
status=0
/bin/bash "$PWD/scripts/zhol-upgrade-db.sh" || status=$?
printf '%s\n' "Нажмите Enter, чтобы закрыть окно."
read -r _ || true
exit "$status"
