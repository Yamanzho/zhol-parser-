# PostgreSQL backup and restore

The application database lives in the Docker volume `pgdata`. A backup is a `pg_dump` file under `data/backups/` on the host. That folder is not the volume.

Do not restore over the application database. Do not run `docker compose down -v`. That command deletes `pgdata`.

## Backup

The `backup` service is defined in `docker-compose.yml` under the `backup` profile. `Запустить Zhol.command` starts it with the database, API, panel, and the two Docker workers. That command does not start the Docker Selenium collector. The macOS LaunchAgent keeps the visible Chrome collector.

Do not run a separate `docker compose up` against the application project while a recreate is still waiting for approval. A one-shot dump of the already running database container does not recreate it:

```sh
sh scripts/zhol-pg-backup.sh
```

The service runs `scripts/zhol-pg-backup-loop.sh`. It writes `data/backups/zhol-<utc-time>.dump` and keeps the 14 newest files. The default interval is 86400 seconds. Set `ZHOL_BACKUP_INTERVAL_SECONDS` to change it. The minimum is 60 seconds.

## Restore drill

Restore into a new empty database. The script refuses the names `zhol`, `postgres`, and the source database.

1. Copy or create the dump inside the disposable Postgres container.
2. Set `ZHOL_PG_CONTAINER` to that container, not the application container `parser-for-timur-db-1`, unless the drill is intentionally pointed at a container you have already decided to use.
3. Set `ZHOL_RESTORE_DUMP` to the dump path inside the container.
4. Set `ZHOL_RESTORE_TARGET` to a new name, for example `zhol_restore_drill`.
5. Set `ZHOL_PG_SOURCE_DB` to the database that was dumped.
6. Run `sh scripts/zhol-pg-restore-drill.sh`.
7. Check `alembic_version` and a known row in the new database.
8. Drop only the drill database when the check is finished.

```sh
export ZHOL_PG_CONTAINER=zhol-s03-test-example
export ZHOL_RESTORE_DUMP=/tmp/zhol-drill.dump
export ZHOL_RESTORE_TARGET=zhol_restore_drill
export ZHOL_PG_SOURCE_DB=zhol_test
export ZHOL_PG_USER=zhol
sh scripts/zhol-pg-restore-drill.sh
```

The application database is not a restore target. A failed drill does not delete companies, snapshots, or orders from it.
