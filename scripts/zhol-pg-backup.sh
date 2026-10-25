#!/bin/sh
# Dump the Compose database to ./data/backups on the host.
# The file is outside the pgdata volume. The database is not modified.
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"
out="${ZHOL_BACKUP_DIR:-$root/data/backups}"
mkdir -p "$out"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="$out/zhol-$stamp.dump"
user=$(python3 - "$root/.env" POSTGRES_USER << 'PY'
import sys
from pathlib import Path
path, key = Path(sys.argv[1]), sys.argv[2]
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == key and value.strip():
        print(value.strip().strip('"').strip("'"), end="")
        raise SystemExit(0)
raise SystemExit(1)
PY
) || {
  echo "POSTGRES_USER is missing from .env" >&2
  exit 1
}
database=$(python3 - "$root/.env" POSTGRES_DB << 'PY'
import sys
from pathlib import Path
path, key = Path(sys.argv[1]), sys.argv[2]
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == key and value.strip():
        print(value.strip().strip('"').strip("'"), end="")
        raise SystemExit(0)
raise SystemExit(1)
PY
) || {
  echo "POSTGRES_DB is missing from .env" >&2
  exit 1
}
umask 077
tmp="$out/.zhol-$stamp.partial"
if [ -e "$file" ] || [ -e "$tmp" ]; then
  echo "$file already exists; not overwritten" >&2
  exit 1
fi
(set -C; docker compose exec -T db pg_dump -U "$user" -d "$database" -Fc > "$tmp") || {
  rm -f "$tmp"
  echo "pg_dump failed; no backup written" >&2
  exit 1
}
if ! docker compose exec -T db pg_restore --list < "$tmp" >/dev/null; then
  rm -f "$tmp"
  echo "dump is not readable by pg_restore; no backup written" >&2
  exit 1
fi
chmod 600 "$tmp"
mv -n "$tmp" "$file"
if [ -e "$tmp" ]; then
  echo "$file appeared meanwhile; kept it and left $tmp" >&2
  exit 1
fi
(cd "$out" && shasum -a 256 "$(basename "$file")" > "$(basename "$file").sha256")
echo "$file"
