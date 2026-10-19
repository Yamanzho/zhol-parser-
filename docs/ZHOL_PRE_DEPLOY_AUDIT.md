# Zhol pre-deployment technical audit

Date: 2026-10-18. Scope: repository as it exists in this working tree. This was an audit only.

No application source, schema, configuration, Docker service, or application database was changed. Migrations were not applied to the application database. No live 2GIS, Yandex, or Google page was opened. A disposable PostgreSQL container was started by the existing pytest fixture and removed when the suite finished. The frontend production build refreshed `web/dist`.

Verdict: **No-Go** for deployment. Local macOS collection can keep running only as a supervised operator process. It is not a release candidate.

## 1. Executive summary

The collection product is a FastAPI admin API, a React panel, PostgreSQL, and a Selenium collector. The intended live path on this machine is a macOS LaunchAgent with visible Chrome. The Docker collector is a separate headless profile and is not a drop-in replacement.

The application database is already at Alembic revision `e5a9b3c20f41`, which matches the migration head. That was checked with a read-only query. Upgrade of that same head succeeds on a disposable database because the rest of the database tests ran. Two downgrade tests are pinned to older heads and did not execute their safety checks.

Six defects block a release:

- Docker `worker`, `browser-worker`, and `collector` exited 17 hours before this audit when PostgreSQL closed the connection. Compose has no restart policy and the engine has no connection-health check, so they stayed down.
- There is no backup or restore procedure. The only copy of application data is the `pgdata` volume.
- Yandex browser jobs do not take the Chrome profile lock. Starting Chrome kills processes that already use that profile.
- Company matching can attach a card by name and address, using address and phones taken from the newest snapshot of any platform.
- An imported Google snapshot becomes the row shown in the admin list and Excel even when it is incomplete, if its timestamp is later than a live snapshot that is only `collected`, not `verified`.
- The pytest suite is red: 6 failed, 970 passed, 0 skipped, of 976 collected tests.

Unauthenticated `POST /api/v1/local/orders` can write orders and credential blobs. The current Compose publish is loopback-only, so this is not an internet-exposed hole today. It becomes critical if that bind is widened.

Live collection against 2GIS, Google Maps, and Yandex Maps was **not tested**. Fixture tests are not evidence that a live city collect works.

## 2. Architecture overview

### What is running

| Piece | Role |
|---|---|
| `zhol/api/main.py` | FastAPI app: `/health`, `/api/v1/local/*`, `/api/v1/admin/*` |
| `web/` | React 19 + Vite admin panel. Compose serves `npm run dev`, not the production bundle |
| `zhol/db/models.py` + `alembic/versions/` | PostgreSQL 16 schema. Head `e5a9b3c20f41` |
| `zhol/jobs/queue.py`, `zhol/worker.py` | Three queues: `main`, `browser`, `collector`. Claim is `FOR UPDATE SKIP LOCKED` |
| Root `parser_service.py`, `search.py`, `card.py`, `browser_ops.py` | Live 2GIS Selenium parser. Not dead code |
| `zhol/agents/scout/selenium_collector.py` | Batch adapter: checkpoint, pause, stop, CAPTCHA hold, per-card commit |
| `zhol/agents/scout/maps_selenium.py` plus `zhol/browser/google/selenium.py` and `zhol/browser/yandex/selenium.py` | Google and Yandex Selenium. Chrome still starts through `parser_service` |
| `zhol/browser/collector.py` and `zhol/browser/discovery/` | Saved-HTML walk. Not the live Selenium path |
| `zhol/browser/cabinet/` and `zhol/fulfillment/` | Playwright cabinet preparation. Separate from prospect collection |
| `scripts/zhol-collector-macos.sh` | LaunchAgent `kz.duna.zhol.collector` for visible Chrome |
| `docker-compose.yml` | `db`, `api`, `worker`, `browser-worker`, `scheduler`, `web`. `collector` is profile `collector` |
| `Запустить Zhol.command` | Starts `db api web worker browser-worker`, then `alembic upgrade head`. Does not start `scheduler` or `collector` |

### Dependency map

```
Browser panel (web)
    -> admin API (Bearer token or local cookie)
        -> PostgreSQL
        -> enqueue Job(queue=collector|main|browser)
LaunchAgent or Docker collector worker
    -> claim Job
    -> exclusive Chrome profile (2GIS and Google only)
    -> Selenium (parser_service / maps_selenium)
    -> Company, CompanyPlatformId, CardSnapshot, CollectionBatch
Excel export reads CardSnapshot only
```

Playwright cabinet code, the Catalog API client (`zhol/agents/scout/live_2gis.py`), and the saved-HTML collector do not share the Selenium DOM parsers. They do share PostgreSQL.

### Observed process state (read-only)

Checked with `docker compose ps -a` and `launchctl print`. Nothing was restarted.

| Process | State at audit |
|---|---|
| `parser-for-timur-db-1` | Up 4 days, healthy. No host port in Compose |
| `parser-for-timur-api-1` | Up, published `127.0.0.1:18080` |
| `parser-for-timur-web-1` | Up, published `127.0.0.1:15173` |
| `parser-for-timur-worker-1` | Exited (1) about 17 hours earlier |
| `parser-for-timur-browser-worker-1` | Exited (1) about 17 hours earlier |
| `parser-for-timur-collector-1` | Exited (1) about 17 hours earlier |
| `scheduler` | Not created by the start script |
| LaunchAgent `kz.duna.zhol.collector` | `state = running` |

Other Compose projects on the same machine (`zhol-browser-lab-*`, `zhol-e1018-*`) were left untouched. They are separate stacks, not this release.

### Duplicate and unused paths

- Live 2GIS parsing lives at the repository root. `zhol` imports it. `Dockerfile.collector` copies those modules. The API image does not.
- `parser_service.run_batch` is a standalone Excel entry point. Admin jobs call `run_browser_batch` / `run_maps_batch`.
- Saved-HTML discovery and Playwright cabinets are parallel products. They are not unused, and they are not the live city collector.
- `requirements.txt` lists only Selenium and openpyxl. Runtime pins are in `pyproject.toml`.
- `SECRETS_KEY` is documented in `.env.example` and is not read anywhere under `zhol/`.
- Admin routes for orders, content, listings, tasks, and reports exist and are reachable by hash. They are not in the panel navigation (`web/src/App.tsx`).

## 3. Verified tests and results

### Python

Command, from the repository root, about 618 seconds, exit code 1:

```text
.venv/bin/pytest -q --tb=line
```

The session fixture in `tests/conftest.py` starts `postgres:16` as `zhol-s03-test-<pid>` on `127.0.0.1`, migrates that database to head, and deletes the container. It does not use `parser-for-timur-db-1`. After the run, no `zhol-s03-test-*` container remained.

| Result | Count |
|---|---|
| Collected | 976 (59 files) |
| Passed | 970 |
| Failed | 6 |
| Skipped | 0 (progress output contained only `.` and `F`) |
| Warnings | 1, Starlette `anyio.abc.BlockingPortal` deprecation |

Failures:

| Test | What the run showed |
|---|---|
| `tests/test_e1014_migration.py::test_downgrade_refuses_and_keeps_rows_when_a_company_has_no_2gis_id` | `AssertionError: assert 'e5a9b3c20f41' == 'c3e7a1b84d02'` at line 168. The disposable database is at head. The downgrade assertion never ran. |
| `tests/test_manual_verification.py::test_status_migration_applies_only_on_the_isolated_database` | `AssertionError: assert 'e5a9b3c20f41' == 'd4f8a2c19e30'` at line 525. Same cause. |
| `tests/test_e72_yandex.py::test_missing_permission_is_refused` | `OrderIntakeError: platform id already belongs to another company` at `zhol/orders/intake.py:245` |
| `tests/test_e73_e74_cabinets.py::test_missing_permission_is_refused[2gis]` | Same integrity error |
| `tests/test_e73_e74_cabinets.py::test_missing_permission_is_refused[google]` | Same integrity error |
| `tests/test_e73_e74_cabinets.py::test_partial_platform_failure_keeps_the_other_log` | Same integrity error |

The four order failures happen because one test creates two companies that reuse the same platform external id (`yandex-1` in `tests/test_e72_yandex.py:289-315`). The partial unique index `uq_company_platform_ids_platform_external` rejects the second insert. The constraint is doing what it should. The tests were not updated, so the permission checks they meant to run did not finish.

Collector, identity, platform-list, and Google-completeness tests are in the 970 passes. They use loopback fixtures. They do not prove a live map collect.

`alembic heads` (files only) printed `e5a9b3c20f41 (head)`.

### Frontend

First attempt, `npx tsc` from the repository root, failed because npm resolved the unrelated package `tsc@2.0.4`. That result is discarded.

Real commands, both exit 0:

```text
cd web && ./node_modules/.bin/tsc -p tsconfig.json --noEmit
cd web && npm run build
```

Vite 6.2.0 transformed 35 modules and wrote `web/dist` in 529 ms. Typecheck is clean.

### Not run

| Check | Why |
|---|---|
| Live 2GIS, Google Maps, Yandex Maps collection | Explicitly out of scope. Would contact live sites. |
| Downgrade of `e5a9b3c20f41` and `d4f8a2c19e30` | The tests that should do this abort on a stale revision pin. Downgrade was not applied to any database by this audit. |
| Dependency CVE scan (`pip-audit` / similar) | Not installed. Installing it would change the environment. |
| Restart of Docker workers, or a reconnect drill | Would change running services. |
| Browser click-through of the admin panel | Would talk to the live API and database. UI findings below are from source. |

## 4. P0 findings

No confirmed P0.

`POST /api/v1/local/orders` has no authentication and can store credential material. On this Compose file the API is published only on `127.0.0.1`. That is recorded as P1-01. It is a P0 the moment the API is reachable beyond the local machine.

No path was found that deletes snapshot history in place. The data risks below are wrong links and hidden rows, not dropped tables.

## 5. P1 findings

### P1-01 Unauthenticated local order API

- Severity: P1. Escalate to P0 if the API is not bound to loopback.
- Component: `zhol/api/local_orders.py`, mounted from `zhol/api/main.py:21`.
- Evidence: `router = APIRouter(prefix="/api/v1/local")` at line 20. `POST /orders` (line 35), `GET /orders/{order_id}` (line 49), and `GET /orders/{order_id}/readiness` (line 57) have no `require_admin`. `create_test_order` accepts `ciphertext_b64` credentials (`zhol/orders/intake.py` around the credential writer). Compose publishes the API as `127.0.0.1:${API_PORT:-18080}:8000` (`docker-compose.yml:33-34`). The running container matches that loopback publish.
- Impact: Any process that can reach the API can create orders and read them by id. Loopback prevents a remote attacker today. A reverse proxy or `0.0.0.0` publish removes that protection.
- Reproduction: Against a non-production API, `POST /api/v1/local/orders` with no `Authorization` header. Not run against the application API.
- Fix: Require the admin token, or do not mount these routes outside tests. Keep the loopback bind.
- Regression test: Unauthenticated `POST /api/v1/local/orders` returns 401. An authenticated call still creates one order.

### P1-02 Workers die when PostgreSQL drops the connection, and they stay dead

- Severity: P1.
- Component: `zhol/db/base.py`, `zhol/worker.py`, `docker-compose.yml`.
- Evidence: `get_engine` is `create_engine(database_url, future=True)` with no `pool_pre_ping` or `pool_recycle` (`zhol/db/base.py:16-20`). `serve` calls `recover` and `run_one` with no handler for `OperationalError` (`zhol/worker.py:44-47`). Compose has no `restart:` key. Logs from `worker`, `browser-worker`, and `collector`, redacted, end with `psycopg.OperationalError: consuming input failed: server closed the connection unexpectedly` during `SELECT ... FROM jobs ... FOR UPDATE SKIP LOCKED`. All three containers are `Exited (1)` since 2026-10-17. The database container itself was still healthy at audit time.
- Impact: A database restart, Docker Desktop sleep, or a server-side disconnect stops queue processing until a person starts the containers again. Jobs left `running` are recovered only while some worker process is alive, because `recover()` runs inside the worker loop. With every Docker worker down, that recovery does not run there. The LaunchAgent collector was still up and does call `recover()` on its own loop.
- Reproduction: Observed. Do not reproduce by killing the application database.
- Fix: `pool_pre_ping=True`, catch disconnects in `serve` and rebuild the engine, and set `restart: unless-stopped` on worker services.
- Regression test: A worker whose test database connection is closed returns to polling and claims the next job. It must not exit the process.

### P1-03 No backup or restore procedure

- Severity: P1.
- Component: operations. No script under `scripts/`, Compose, or `docs/` runs `pg_dump` or `pg_restore`.
- Evidence: Persistence is the Docker volume `pgdata` (`docker-compose.yml:118-121`) plus comments that say not to delete volumes. Search of the repository found no backup runbook.
- Impact: Losing the volume loses companies, snapshots, orders, and credentials. There is no tested restore.
- Fix: A scheduled `pg_dump` to a path outside the volume, a restore drill on a disposable database, and a written restore order that does not include `docker compose down -v`.
- Regression test: Restore a dump into an empty disposable database and assert `alembic_version` is `e5a9b3c20f41` and a known fixture row is present. Do not run this against the application database.

### P1-04 Yandex jobs skip the Chrome profile lock

- Severity: P1.
- Component: `zhol/api/admin_jobs.py`, `zhol/browser/profile_gate.py`, `parser_service.py`.
- Evidence: Google takes the lock (`admin_jobs.py:406-411`). Yandex falls through to `_run_maps` without it (`admin_jobs.py:412-413`). 2GIS takes the lock at lines 422-425. `_start_chrome` always calls `release_browser(profile)` (`parser_service.py:313-316`), which SIGTERMs and then SIGKILLs processes whose command line uses that `--user-data-dir` (`parser_service.py:173-199`).
- Impact: A Yandex job can start while a 2GIS or Google job holds the flock, then kill that Chrome. The flock does not cross the Docker volume and the macOS profile directory. The LaunchAgent script refuses to start if a Docker collector container is already running (`scripts/zhol-collector-macos.sh:541-544`). That check does not cover a Yandex job inside the same process family, and it does not cover two stacks pointed at one database.
- Fix: Wrap every browser mode, including Yandex, in `exclusive_chrome_profile()`.
- Regression test: A second collector that starts while the lock is held raises `JobDeferred` and does not call `release_browser`.

### P1-05 Map CAPTCHA ends the batch, and headless Chrome cannot finish a robot check

- Severity: P1 for remote deployment and for Google/Yandex. 2GIS on a graphical Mac is a different path.
- Component: `zhol/agents/scout/maps_selenium.py`, `zhol/agents/scout/selenium_collector.py`.
- Evidence: `_raise_if_blocked` raises `MapAccessBlocked` for captcha and access restriction (`maps_selenium.py:217-222`). The handler marks the batch `interrupted`, `retryable: False`, and returns (`maps_selenium.py:1000-1006`). 2GIS instead holds `awaiting_manual_verification` and waits on the same window. Headless and container runs are documented to close the window and wait for a machine with a display. The LaunchAgent plist is `LimitLoadToSessionType=Aqua` and `ZHOL_CHROME_HEADLESS=0`. Docker collector sets `ZHOL_CHROME_HEADLESS=1` (`docker-compose.yml:80`).
- Impact: A Google or Yandex robot check does not resume in the same browser. A headless server cannot show the check to an operator. Topology B cannot finish a live collect that hits a check.
- Fix: Use the 2GIS hold for maps, and treat headless as "stop and wait for a headed worker" rather than `retryable: False` with no operator path. Do not add a bypass.
- Regression test: A fixture page with the captcha marker leaves a maps batch in `awaiting_manual_verification` and does not open the public host.

### P1-06 Yandex search does not scroll, and pagination looks for links the saved markup does not have

- Severity: P1 for Yandex collection completeness. Live Yandex was not opened.
- Component: `zhol/browser/yandex/selenium.py:63-67`, `zhol/agents/scout/maps_selenium.py` page loop.
- Evidence: Page numbers are read only from `a.search-pagination__page[href]`. `tests/test_yandex_saved_selectors.py` expects `pageNumbers == []` for the saved search HTML. Discovery returns done when the next page number is absent. There is no feed-scroll path for Yandex. Google has one.
- Impact: On markup that matches the saved fixture, collection stops after page 1. A live infinite-scroll results list would do the same. This is confirmed for the fixture. It is a likely live failure, not a live measurement.
- Fix: Detect the end of a Yandex result list the way the current site actually pages, using a fixture that includes a second page, before any live run.
- Regression test: A two-page local fixture returns both pages. A one-page fixture still stops.

### P1-07 Google "feed complete" is a fixture attribute

- Severity: P1 for claiming a complete Google city collect. Collection itself can still stop by stall.
- Component: `zhol/browser/google/selenium.py:46-49`.
- Evidence: `done` is `feed.getAttribute("data-feed-complete") === "1"`. Live Google Maps does not set that attribute. The Python loop then ends on a stall of identical item counts or `MAX_SCROLLS`, with `complete` false.
- Impact: A live run can stop early and record an incomplete discovery while the batch still reaches `completed` when no retry ids remain (`maps_selenium.py:995-998`). Fixture tests that set the attribute do not show this.
- Fix: Treat stall as incomplete in the batch status, or detect the live end-of-feed node. Do not mark `completed` when `link_collection_complete` is false.
- Regression test: A fixture feed without `data-feed-complete` that stops growing is `interrupted` or explicitly partial, not `completed`.

### P1-08 Name and address can merge companies, and the address may come from another platform

- Severity: P1.
- Component: `zhol/agents/scout/match.py`, `zhol/agents/scout/identity.py`, `zhol/agents/scout/identity_store.py`, `zhol/agents/scout/maps_selenium.py`.
- Evidence: `verdict_for` returns `same` when phones overlap or when both name and address match (`match.py:65-66`). `relate` returns `same` even when `phone` is false, as long as the two sides are not in the "both have phones and they differ" case (`identity.py:65-72`). `_newest_text` walks every snapshot of the company and keeps the latest address, with no platform filter (`identity_store.py:214-224`). `_stored_profile` reads address and phones from the single newest snapshot of any platform (`maps_selenium.py:605-616`).
- Impact: A later Google or Yandex snapshot can supply the address used to attach the next 2GIS card, or the reverse. Distinct businesses that share a name and a short address can become one `companies` row. Platform ids are withheld unless a phone confirms the attach (`identity_store.py:258-263`), so the id table is stricter than the company link. Snapshots stay attached to the merged company.
- Fix: Match using only the snapshot of the platform being compared, and require a shared normalized phone before `same` when both sides have phones. Name plus address alone should be `ambiguous`.
- Regression test: A Google snapshot with a different street must not supply the address that attaches a new 2GIS firm. Two salons with the same name and compatible address and different phones stay two companies.

### P1-09 An incomplete Google snapshot can become the row the panel and Excel show

- Severity: P1.
- Component: `zhol/agents/scout/platform_rows.py`, `zhol/audit/bek.py`, `zhol/agents/scout/persist.py`.
- Evidence: Lists take the latest `card_snapshots` row per company where `platform` matches and `match_status = 'found'`, ordered by `collected_at`, then `id` (`platform_rows.py:107-121`). There is no filter on `status` or completeness. Live Selenium inserts `status="collected"` (`persist.py:273`). Bek import skips a platform only when a `verified` snapshot is newer than the audit timestamp (`bek.py:592-600`). It inserts rather than updates. `apply_google_repair` also appends and does not rewrite the old row (`google_card.py:69-103`).
- What is already safe: a 2GIS name is not copied into the Google sheet. `tests/test_google_completeness.py::test_incomplete_google_rows_stay_empty_and_are_marked` passed and checks that. An incomplete live Google card is rejected before insert (`maps_selenium.py:668-676`). History rows are not deleted.
- Impact: If an imported Google row has `match_status=found` and a later `collected_at` than a complete live Google row, the list and the Google Excel sheet show the incomplete name and URL. The live row remains in the table and is not what the operator exports. The guard does not consider `collected` live rows "newer data worth keeping".
- Fix: Prefer the latest snapshot that is complete for name and source URL, or skip import when any newer `collected` or `verified` snapshot exists for that platform. Never copy another platform's fields in.
- Regression test: Insert a complete Google snapshot, then an incomplete Google snapshot with a later `collected_at`. `list_platform_rows(..., "google")` still returns the complete name and URL. The incomplete row remains in history.

### P1-10 The release suite is red, and the downgrade tests no longer run

- Severity: P1 as a release gate. The four order failures are stale tests against a constraint that is correct. The two migration failures mean downgrade safety was not demonstrated.
- Component: `tests/test_e1014_migration.py:168`, `tests/test_manual_verification.py:525`, `tests/test_e72_yandex.py:289-315`.
- Evidence: Section 3. Disposable upgrade-to-head did succeed, because 970 database tests ran on a schema migrated to `e5a9b3c20f41`.
- Impact: A green-suite rule cannot be satisfied. The downgrade guards in `c3e7a1b84d02` and `d4f8a2c19e30` were not executed in this run.
- Fix: Point those tests at head, then downgrade one step and assert the guard. Give the second order in each cabinet test its own external id.
- Regression test: The six tests pass on a disposable database at head `e5a9b3c20f41`.

### P1-11 Partial collect is painted as success

- Severity: P1.
- Component: `web/src/collect.tsx:464-481`, `web/src/ui.tsx:80-86`, `zhol/api/admin_actions.py:361-377`.
- Evidence: If at least one source enqueues, the API returns HTTP 200 with both `jobs` and `errors`. The panel puts the error text into the same string as "Отдельных сборов: N" and `Feedback` renders any `message` with class `ok`. HTTP 4xx still goes through `readBody` and is shown as `.error`. A later job failure does not clear the green enqueue message.
- Impact: An operator can read a green line while one of the two sources was refused. The status pill can later say the batch failed, with the green line still on screen.
- Fix: If `errors` is non-empty, render that block as `.error` and do not use `.ok`. Clear the enqueue message when polled status is `failed`, `dead`, or `interrupted`.
- Regression test: A collect response with one job and one error shows the error style and does not show the success style.

### P1-12 Compose defaults are safe only by accident

- Severity: P1 for any copy of this Compose file onto a reachable host.
- Component: `docker-compose.yml`.
- Evidence: `POSTGRES_PASSWORD` defaults to `zhol` (lines 4-6) and the same default is embedded in `DATABASE_URL` (line 20). `ZHOL_2GIS_BROWSER_GRANT` defaults to `written-browser-retention-local` (lines 27 and 73), which `grant_scope()` accepts as a real grant (`zhol/agents/scout/browser_2gis.py:77-83`). `ZHOL_BROWSER_ENGINE` defaults to `1`. The web image command is `npm run dev` (`web/Dockerfile:9`). No service sets `restart:`. The running database password was not printed and was not compared to the default.
- Impact: An env file that leaves these unset turns on live 2GIS authorization and a known database password. The dev server is the admin UI. Workers do not come back after the crash in P1-02.
- Fix: Empty defaults for grants and the database password, fail startup when they are missing, serve `vite build` output from a static server, and add restart policies. Do not commit a filled `.env`.
- Regression test: Compose config with an empty env rejects startup. A unit test asserts the grant default in Compose is empty.

## 6. P2 and P3 findings

### P2-01 OpenAPI is public

`zhol/api/main.py:20` does not set `docs_url=None`. `/docs`, `/redoc`, and `/openapi.json` are unauthenticated. Admin calls still require a token. Fix: disable docs unless a dev flag is set.

Regression test: `/openapi.json` is 404 when the flag is off.

### P2-02 Local-access peer check treats every `172.*` address as private

`zhol/api/local_access.py:46-49`. RFC1918 is `172.16.0.0/12`, not every address that starts with `172.`. Host `api` is a local host name. This matters only when `ZHOL_LOCAL_ACCESS=1`, which the offline overlay sets (`docker-compose.offline.yml:8`). The default Compose file does not. Fix: parse the address and allow only loopback plus `172.16.0.0/12`.

Regression test: `172.15.0.1` is rejected and `172.16.0.1` is accepted when the rest of the local gate matches.

### P2-03 One Chrome profile for every platform

`parser_service.py` uses a single `--user-data-dir`. The lock serializes collectors. It does not separate 2GIS, Yandex, and Google cookies. Docker uses one volume, `collector-chrome`. Fix: one profile directory per platform.

Regression test: Google and 2GIS jobs resolve different profile paths.

### P2-04 Screenshots are kept forever

`zhol/browser/evidence.py` validates files. No TTL or purge was found under `zhol/`. Export can still carry a screenshot path (`zhol/export/mailing.py`). Fix: a retention job and a rule that exports do not include local paths.

Regression test: A file older than the retention window is removed and the snapshot column becomes null.

### P2-05 Collector image runs root Chromium with `--no-sandbox`

`Dockerfile.collector` has no `USER`. `parser_service.py` adds `--no-sandbox` when `ZHOL_CHROME_CONTAINER=1`. Fix: a non-root user. Keep the sandbox flag only if the runtime cannot use a user namespace, and document that.

Regression test: The image config `User` is not root.

### P2-06 `.gitignore` does not cover runtime data, and it hides `.env.example`

`.gitignore:4-5` ignores `.env.*`, so `.env.example` is not tracked (`git ls-files` reported it untracked and ignored). `data/admin/` is not ignored. Exports and saved HTML can be committed by mistake. `.env` itself is ignored. Fix: ignore `data/admin/` and chrome profiles, and force-add `.env.example` only.

Regression test: `git check-ignore` accepts `data/admin/exports/x.xlsx` and does not ignore `.env.example`.

### P2-07 API errors return exception text

`zhol/api/admin_actions.py` returns `str(exc)` after stripping the admin-root path prefix. `local_orders.py:39-42` returns `str(exc)` with no strip. Fix: a stable code plus a short Russian sentence. Log the exception server-side.

Regression test: A raised `RuntimeError("/secret/path")` is not present in the JSON body.

### P2-08 Admin bearer token is in `sessionStorage`

`web/src/api.ts:3-24`. Any XSS in the panel can read it. The cookie path is `HttpOnly`. Fix: use the cookie session for local use and avoid storing the bearer token in web storage.

Regression test: After local sign-in, `sessionStorage` has no token key.

### P2-09 `SECRETS_KEY` is unused

`.env.example` lists it. No reader exists under `zhol/`. Callers can store bytes that are not encrypted. Fix: encrypt with `SECRETS_KEY` before insert, or reject ciphertext the server cannot identify as encrypted.

Regression test: Inserting a credential without the key fails. A round trip decrypts only with the key.

### P2-10 Platform Excel escaping is narrower than the mailing export

`zhol/export/mailing.py:24` prefixes `=`, `+`, `-`, `@`, tab, and CR. `zhol/agents/scout/platform_rows.py:184-192` prefixes only the first four. Fix: use one helper.

Regression test: A phone or name that starts with a tab is stored with a leading apostrophe in the platform workbook.

### P2-11 Fixture batch claim has no row lock

`zhol/agents/scout/batch.py:172-189` selects pending items and updates the first one without `FOR UPDATE SKIP LOCKED`. Selenium commits do lock the batch. Two workers on a fixture or discovery batch can claim the same item. Fix: lock the item row.

Regression test: Two concurrent claims return different item ids.

### P2-12 A collect transaction stays open across page reads

`zhol/agents/scout/jobs.py:188-205` calls `collect_company` inside `session.begin()`. Fix: read pages first, then open a short transaction to insert.

Regression test: A slow fixture read does not hold a row lock for the whole read. Assert with a lock timeout in the test database.

### P2-13 Failed snapshots are inserted again on retry

`find_reusable` in `zhol/agents/scout/persist.py` accepts only `collected` and `verified`. A `failed` row for the same collection key is ignored, so a retry appends another row. There is no unique index on the JSON collection key. Fix: a unique index on `(company_id, platform, collection key)` or reuse the failed row.

Regression test: Two inserts with the same key yield one snapshot.

### P2-14 Many companies may have a null 2GIS id

`companies.id_2gis` is `UNIQUE` and nullable (`zhol/db/models.py:35`). PostgreSQL allows multiple nulls. Google-only and Yandex-only companies depend on soft matching. That matches migration `c3e7a1b84d02` and is still how duplicates appear when matching fails open. Fix: a partial unique key on normalized name, phone, and city for rows with null `id_2gis`, or require a platform id before insert.

Regression test: Two Google companies with the same place id cannot be inserted. Two with different place ids can.

### P2-15 `card_snapshots.status` has no check constraint

The model checks `platform` and `match_status` only (`zhol/db/models.py:80-85`). Application code uses `collected`, `verified`, `rejected`, and `failed`. A raw write can store anything, and P1-09 then treats it as a candidate for "latest". Fix: a check constraint matching the transition table.

Regression test: Inserting `status='nope'` fails. Inserting `collected` succeeds.

### P2-16 A hung handler keeps its lease

The heartbeat thread extends `run_after` every lease/3 (`zhol/jobs/queue.py:128-144`). `recover_stale` only sees rows whose deadline has passed (lines 188-195). A stuck Chrome therefore stays `running` and holds the profile. The S9 watchdog described in `docs/decisions.md` is not what the worker does today. Fix: a maximum runtime that stops the heartbeat and fails the job.

Regression test: A handler that does not return within the cap becomes `failed` even while the heartbeat thread is alive.

### P2-17 `/health` does not touch the database, and most services have no healthcheck

`zhol/api/main.py:28-30` returns `{"status":"ok"}`. Compose healthchecks exist for `db` and `collector` only. Fix: `/health` runs `SELECT 1` with a short timeout. Add healthchecks for `api` and `worker`.

Regression test: When the test database is down, `/health` returns 503.

### P2-18 Yandex is collectable and invisible in the source board

Tabs are only 2GIS and Google (`web/src/companies.tsx:310-313`). The API returns 404 for any other platform (`zhol/api/admin.py:156-157`). `PLATFORMS` in `platform_rows.py:23` is the same pair. Yandex rows still exist on the company page. Fix: add a Yandex sheet that reads only Yandex snapshot fields.

Regression test: A Yandex snapshot does not appear on the 2GIS or Google sheet, and it does appear on a Yandex sheet.

### P2-19 The Sources checkboxes do not match `browser_yandex`

`setup.py:19` sets `AVAILABLE` to only `2gis`, so Yandex and Google are `available: false` in the sources array (line 214). The mode radio still sends `platforms: ["yandex"]` (`web/src/collect.tsx:764-769`). The checkbox panel can show Yandex disabled while the request includes it. Fix: drive the checkboxes from `browser_sources`, not from the fixture `AVAILABLE` set.

Regression test: With a Yandex grant in the test env, the Yandex control is enabled and the payload platform is `yandex` only.

### P2-20 The panel still says live collection is off

`web/src/pages.tsx` and the banner hard-code a "no live collection" line while `collection_setup` can report browser modes as available. Fix: render `browser_2gis.available` and the map grants from `/scout/setup`.

Regression test: When the setup payload says the browser source is available, the banner does not say it is off.

### P2-21 A failed refresh drops a job from the panel

`refreshJobs` in `web/src/collect.tsx` uses `.catch(() => null)` and then filters nulls. A transient 500 removes the running job from the list with no error. Fix: keep the previous job and show the error.

Regression test: A rejected job GET leaves the previous card on screen.

### P2-22 Maps collectors do not use the 2GIS pace delay

2GIS defaults to `wait_turn`. The maps admin path calls `_wait` with `(0.0, 0.0)` when `delay_range` is omitted. Combined with P1-06, a live Yandex or Google run has no delay between cards. Fix: pass the same pace helper.

Regression test: With the pace file set, two map card reads are at least the configured gap apart. Use a fake clock.

### P2-23 Selenium does not read HTTP 429

`_blocked_kind` treats visible text `limited` as an access restriction (`maps_selenium.py:204-214`). It does not read the HTTP status. Catalog API retries in `live_2gis.py` are a different client. Fix: if the driver can see a 429, stop the batch as retryable and do not scrape harder.

Regression test: A fixture response marked 429 sets `retryable: true` and does not request the next page.

### P2-24 2GIS Selenium snapshots leave audit fields empty

Root `card.py` collects name, address, and contacts. Rating, photos, prices, and review counts stay null with reason `not_shown`. That matches `tests/test_selenium_collector.py`, which passed. It is not a full card audit as described in `AGENTS.md` section 5.1. Fix: either fill those fields from the card DOM or label the batch as contacts-only in the panel.

Regression test: A fixture card with a visible rating stores that rating. A fixture without one stores null, not 0.

### P2-25 `collection_batch_items.id_2gis` stores Google and Yandex ids

`maps_selenium.py` writes `place.external_id` into the column named `id_2gis`. Uniqueness is per batch, so it works, and reports that assume a 2GIS firm id will be wrong. Fix: rename to `external_id` plus `platform`, in a new migration. Do not rewrite old rows in place without a copy.

Regression test: A Google batch item round-trips the place id in the new column and does not require a 2GIS id.

### P2-26 Upgrade to the platform-id unique index does not dedupe first

`alembic/versions/c3e7a1b84d02_nullable_company_id_2gis.py` creates `uq_company_platform_ids_platform_external`. If duplicate `(platform, external_id)` rows already exist, upgrade fails. The application database is already at head, so this already succeeded there. It remains a risk for any other database restored from a pre-index dump. Fix: abort with a count of conflicting ids before creating the index, and do not delete rows automatically.

Regression test: A disposable database with two rows sharing an external id fails upgrade with that count, and both rows remain.

### P3-01 `opened` in the maps batch loop is never incremented

`zhol/agents/scout/maps_selenium.py` near the card loop. Harmless.

### P3-02 Hidden admin screens

`web/src/App.tsx` routes orders, content, listings, tasks, and reports, and the nav does not link them.

### P3-03 Sign-in copy disagrees with storage

The login text says the token is not kept in the page. `setToken` writes `sessionStorage` (`web/src/api.ts:22-23`).

### P3-04 "Already in the database" increments the duplicate counter

`selenium_collector.py` and maps `_commit_place_locked` increment `duplicate_companies` when the company already exists. That is not a duplicate inside the current search.

### P3-05 Cookie `secure=False`

`local_access.py:127-134`. Appropriate for localhost HTTP. Wrong if the panel is later served on plain HTTP beyond localhost.

### P3-06 Local sessions are an in-memory dict

`local_access.py` `_sessions`. A second API worker would not see them. Restart logs everybody out. Fail-closed, so this is debt.

### P3-07 City collector knows only Almaty

`maps_selenium.py:197-201` raises unless the city folds to `алматы`. Consistent with the current product. A second city will refuse rather than search the wrong place.

### P3-08 Start script does not start the scheduler, and the stop script stops it

`Запустить Zhol.command:80` starts `db api web worker browser-worker`. `Остановить Zhol.command:17` also stops `scheduler`. Periodic enqueue does not run unless a person starts it. Workers still call `recover()` themselves while they are up.

## 7. Security findings

Confirmed controls:

- Admin routes use `require_admin` (`zhol/api/admin.py:44-63` and router line 90). Empty `ZHOL_ADMIN_TOKEN` returns 503.
- Comparison is SHA-256 then `hmac.compare_digest` (`admin.py:66-70`).
- Mutations require `X-Zhol-Request: admin`.
- Admin read sessions set the transaction read-only (`admin.py:34-37`).
- No CORS middleware. Browser cross-origin reads are denied by default.
- Catalog HTTP is limited to `catalog.api.2gis.com` and an allowlisted path (`live_2gis.py`).
- Browser navigation refuses public hosts when the run is pinned to loopback (`browser_ops.py`, covered by `tests/test_browser_navigation_guard.py`, which passed).
- Excel formula prefixes are escaped, with the gap in P2-10.
- `zhol/redact.py` and `safe_error` strip secrets from job errors. This audit did not print tokens from `.env` or LaunchAgent.

Findings are P1-01, P1-12, P2-01, P2-02, P2-03, P2-04, P2-05, P2-06, P2-07, P2-08, P2-09, P3-05, and P3-06.

Dependency CVE scan: **not run**.

## 8. Data integrity findings

| Question | Result |
|---|---|
| Company identity | Primary key `companies.id`. Optional unique `id_2gis`. Multiple nulls allowed (P2-14). |
| Platform id uniqueness | Partial unique `(platform, external_id)` where `external_id` is not null, since `c3e7a1b84d02`. Primary key is `(company_id, platform)`. |
| Cross-platform matching | Name, address, phone, and platform refs. See P1-08. |
| Ambiguous matches | `relate` / `choose` can return `ambiguous` and the maps commit rejects the card (`maps_selenium.py:700-702`). |
| Snapshot history | Inserts. Bek import and Google repair do not update the old row. |
| Verified versus collected | Audits prefer verified. Platform lists do not (P1-09). |
| 2GIS and Google sheets | Separate. A passed test asserts a Google name is absent from the 2GIS sheet (`tests/test_platform_lists.py`). Yandex is omitted from both (P2-18). |
| Incomplete Google overwrite of live bytes | The live row is not updated. It can disappear from the list (P1-09). |
| Batch idempotency | Unique `(batch_id, id_2gis)` per batch. Selenium locks the batch. Fixture claim does not (P2-11). |
| Failed cards | Status `failed` plus reason. Retries append (P2-13). |
| Concurrent jobs | Queue claim is `SKIP LOCKED`. Profile lock is advisory and skipped for Yandex (P1-04). |
| Provenance | `field_sources` per field, plus `_collection`. Matching can ignore platform (P1-08). |

### Migration chain

Linear. One head. No merge revisions.

| Revision | Parent | What upgrade does | Downgrade risk |
|---|---|---|---|
| `9c9c10b4e4ff` | none | Initial schema. `id_2gis` NOT NULL | Drops all tables |
| `b7e4c1a2d3f0` | `9c9c10b4e4ff` | Collection batches | Drops batch tables |
| `c8e41a9b2d10` | `b7e4c1a2d3f0` | Manual mode, pause, stop, progress | Fails if those values exist. No pre-check |
| `d9e75b1c4a20` | `c8e41a9b2d10` | Listing plans | Drops the table |
| `e77b4d8c1a02` | `d9e75b1c4a20` | Listing submissions | Drops the tables |
| `f3b8c1d4e5a6` | `e77b4d8c1a02` | Source mode `live_2gis` | Fails if such rows exist. No pre-check |
| `a10e5c4d6b21` | `f3b8c1d4e5a6` | Source mode `browser_2gis` | Same |
| `c3e7a1b84d02` | `a10e5c4d6b21` | Nullable `id_2gis`, platform-id unique index | Guarded if any `id_2gis` is null. Not re-tested this run |
| `d4f8a2c19e30` | `c3e7a1b84d02` | Status `awaiting_manual_verification` | Guarded. Not re-tested this run |
| `e5a9b3c20f41` | `d4f8a2c19e30` | Source modes `browser_yandex`, `browser_google` | Guarded by a count. Guard not executed this run |

Application database revision, read-only `SELECT version_num FROM alembic_version`: `e5a9b3c20f41`. No migration was applied.

Downgrade of the middle check constraints can fail on live rows. That failure does not delete rows. It is still a brittle rollback.

## 9. Deployment risks

### Topology A, local macOS

Visible Chrome, LaunchAgent `kz.duna.zhol.collector`, profile under `~/Library/Application Support/Almaty2GISCollector/ChromeProfile`. The script refuses to start when the Docker `collector` container is running. This is the only topology that can show a robot check to a person. It was running during the audit. Live collection was not executed.

Depends on a graphical session: LaunchAgent `Aqua`, `osascript` to raise Chrome, manual verification, headed Playwright cabinet pilots. None of these work unchanged on a server without a display.

### Topology B, remote Linux / Docker

`docker compose --profile collector` builds `Dockerfile.collector`, sets headless Chromium, `shm_size: 1gb`, and a heartbeat healthcheck. The main image has no browser. Playwright is an optional extra and is not installed in either image.

What does not work unchanged on a headless server:

- Robot-check confirmation.
- The LaunchAgent and `osascript`.
- Any flow that assumes `ZHOL_CHROME_HEADLESS=0`.
- Google and Yandex collects that hit a check (P1-05).
- A complete Yandex result list (P1-06) and a proven-complete Google feed (P1-07).

### Other deployment gaps

- `Запустить Zhol.command` runs `alembic upgrade head` on the application database every start. That is the documented procedure. This audit did not run it.
- Scheduler is not in that start command (P3-08).
- Docker workers are already stopped (P1-02).
- API health is shallow (P2-17).
- No memory or CPU limits. The collector profile sets `shm_size` only.
- No backup (P1-03).
- Environment separation is "whatever is in `.env` plus Compose defaults" (P1-12). Offline overlay adds local access and fixture mounts. It does not add a second database.

## 10. Readiness matrix

Fixture success is not live success.

| Area | Result | Why |
|---|---|---|
| 2GIS collection | PARTIAL | Fixture Selenium tests passed. Pause, resume, stop, checkpoint, and CAPTCHA hold exist in code. Live site not contacted. Docker collector is exited. LaunchAgent was up and was not exercised. |
| Google Maps collection | PARTIAL | Fixture completeness tests passed. Live feed end detection is a fixture attribute (P1-07). CAPTCHA does not resume (P1-05). Live site not contacted. |
| Yandex Maps collection | FAIL | Profile lock skipped (P1-04). Pagination stops on the saved markup (P1-06). No platform tab (P2-18). Grant defaults to closed, which is correct. Live site not contacted. |
| PostgreSQL data integrity | PARTIAL | Head matches. Append-only snapshots and platform-id uniqueness are real. Matching and "latest snapshot" can hide or merge the wrong card (P1-08, P1-09). |
| Job queues | FAIL | Claim and lease code is sound and covered by passing tests. The three Docker workers are crashed and will not restart (P1-02). |
| Authentication and security | PARTIAL | Admin token gate is real and loopback-bound. Local order routes are open (P1-01). Compose defaults are weak (P1-12). CVE scan not run. |
| React admin | PARTIAL | Typecheck and production build passed. Partial collect renders as success (P1-11). Yandex source UI disagrees with the payload (P2-19). Panel was not clicked. |
| Excel export | PARTIAL | Platform workbook tests passed and keep 2GIS and Google apart. Latest-row rule can export an incomplete Google card (P1-09). Tab escaping is incomplete (P2-10). |
| Local macOS operation | PARTIAL | LaunchAgent was running. Visible Chrome and manual verification are implemented. Not exercised. Docker workers for the other queues are down. |
| Remote Docker deployment | FAIL | Headless robot checks, worker restart, backups, dev server, default grant, and default database password. |
| Backup and restore | FAIL | No procedure and no drill (P1-03). |

## 11. Minimal remediation plan

Do these before any deployment announcement. Do not start with feature work.

1. Bring Docker workers back only after `pool_pre_ping` and a restart policy exist. Until then, leave them stopped so they do not fight the LaunchAgent. Confirm with a disposable database, not by bouncing the application database.
2. Add a `pg_dump` path and restore it once into a new empty database.
3. Put Yandex behind `exclusive_chrome_profile()`.
4. Stop treating name plus address as `same`, and stop reading another platform's snapshot as the match profile.
5. Make platform lists skip an incomplete Google snapshot when an older complete one exists. Teach Bek import to skip when a newer `collected` or `verified` snapshot exists.
6. Render partial collect errors in the error style.
7. Repair the six failing tests so the suite is green on head `e5a9b3c20f41`, including one real downgrade on a disposable database.
8. Remove the Compose default grant and the default database password. Serve the built admin assets.
9. Only after that, run one supervised live pilot on the Mac for 2GIS, then Google, with a person present for a robot check. Do not treat that pilot as done until a second page of Yandex results is proven on a fixture.

Estimated effort: about 8 to 12 engineer-days for items 1-8, plus a separate live pilot that this audit did not attempt. Yandex live pagination may add more once a current results page is captured legally as a fixture.

## 12. Go / No-Go recommendation

**No-Go.**

The schema head is the expected revision, admin authentication exists, 2GIS and Google sheets do not copy each other's fields, and 970 isolated tests passed. That is not enough to deploy.

Docker queue workers are already dead after a database disconnect. There is no backup. Yandex can kill another collector's Chrome. Matching can join the wrong companies. An incomplete Google import can become the exported row. The test suite is red. A headless server cannot finish the collection the Mac LaunchAgent is built for.

Local supervised 2GIS collection on the running LaunchAgent may continue as an operator workflow. It should not be described as a released service, and it should not be moved to a remote host by reusing this Compose file unchanged.
