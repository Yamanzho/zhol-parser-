# Changelog

## 0.2.0

- Company lookup uses indexed name and phone keys instead of reading every company in a city.
- One exact candidate is attached only when no other candidate is ambiguous. Confirmed platform ids stay on their company.
- `zhol rebuild-match-keys` rebuilds keys after a direct SQL load. It does not change companies.
- `zhol identity-report` prints unconfirmed snapshot pairs and does not write.
- CSV and JSON exports no longer stop at 5 000 rows. Excel stops at the worksheet limit of 1 048 575 data rows.
- `zhol identity-report` no longer fails with ImportError in a fresh process. `zhol.browser.discovery` loads its engine lazily, which breaks the import cycle with `zhol.agents.scout.identity`. The report runs in a read-only transaction.
- `zhol schema-check` compares the database revision with the code without writing. Exit 0 match, 3 older, 5 newer or unknown, 4 empty, 2 unreadable.
- `Запустить Zhol.command` checks the schema before any writer starts and stops on a mismatch. It no longer migrates a database with data. An empty database still gets its tables.
- `Обновить базу Zhol.command` and `scripts/zhol-upgrade-db.sh` upgrade explicitly: stop all writers including the macOS collector, wait for zero connections, write a private checked dump to `data/backups/pre-upgrade/`, migrate, check again.
- The macOS collector does not start on a mismatched schema.
- The backup loop follows the wall clock, so a dump missed during Mac sleep is written after wake. Dumps are mode 600, written through a checked partial file, never overwritten, and have a `.sha256`.
- `scripts/zhol-deploy-macos.sh` is the single deployment entry (`check`, `deploy`, `rollback`). It always passes both Compose files, validates the effective configuration, `.env`, PostgreSQL, the Alembic revision, active collection jobs and free disk space, builds while the services keep running, probes the new images (`zhol.deploy_probe`), saves the running images for rollback, replaces only changed services, verifies the API and the panel, and rolls back on a failed check. It never migrates, never touches the database container or backups, never removes images, never pushes. See `docs/ops/deploy-macos.md`.
- Migration `b6d2f4a81c30` adds the key table and the indexes used by lookup, the newest snapshot, and the job queue. Downgrade drops those objects only.

## 0.1.0

- Versioned collection API, simplified panel, and localhost demo.
