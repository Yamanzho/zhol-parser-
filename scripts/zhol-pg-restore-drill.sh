#!/bin/sh
# Restore a dump into a new empty database on an explicit Postgres container.
# Refuses the application database. Does not stop Compose or delete volumes.
set -eu

container="${ZHOL_PG_CONTAINER:?set ZHOL_PG_CONTAINER to the disposable Postgres container}"
dump="${ZHOL_RESTORE_DUMP:?set ZHOL_RESTORE_DUMP to the dump path inside that container}"
target="${ZHOL_RESTORE_TARGET:?set ZHOL_RESTORE_TARGET to a new database name}"
user="${ZHOL_PG_USER:-zhol}"
source="${ZHOL_PG_SOURCE_DB:-zhol}"

case "$target" in
  *[!a-z0-9_]*)
    echo "refusing target database name: $target" >&2
    exit 2
    ;;
esac
if [ "$target" = "zhol" ] || [ "$target" = "postgres" ] || [ "$target" = "$source" ]; then
  echo "refusing to restore into $target" >&2
  exit 2
fi

docker exec "$container" psql -U "$user" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$target\""
docker exec "$container" pg_restore -U "$user" -d "$target" --no-owner --no-acl "$dump"
