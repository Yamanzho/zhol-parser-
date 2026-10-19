#!/bin/sh
# Write PostgreSQL custom-format dumps to /backups.
# That directory is a host folder, not the pgdata volume.
# This script does not drop, truncate, or replace the application database.
set -eu

interval="${ZHOL_BACKUP_INTERVAL_SECONDS:-86400}"
case "$interval" in
  ''|*[!0-9]*)
    echo "ZHOL_BACKUP_INTERVAL_SECONDS must be a whole number of seconds" >&2
    exit 2
    ;;
esac
if [ "$interval" -lt 60 ]; then
  echo "backup interval must be at least 60 seconds" >&2
  exit 2
fi

mkdir -p /backups
while true; do
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  dest="/backups/zhol-${stamp}.dump"
  pg_dump -Fc --file="$dest"
  ls -1t /backups/zhol-*.dump 2>/dev/null | awk 'NR>14' | while read -r old; do
    rm -f "$old"
  done
  sleep "$interval"
done
