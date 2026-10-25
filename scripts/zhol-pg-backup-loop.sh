#!/bin/sh
# Write PostgreSQL custom-format dumps to /backups.
# That directory is a host folder, not the pgdata volume.
# This script does not drop, truncate, or replace the application database.
#
# The schedule follows the wall clock, not one long sleep. A sleeping Mac pauses
# the Docker VM, and `sleep 86400` then lasts days. Here a short tick compares the
# newest dump with the current time, so a missed dump is written within a minute
# of waking up.
set -eu

interval="${ZHOL_BACKUP_INTERVAL_SECONDS:-86400}"
tick="${ZHOL_BACKUP_TICK_SECONDS:-60}"
keep="${ZHOL_BACKUP_KEEP:-14}"
case "$interval$tick$keep" in
  ''|*[!0-9]*)
    echo "ZHOL_BACKUP_INTERVAL_SECONDS, ZHOL_BACKUP_TICK_SECONDS and ZHOL_BACKUP_KEEP must be whole numbers" >&2
    exit 2
    ;;
esac
if [ "$interval" -lt 60 ]; then
  echo "backup interval must be at least 60 seconds" >&2
  exit 2
fi
if [ "$tick" -lt 1 ] || [ "$keep" -lt 1 ]; then
  echo "backup tick and keep must be positive" >&2
  exit 2
fi

umask 077
mkdir -p /backups

newest_age() {
  newest=$(ls -1t /backups/zhol-*.dump 2>/dev/null | head -n 1 || true)
  if [ -z "$newest" ]; then
    echo "$interval"
    return
  fi
  echo $(( $(date +%s) - $(date -r "$newest" +%s) ))
}

dump_once() {
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  dest="/backups/zhol-${stamp}.dump"
  tmp="/backups/.zhol-${stamp}.partial"
  if [ -e "$dest" ] || [ -e "$tmp" ]; then
    echo "backup $dest already exists; not overwritten" >&2
    return 0
  fi
  if ! pg_dump -Fc --file="$tmp"; then
    rm -f "$tmp"
    echo "backup failed at $stamp" >&2
    return 0
  fi
  if ! pg_restore --list "$tmp" >/dev/null; then
    rm -f "$tmp"
    echo "backup at $stamp is not readable by pg_restore; discarded" >&2
    return 0
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$dest"
  (cd /backups && sha256sum "$(basename "$dest")" > "$(basename "$dest").sha256" && chmod 600 "$(basename "$dest").sha256")
  echo "backup written $dest"
  ls -1t /backups/zhol-*.dump 2>/dev/null | awk -v keep="$keep" 'NR>keep' | while read -r old; do
    rm -f "$old" "$old.sha256"
  done
}

while true; do
  if [ "$(newest_age)" -ge "$interval" ]; then
    dump_once
  fi
  sleep "$tick"
done
