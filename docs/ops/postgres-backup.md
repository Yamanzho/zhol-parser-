# PostgreSQL backup and restore

The application database lives in the Docker volume `pgdata`. A backup is a `pg_dump` file under `data/backups/` on the host. That folder is not the volume.

Do not restore over the application database. Do not run `docker compose down -v`. That command deletes `pgdata`.

## Backup

The `backup` service is defined in `docker-compose.yml` under the `backup` profile. `Запустить Zhol.command` starts it with the database, API, panel, and the two Docker workers. That command does not start the Docker Selenium collector. The macOS LaunchAgent keeps the visible Chrome collector.

Do not run a separate `docker compose up` against the application project while a recreate is still waiting for approval. A one-shot dump of the already running database container does not recreate it:

```sh
sh scripts/zhol-pg-backup.sh
```

The service runs `scripts/zhol-pg-backup-loop.sh`. It writes `data/backups/zhol-<utc-time>.dump` and keeps the 14 newest files (`ZHOL_BACKUP_KEEP`). The default interval is 86400 seconds. Set `ZHOL_BACKUP_INTERVAL_SECONDS` to change it. The minimum is 60 seconds.

### Schedule and Mac sleep

Up to 0.1.0 the loop ran `pg_dump` and then `sleep 86400`. The Docker Desktop VM does not count time while the Mac sleeps, so on a laptop that sleeps at night the next dump came days later. On the installation checked on 25.10.2026 the container started on 18.10 and the only dumps were from 18.10 and 25.10.

From 0.2.0 the loop wakes every `ZHOL_BACKUP_TICK_SECONDS` (default 60) and compares the wall clock with the time of the newest `zhol-*.dump`. When the newest dump is older than the interval, it dumps at once. After the Mac wakes up, a missed dump is written within about a minute. Nothing runs while the Mac or Docker Desktop is off, so keep Docker Desktop running and check the date of the newest file in `data/backups/` from time to time.

The new loop takes effect when the `backup` container is created again, for example by `Запустить Zhol.command` after an update.

### What a dump file guarantees

- New files are created with `umask 077` and mode `600`. The folder `data/backups/` is `700`.
- A dump is written to a hidden `.zhol-<utc-time>.partial` file first. It gets its final name only after `pg_restore --list` reads it. A failed or unreadable dump leaves no `zhol-*.dump`.
- An existing file is never overwritten. The loop and `scripts/zhol-pg-backup.sh` stop if the target or the partial file already exists.
- Each dump has a `<name>.sha256` next to it. Rotation removes a dump together with its checksum.
- The password is passed by the environment and is not printed. Logs contain only file names and times.

Files written before 0.2.0 may still be mode `644`. The `700` folder already hides them from other local accounts. To tighten them, the owner runs `chmod 600 data/backups/zhol-*.dump` once.

Before a migration, `Обновить базу Zhol.command` writes its own read-only copy to `data/backups/pre-upgrade/`. Rotation does not touch that folder.

### Check a dump

```sh
cd data/backups
shasum -a 256 -c zhol-<utc-time>.dump.sha256
docker compose exec -T db pg_restore --list < zhol-<utc-time>.dump | grep -c 'TABLE DATA'
```

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

## Return the application database to a dump

Only the owner decides this, after a drill shows that the dump is good. Stop every writer first: `scripts/zhol-collector-macos.sh stop`, then `docker compose --profile backup stop api web worker browser-worker backup`. Restore into a new database, check it, and only then point the application at it or rename databases. The application volume is never deleted for this.
