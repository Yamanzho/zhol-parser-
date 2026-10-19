# Backlog

Open after S0:

- R6 (Timur): GPT and Gemini model ids, prices of all models with source link and date, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GOOGLE_API_KEY`. Until then `api` does not start and `council-dev` exits with the configuration errors.
- `council-dev --stage S0` with real models and the second-AI code review (S0 acceptance #11). Both need R6 or a manual review by
  the checklist `council/checklists/dev-stage.md`.
- Telegram: `TELEGRAM_BOT_TOKEN` and `RESPONSIBLE_TG_CHAT_ID` from Timur. `notify()` is tested with a fake transport only.
- Queue retry count (3 runs vs 4 runs), see `docs/decisions.md`, Queue.
- `eval --prompt <name>`: runner for prompt golden sets arrives with the first prompt golden set (S1). Now exits 2.
- Native-speaker gate for Kazakh texts (`native_first`, R8).
- Checklist item numbering when two checklists are merged (Kazakh uplift, `content`).
- Handlers for scheduled jobs (`manager.*` S9, `review.weekly_audit` / `review.monthly_quality` S9). The worker loop now
  runs, but it claims only `snapshot.verify`, `scout.collect` and `review.snapshot_recheck`. Scheduled jobs stay queued.
- Subscription schedule: `reviews.collect`, monthly `invoice.issue`, `invoiced` → `active` at `period_start` (S8).
- `gaps` of imported audits are empty; gap list and code-computed discrepancies are S2.
- How `source/base-contacts.csv` is loaded: `import-contacts` (S1 batch 1) creates missing non-etalon companies with `source=base_550_contacts` and does not write snapshots. `import-etalon` still refuses that file.

Open after S1 batch 1:

- `auditor.audit` is not enqueued after `snapshot.verify` (S2).
- `scout-compare` against a fresh live collection of the 50 reference salons, and the 90% / 85% acceptance thresholds.
- `eval --prompt` now runs `scout-extract` and `scout-match` offline. Other prompt names still exit 2.

Open after S1 batch 2:

- Approval before Yandex, Google, or the 50-company 2GIS run.
- Screenshot review for `scout_fields` and the `category` field of `scout-extract` need a model (R6). The offline runner reports them as unsupported.
- The `match` review point is still a human task when `same` has no shared phone. Paid model calls stay off.
- Playwright and Chromium are not in the Docker image. The 3-company pilot runs on the host.
- `worker` and `browser-worker` claim only the three Scout jobs. Scheduled `manager.*` and `review.*` jobs stay queued.

Client scope 11.10.2026, kept unused (do not delete, do not build next):

- Texer observation text, one-page PNG, and the observation council. Bek writes the audit. `score()` and empty-field lists stay.
- Syntony invoice execution, Kaspi, subscription billing, the manager SLA agent, and a full admin CRM. E9.2 adds local fixture, manual, and offline actions. Live cabinets and Bek stay closed.
- Architecture §10 Bek routes. Local order intake and the admin panel are the only extra HTTP routes. E9.2 adds action routes under `/api/v1/admin`. They are not Bek routes.
- `reports.xlsx_path` is the completion workbook, not the mailing file.

E1 Excel export is implemented: `python -m zhol.cli export-mailing --out data/export/mailing.xlsx`.
E2 collected the two remaining reference 2GIS cards in one visible browser. Report: `docs/pilot/e2-2gis.md`.
E3 collected public Yandex cards for Badi and Velara and did not store Google salon cards. Report: `docs/pilot/e3-maps.md`.
E4 adds `scout-batch` for a fixture catalog with resume and Excel. Report: `docs/pilot/e4-batch.md`.
E4.1 reads saved search pages and a manual browser export. Live city and category discovery stays closed, including 2GIS catalog extraction. Report: `docs/pilot/e4.1-discovery.md`.
E5 records a local test order with materials and owner authorization. Readiness does not start a listing edit, and Bek, payment, and WhatsApp stay out. Report: `docs/pilot/e5-orders.md`.
E6 prepares text and illustrative images from approved facts and does not publish them. Report: `docs/pilot/e6-content.md`.
E7.1 plans authorized listing creates and updates and dry-runs them. It does not submit a form. Unfinished E6 content does not block approved facts. Report: `docs/pilot/e7.1-listing.md`.
E7.2 prepares authorized Yandex Business field edits in a supervised browser session and stops before Save. It does not publish. Report: `docs/pilot/e7.2-yandex.md`. Submitting the form remains a later batch.
E8 writes a completion report from stored evidence. A matching snapshot is not a live check. Bek, payments, and cabinet submission stay closed. Report: `docs/pilot/e8-completion.md`.
E9.1 adds a local read-only web panel over stored records. It does not submit listings, call Bek, or run live discovery. Report: `docs/pilot/e9.1-admin.md`.
E9.2 connects that panel to fixture and manual scout batches, Bek workbook import, local orders, offline content, dry-run listing plans, offline submission simulation, and completion reports. It does not open a live cabinet or call Bek. Report: `docs/pilot/e9.2-admin.md`.
E9.3 runs scout batches on the existing main worker and lets the panel prepare, approve, and simulate each platform separately. Live cabinets and Bek stay closed. Report: `docs/pilot/e9.3-admin.md`.

Fields the export leaves blank until a later collection stores them:

- District, email, branch name (unless the 2GIS snapshot has `name`).
- Description and opening hours on 2GIS, Yandex, and Google.
- Google photo count on the 50 reference rows. The dictionary key `photos_count` exists. The live Google search did not show these two salons, so those cells were not filled from a new snapshot.
- Platform category on the 50 reference rows. The reference file has niche `салон`, which stays unclassified because it is ambiguous.
- Services, except Badi. The Yandex price block stored four named services on that company. Velara's page did not list prices.
