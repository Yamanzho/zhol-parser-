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
docker compose exec -T db pg_dump -U "$user" -d "$database" -Fc > "$file"
echo "$file"
