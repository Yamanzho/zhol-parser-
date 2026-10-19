#!/bin/bash
# Isolated Finder probe. Does not start Zhol.
LOG="${HOME}/Library/Logs/Zhol/launcher-probe.log"
mkdir -p "$(dirname "$LOG")"
{
  printf 'timestamp=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
  printf 'script=%s\n' "$0"
} >> "$LOG"
printf '%s\n' "Проверка запуска прошла успешно."
printf '%s\n' "Журнал: $LOG"
exit 0
