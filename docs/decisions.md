# Decisions

Open items (not resolved here):

- Architecture §13 uses `Asia/Almaty`. S0.2 requires an injectable UTC-aware clock.
  `timeutil.now()` currently returns UTC-aware datetimes. Zone policy is deferred.
- R6: GPT/Gemini model IDs and prices are unset until logged from official price lists.
- Architecture §3 uses `bigserial`. Models use PostgreSQL `IDENTITY` (equivalent bigint PK). Not a business-rule change.
- `transition()` enforces the §4 graph only. Invoice/package/access predicates stay with callers (S3+).
- §4.2 names `intake_missing` and `report_ready` on two edges; payload is `{order_id}` until `docs/syntony-contract.md` (S3).
- §4.3 has no `human` path. Setup-only `human` is not applied to subscription orders.
- Concurrent updates use `UPDATE ... WHERE stage = current` (no version column in §3).

Queue (S0.5), open for Timur:

- Retry count. Architecture §5: "3 попытки с паузой 1, 5, 30 минут". S0 acceptance #6: "задача падает 3 раза → dead".
  Read together, 3 failures end in `dead`, so only the 1 and 5 minute pauses are used. Implemented `MAX_ATTEMPTS = 3`,
  `RETRY_DELAYS = (1, 5, 30)`. Option B: 4 runs, all three pauses used, acceptance #6 reads "3 retries". Needs a choice.
- `dedup_key`. §3 makes it `unique` for every row; the comment says "одна живая задача". To allow a new job after the old one
  finishes, `dedup_key` is set to `NULL` when a job becomes `done` or `dead` (no schema change). The job row, type, payload and
  last error stay. Option B: a partial unique index on live statuses (new migration).
- Leases are not in the spec. A crashed worker would leave a job `running` forever. While `running`, `run_after` holds the lease
  deadline (default 30 minutes); `recover()` counts an expired lease as a failed attempt. `attempts` is the claim token, so a worker
  whose lease expired cannot complete or fail the job. Who calls `recover()` periodically (scheduler or `manager.sla_scan`) is S9.
- Dead job: `human_tasks.reason = 'job_dead'`, `object_type = 'job'`. The reason value is not named in the spec.
- `last_error` is redacted (API keys, bot tokens, bearer tokens, `password=`/`token=` values) and cut to 2000 characters.

Guards (S0.6):

- `forbidden_phrases()` matches the literal list from AGENTS.md 5.2, `prompts/auditor-observations.md` and S0 step 10, whole words,
  case-insensitive, `ё` = `е`. "Обещать рост трафика или позиций" (AGENTS.md 5.2) has no fixed wording; only "рост продаж",
  "увеличим", "в топ", "гарантируем" are caught by code, the rest stays with the `observations` review point.
- `kzt()` accepts whole non-negative tenge only (`amount_kzt` is `int` in §3) and formats as `30 000 ₸` with ordinary spaces, as in AGENTS.md 4.

LLM gateway and configuration (S0.7-S0.8):

- R6 still open. `config/models.yaml` lists the three Claude ids from architecture §6 with `null` prices; GPT and Gemini entries are
  commented-out templates. `check_startup()` rejects the config, so `api` exits at startup (compose shows `api exited (3)`).
  This is intended until Timur supplies GPT/Gemini ids, prices with source link and date, and the three API keys in `.env`.
- The pinned `anthropic` SDK has no `temperature` argument in `messages.create`; Claude calls ignore the prompt temperature.
  OpenAI and Gemini use it.
- A review member written as a family (`gpt`, `gemini`) must match exactly one model of that family in `models.yaml`; two GPT models
  make the config invalid, so the reviewer is never chosen silently.
- Points and prompts that receive images (`scout_fields`, `applied`, `content`, `scout-extract`) are listed in code and require
  `vision: true`. The spec has no field for this.
- The schema retry happens inside `llm.run()` (one retry with the error text, architecture §6). The review engine adds one more
  attempt per participant on top of that (03 §6.1 "один повтор, затем исключён").
- pydantic bumped 2.11.9 to 2.14.0: `google-genai` 2.29.0 requires pydantic >= 2.12.5.
- `config/scoring.yaml` and other S0 config files are not part of this batch (S0.13).

Review engine (S0.9):

- `review.run()` takes, besides the documented arguments, `author_prompt` (needed for the fix loop and double modes),
  `author_input` and `compare` (double modes), `code_check`, `escalate` (change_request uplift, the caller decides `nap`/`more_than_5`)
  and `language`.
- Kazakh text is detected by Kazakh-specific letters or `language="kk"`; the point becomes a council with `kazakh_text` members and
  both checklists. Both checklists number their items from 1, so an `unverified` item number may refer to either list; a main item in
  either list sends the work to a human. Needs a numbering convention for checklists.
- The native-speaker gate (`native_first: 20`, R8) is not implemented yet; only `human_first_blocking` creates a human task.
- Secretary output that is not an exact partition of issue ids is retried once, then each issue becomes its own group
  (flag `secretary_fallback` in `council_runs.groups`). Votes are counted the same way.
- Double modes: any discrepancy from `compare` gives `return` with reason `discrepancy` and no fix loop; the caller turns it into a
  question to the client (intake) or "no complaint" (complaint).
- Code check runs first in every round. Blocking code issues give `return` with reason `code_check` and no model call;
  a long dash is a minor note (AGENTS.md 12 forbids it in client texts, but 03 does not set its weight).
- `human_tasks.reason`: `review_human` for verdict human, `review_human_first` for the blocking first-N gate. Not named in the spec.
- Point `applied` has `author_family: none`; stored as `'none'` in `council_runs.author_family` (column is not null).

Reference import and scoring (S0.10-S0.11):

- The etalon has only the date of viewing. `collected_at` and `field_sources.seen_at` are 2026-09-30 00:00 Asia/Almaty;
  `дата_просмотра` for `score()` is the Almaty date of `collected_at`. Almaty is a fixed UTC+5 offset (no DST since 2024).
- `field_sources.read_by = 'manual'` for imported snapshots (§3 lists `code|model`); each source also keeps `source` and `csv_column`.
  Keys of 04 §1.1-1.3 without a CSV column (`category`, Yandex/Google `name`, `address`, `phones`) are stored as `null`.
- Only an empty CSV cell becomes `null`. The CSV has explicit `0` for not-found cards (Yandex counts for 4 «не найдена» rows,
  `google_отзывов` for 28 rows); they are kept as `0` because the dataset says so. For new collection (S1) a not-found card has `null`.
- Google has no URL in the CSV: no `company_platform_ids` row for Google. Yandex `external_id` is the number in `/org/<slug>/<id>/`.
- Import creates one `draft` audit per company: `score_total` from the ported `score()`, `derived` by 04 §1.4,
  `discrepancies` = `[{"legacy_text": ...}]` or `[]` when the cell is empty, `gaps = []` (gaps are S2). `score_groups` keys are
  `reviews`, `prices`, `photos`, `contacts`.
- Idempotency: company by `id_2gis`; snapshot by company, platform, `collected_by = manual_import` and `collected_at`; audit by
  company and snapshot ids. A second import adds nothing. An existing company with another `source` is not changed.
- `score()` reads `2гис_доля_ответов`; the adapter gives it `derived.reply_share` (= replies / shown). On all 50 rows this equals
  the CSV column within rounding and never crosses a threshold.
- `config/scoring.yaml` holds the literals of `score()`; the port is compared with the original function on the 50 rows and on
  3000 generated boundary rows. Methodology §2 (version 1) tables differ from the 1.1 code (for example photos 7 vs 15 points);
  the code and `балл_итого` are taken as the source, as AGENTS.md 5.2 says.

Scheduler (S0.12):

- Schedule times are Asia/Almaty (architecture §13). Naive `--now` stays UTC, as decided in S0.2: `--now 2026-11-02T09:00` is
  14:00 in Almaty. Use `--now 2026-11-02T09:00+05:00` for local time.
- Slots: `manager.sla_scan` every 15 minutes; `manager.digest` from 09:00 once per day; `review.weekly_audit` once on Monday;
  `review.monthly_quality` once on the 1st. The spec gives no time for the weekly and monthly jobs: they fire from 00:00.
  A missed slot is caught up by the next tick of the same day, older slots are not replayed.
- One job per type and slot ever (checked on `payload.slot`, all statuses), plus `dedup_key` and a transaction advisory lock against
  parallel ticks. Every tick first runs `recover()` for expired leases.
- Handlers for these job types are S8-S9. Until then the jobs stay `queued` (worker is idle). Subscription jobs (`reviews.collect`,
  monthly `invoice.issue`, `invoiced` → `active`) are S8.

Queue heartbeat (S0.9 part E):

- `run_one()` extends the lease from a background thread every `lease/3` while the handler runs, only for the same attempt
  (`StaleJob` otherwise). A handler that hangs forever keeps its lease; the S9 SLA scan is expected to catch it.

Scout batch 1 (S1), open for Timur:

- The package is `zhol/agents/scout` plus `zhol/browser/{twogis,yandex,google}/public.py`, as in architecture §2.
  The batch note said `zhol/scout`. The architecture path is the one implemented.
- `transition()` now also accepts `CardSnapshot`: `collected` → `verified` | `rejected` | `failed`, and `verified` → `rejected`.
  Orders are unchanged. A failed page is a new row with status `failed`. `match_status` stays one of the three dictionary values.
  The page-load failure is `fail_reason`, not a fourth match status.
- Retry idempotency uses `field_sources._collection.key` (no new column). A finished job gets a new key and therefore a new snapshot.
  Rows already stored are not updated. `company_platform_ids.access_status != none` is left as it is, and an external id that
  already belongs to another company is not moved (`human_tasks.reason = platform_id_conflict`).
- `same` without a shared phone is not verified and does not write `company_platform_ids`. A human task `match_needs_review`
  stands in for the `match` review point until a model gateway is configured (R6). Model-read fields on a 10% recheck open
  `scout_fields_unavailable` for the same reason. No council row is written without a gateway.
- `snapshot.verify` does not enqueue `auditor.audit` or the other S2 jobs.
- Name matching ignores case, quotes and the words салон, студия, beauty, studio, then requires the remaining tokens to be equal.
  A transliteration table is not included: a phone match still counts as `same`. Address matching uses street type and house number.
  Two `same` candidates are `ambiguous_match`, not a merge.
- Live fetch is refused. Selectors are attribute names in `config/platforms.yaml`. Tests read `tests/fixtures/pages/`.

Scout batch 2 (S1), open for Timur:

- A shared phone with two different street-and-house addresses is `ambiguous` (`hold=ambiguous_match`), not `same`.
  The prompt allows `same` on a phone alone and `other_address` for another branch. When both apply, a false merge is worse,
  so the case waits for a person. A phone with the same address, or with one address missing, is still `same`.
- Name comparison folds Cyrillic to Latin (letter by letter, including Kazakh letters) and then drops салон, студия, beauty, studio.
  Extra tokens still mean a different name. This is the transliteration the prompt already allows.
- `worker` runs `snapshot.verify` and `browser-worker` runs `scout.collect` and `review.snapshot_recheck`.
  Other queued types are not claimed. `check_startup()` is unchanged, so `api` still exits until R6.
- Public Playwright collection is 2GIS firm pages only, with `ZHOL_SCOUT_LIVE=1` and exactly 3 ids. A restriction page stops the pilot.
  Yandex and Google live collection stay off. The Docker image does not install a browser.

Scout batch 2.1 (S1), confirmed:

- A shared phone with a different street and house is `ambiguous`. `choose()` returns no candidate, and `match_status("ambiguous")` is `not_found`.
  Those branches are never merged automatically. The rule from batch 2 is unchanged.
- A 2GIS page that asks to prove the visitor is not a robot is `robot_check`, not a company card. If that page remains after a person is given time
  to pass it in a visible browser, the outcome is `access_blocked` and no fields are extracted. The program does not solve the check.

Client scope, 11.10.2026 (planning only, no code change):

Bek stays in Syntony. This repository does not implement Bek, a second sales agent, a CRM, or a payment processor.
AGENTS.md 5.2-5.3 still says Texer writes the audit text and Bek only sells. The client clarification says Bek also writes the audit.
Until Timur confirms that sentence, the working split is: Zhol collects cards, stores evidence, computes the score and empty fields,
and hands Bek an Excel file. The Texer observation prompt, one-page PNG, and observation council stay in the tree and are not called.
`score()` stays, because the Excel completeness columns are code, not a model.

Excel replaces the mailing CSV as the file Bek receives. Architecture §10 `GET /api/v1/export/mailing` stays specified and unbuilt.
`reports.xlsx_path` stays the later completion report, not this mailing file.

Handoff, Zhol to Bek, one row per `id_2gis`, file `mailing.xlsx`, command `python -m zhol.cli export-mailing`.
The workbook has 40 columns in this order: Company ID, Company Name, Business Sector, Business Category,
Business Subcategory, Services / Activities, Branch Name, City, District, Address, Phone, WhatsApp, Instagram,
Website, Email, 2GIS URL, 2GIS Rating, 2GIS Review Count, 2GIS Photo Count, 2GIS Description, 2GIS Opening Hours,
Google URL, Google Rating, Google Review Count, Google Photo Count, Google Description, Google Opening Hours,
Yandex URL, Yandex Rating, Yandex Review Count, Yandex Photo Count, Yandex Description, Yandex Opening Hours,
Overall Completeness Score, Missing Information, Identified Problems, Source, Collection Date, Screenshot Path,
Verification Status.

Rules: stored snapshot fields only. `config/categories.yaml` maps an exact category label to sector, category, subcategory.
An unknown label, an ambiguous label, or two different mappings on one branch stay blank. Services come from confirmed
`company_profiles.services` and are joined with `; `. A company name is not a service. Empty stays blank, stored 0 stays 0.
The score cell is `audits.score_total`, not a new formula. A branch whose latest 2GIS snapshot is `robot_check` or
`access_blocked` is omitted. Cells that start with `=`, `+`, `-`, or `@` are written as text. No passwords.

Handoff, Bek to Zhol, after the client has agreed:

| Input | Required | Zhol result |
|---|---|---|
| confirmed order | `id_2gis`, branch count, Bek deal id | `order` row, no invoice inside Zhol |
| materials | client text and files | stored, facts not invented |
| owner access | manager invite, or a secret handed to a person | browser session, password not logged |
| paid signal | Bek or Syntony says paid | work may start; Zhol does not charge the card |

Listing create or update stays behind that order and an owner grant. Generated pictures are covers, banners, and backgrounds.
They are labeled illustrative. Photos of premises, work, and staff come only from the client.

Revised batches, each tested alone. S0-S10 files stay as they are. Later stages are not deleted.

| Batch | Done when |
|---|---|
| E1 | Excel from the 50 imported reference snapshots, no browser, no model |
| E2 | Two remaining reference 2GIS cards in the same visible browser, or `access_blocked` with evidence |
| E3 | Yandex and Google public read for those three, fixtures first, live only after E2 |
| E4 | Same Excel rebuilt from the live snapshots, provenance filled |
| E5 | Local test order, materials, owner authorization, and a readiness check. No Bek, payment, or listing edit |
| E6 | Text and illustrative image from client materials, refuse premises and product photos |
| E7 | One authorized listing edit on one platform, screenshots, stop if the page is unexpected |
| E8 | Completion note: before, after, what was generated and labeled illustrative |

E4, batch collector (11.10.2026):

The batch line in the table above said the Excel would be rebuilt from live snapshots. Live discovery by city and category is not implemented for 2GIS, Yandex, or Google, so E4 does not open those catalogs and does not present fixture rows as live data. `scout-batch` reads `tests/fixtures/catalog.yaml` unless `--source-mode live` is set. Live mode requires `ZHOL_SCOUT_LIVE=1` and then stops with `unsupported` for every platform. A batch row is committed before the next company starts. Resume skips completed rows. An interrupted row is tried once more, then left `failed`. Excel cells distinguish `not_found` from `not_checked`. A blocked page is not listing data. Ambiguous matches are not merged into the common contact columns.

E5, local order intake (11.10.2026):

The batch line above used to say the input came from Bek. This batch does not call Bek, WhatsApp, Syntony, or a payment provider. A person creates a setup order with `create-test-order` or `POST /api/v1/local/orders`. The order is inserted at `created` through `transition()`. The next legal edge is still `prepay_invoiced`; this batch does not take it and does not write an invoice. `orders.amount_kzt` stores the published tariff for 1, 2, or 3 branches. That number is a quote, not a charge.

The idempotency key is `deals.syntony_deal_id = local-test:<key>`. The `local-test:` prefix is not a Syntony deal. A second insert with the same key rolls back inside a savepoint, including a company row created in that attempt. A new company needs a caller-supplied `id_2gis`, name, and city. Address, phones, and services are stored only as client facts with `source_ref` and `needs_confirmation`. A fact is approved only when `approved` is true. Materials, missing fields, and requested changes are stored on `intake`, `client_messages`, `client_files`, and a draft `change_packages` row. Owner authorization is `company_platform_ids.access_status` (`none`, `invited`, `verified`, `lost`) and is a local attestation, not a cabinet check. A plaintext password is rejected. Ciphertext goes to `credentials` only when the caller passes `ciphertext_b64`.

Readiness returns `ready` or `not_ready` plus missing company information, missing materials, missing owner permissions, and unresolved ambiguities. `ready` requires an approved name, address, and phones, logo and real photos (or `not_applicable`), `verified` on every selected platform, and no unverified fact or NAP change without consent. A create also needs approved hours and services. `require_execution_allowed` raises unless that check passes. It does not open a browser or move the stage.

E4.1, saved-page discovery (11.10.2026):

City and category discovery reads a local manifest of saved HTML, or a manual export a person saved from the browser. The default remains that path. Fixture mode does not open a website. `source=live` still stops before any page loads. 2GIS catalog extraction is refused in code. A robot check or a missing file stops that platform and does not become a company. Firm links are taken only when the saved page shows a permitted firm URL. An unclassified or other-category label is skipped. The same firm id, or the same normalized name and address, is one branch. A different house number stays a second branch. A shared phone does not merge branches. Pause and stop are checked between search pages. Resume continues the saved cursor and does not repeat a completed batch row. The same 40-column Excel writer is used. Screenshots and the page timestamp are stored with the hit. A permitted live pilot is not part of this batch.

E3, Yandex and Google public read (11.10.2026):

The live read is Badi and Velara only. One normal headed browser opens the public search URL, then the card when the name and address match. A robot check or a consent wall stops the next page. The program does not solve the check.

A city name and a floor marker do not make a second branch. The house number must still match, and one street token set must be contained in the other. `Velara beauty` matches `Velara` because `beauty` is a name filler. A shared phone with a different street and house stays ambiguous and is not stored. `same` without a shared phone stores the snapshot and opens `match_needs_review`. It does not write `company_platform_ids`. If search does not match and a public URL is already stored, that page is opened and matched by the same rule. A miss does not replace a reference snapshot.

WhatsApp is stored only from a `wa.me` link or a line that says WhatsApp. Instagram is stored only when the handle is letters, digits, dots, or underscores. A broken link is dropped. The website ignores map asset hosts and social hosts. An explicit "ещё нет отзывов" is review count 0. Common Excel contacts follow `config/contacts.yaml`: the first filled source stays, a later empty source does not clear it, and a different value is reported. The same site with `http` and `https` is not a conflict.

Google search for these two queries opened the Samal-2 district and a results map. The salon cards were not on those pages, so no Google snapshot was written.

E2, 2GIS visible card (11.10.2026):

The rubric under the company name is read by code and stored as `category` only when `config/categories.yaml` maps that exact label. An unknown label, an ambiguous label, or two different mappings stay null. Sector and subcategory are still computed by the Excel export, not by a model. `city`, `district`, `email`, `description`, and `hours` are snapshot fields when the page shows one unambiguous value. Named services are stored on `company_profiles` only when a price tab shows the name next to a price. The company name is not a service. A rerun looks up `id_2gis` and does not insert another company. One visible browser opens the two remaining reference cards; a robot check stops the next card and saves the screenshot.

E7.1, listing plan and dry-run (11.10.2026):

E7 in the batch table is still one real listing edit with screenshots. This batch does not do that edit. `dry-run-listing` loads a local order, decides create or update per platform, and writes a field plan from approved company facts plus checked illustrative content. `apply_changes` returns `dry_run` and does not send a form. A second run with the same result does not insert another `change_items` row or another `change_log` row.

`change_log.result` may be `dry_run` for that prepared apply. `screenshot_after` stays empty. The order stage is not moved.

A platform that fails, pauses, or stops does not remove another platform's log. Critical missing name, address, or phones, and a run where every selected platform lacks `verified`, raise before any log. Name, address, phone, and category on an existing listing are held until a separate client yes is stored on a change item. A new listing may use the approved fact's `source_ref` as that yes.

Saved cabinet HTML is the only page this batch reads. A login wall, a robot check, or an SMS code pauses for a person. The program does not type a password, solve a check, read a code, or open the live cabinet. Plaintext passwords are not accepted. Ciphertext in `credentials` is not read.

E6 content is optional. A checked `content_items` row with `brief.illustrative: true` can be planned. A missing or unfinished row is `content_unavailable` and does not block approved facts. Generated photos of premises, work, or staff are refused. 2GIS writes the site to `link`. The 2GIS and Yandex `prices` values stay observed; the service list is the writable price data. Google Business Profile does not write services, WhatsApp, or Instagram in this batch because those are not editable fields in the dictionary. Description stays read-only until a client fact exists for it.

E7.2, Yandex Business prepare (11.10.2026):

`prepare-yandex-listing` fills an existing Yandex organization from approved client facts and stops before Save, Publish, or Submit. The public card is not treated as updated. `change_items` stay `draft`. A `change_log` row with `result=prepared` stores the before and prepared screenshots. An open human task `yandex_listing_review` is the approval checkpoint for a later batch. Passing approval into this command is refused.

`description` and `category` are optional client facts (`value`, `source_ref`, `needs_confirmation`). Yandex may write `description` only from an approved fact. 2GIS and Google still keep `description` read-only. The Yandex `prices` counter stays observed. Service prices are written only as the approved service list.

The browser is headed and starts with a blank context. A saved browser profile and `credentials` ciphertext are not read. A person may sign in during the live wait. A login wall, a robot check, an SMS code, or a reauthentication prompt pauses the run. Password and code fields are not read and are not copied into evidence. The organization id and the visible organization name must match the order. The visible role must be owner or representative. A missing control stops the run before any field is written. A second run with the same prepared values does not add another log row. Live https stays closed unless `ZHOL_YANDEX_CABINET=1`. Automated tests use saved cabinet HTML, not a production account.

E7.3 and E7.4, 2GIS and Google Business Profile prepare (11.10.2026):

`prepare-2gis-listing` and `prepare-google-listing` use the same prepare step as Yandex. Saved cabinet HTML is the default. A headed browser opens only when `ZHOL_2GIS_CABINET=1` or `ZHOL_GOOGLE_CABINET=1`, with a blank context and no saved profile. The command fills an existing organization and stops before Save, Publish, or Submit. It does not create a listing.

2GIS writes the site to `link`. Description and the prices flag stay read-only. Service prices are the writable price data. WhatsApp and Instagram are prepared only when the cabinet already has those controls and the client approved the value. Google writes description from an approved client fact. Google does not write services, WhatsApp, or Instagram: they are not editable fields. A login wall, robot check, SMS code, or reauthentication prompt pauses the run. The organization id, the visible name, and an owner or representative role are checked before any field is written. A missing or unknown control stops the run before a partial write. One platform stopping does not remove another platform's log. A second run with the same prepared values does not add another log row. Human tasks `2gis_listing_review` and `google_listing_review` are the approval checkpoint for a later batch. Passing approval into these commands is refused.

E7.5.1, original Bek workbook (11.10.2026):

The original workbook is `source/bek-audit-original.xlsx`. It has four sheets: `Все данные`, `Сводка`, `Контакты для связи`, and `Как читать`. A workbook that has those four sheets is read from them. Any other xlsx still uses the header scan from E7.5.

`Все данные` is the evidence. Repeated titles such as «Рейтинг» are read under the merged group on the row above. The 2GIS and Yandex addresses are the hyperlink targets, not the visible words «открыть» or «Открыть 2ГИС». `Сводка` must show the same five scores. `Контакты для связи` must show the same phone, WhatsApp, and firm id; its hyperlink is kept. `Как читать` says an empty cell was not determined, so it stays null and does not become 0 or a task. «нет» is a confirmed absence. «нет данных» on Google rights stays null. «0» stays 0. A phone that starts with `+` is a phone. `да` and `нет` on a yes/no column become the same 1 and 0 the rest of the importer already stores. `score()` is still not called.

Rows 53-152 do not follow that header: the total-score cell is a phone number and the 2GIS link cell is not a firm hyperlink. E7.5.1 left them unmatched. The guide describes 50 salons. Those 50 are the rows that do follow the first header.

E7.5.2, cosmetology section (11.10.2026):

Rows that fail the first header are read as a later section when «Сводка», «Контакты для связи» and «Все данные» name the same business, the five scores agree, and the contact sheet has a 2GIS firm link. The category stays «косметология» and the sector stays «косметология (Almaty)». Phone, WhatsApp, Instagram, address and the firm id are stored only when the sheets agree. A firm id that already belongs to a different name stays a human task. Two businesses at one address stay two companies.

2GIS rating, rating count, photo count and the price tab are stored when «Сводка» and «Все данные» show the same value. Yandex rating is 2GIS minus 0.1 on every one of these rows, and every Yandex organization id is a prefix of the firm id, so those Yandex and Google numbers are not stored and `score()` is not called. The sheet scores are copied. An empty cell stays null. «Не найдена» for Google stays a verification candidate on the plan. It does not become `not_found` and it does not open a create-listing task. Reply share, last-review date, ads, news, owner flags and the unlabeled booking links have no corroborated column, so they are not imported.

A sales sentence in «Главные пробелы» is stored on the audit. Only «Расхождения между сервисами» can open a cross-platform task. A low total score does not open a task.

E7.5, Bek audit plans (11.10.2026):

`import-bek-audit` reads an xlsx workbook with openpyxl. The detailed sheet is the one whose header matches the audit columns. Summary sheets are ignored for that older shape. Scores and score groups are copied from the sheet. `score()` is not called and is not changed. A formula cell is not a value. A phone that starts with `+` is a phone.

Companies match on `id_2gis`, including the id in a 2GIS URL. A second row with the same id, an id that disagrees with the URL, a name and address that fit more than one company, or a phone at a different address stays a human task (`bek_audit_ambiguous` or `bek_audit_unmatched`). Rows whose `слой` is outside the manually checked layers are not imported. A second import adds no companies, snapshots, audits, or plans.

A verified snapshot with `collected_at` after the sheet date is left as it is. The older audit row is not written over it. An equal or older snapshot is also left unchanged. The Bek row is a new `collected` snapshot with `collected_by = bek_audit`.

Plans live in `listing_improvement_plans`. A task is `ready_for_preparation` only when the supervised prepare command can fill that control and the approved fact, verified owner access, and cabinet control are already stored. Name, address, phone, and rubric also need a stored client yes. Human approval after prepare still blocks submit. `prepare-2gis-listing`, `prepare-yandex-listing`, and `prepare-google-listing` do not create a listing, so a missing card is `unsupported`. Review replies, owner verification, real photos, platform reconciliation, and missing client facts are `manual`. No task is marked completed. The import does not open a browser, call Bek, or write `change_log`.

E7.6, new listing preparation (12.10.2026):

`prepare-2gis-create`, `prepare-yandex-create`, and `prepare-google-create` fill a blank create form and stop before Create, Save, Publish, or Submit. They do not create a public listing. The older update commands still refuse a create order.

A draft is allowed only when the platform has no stored id or url, no snapshot is `found` or `other_address`, and the newest search is a verified `not_found` from `order_fresh`, `report`, or `subscription_start` within 14 days. An audit `not_found`, including a Bek workbook row, stays unverified. A person must also store `absence_confirmation` on the order platform, with a source ref and a time within 14 days. Anything else is `uncertain` or `existing_listing` and opens a human task. Verified owner access is required. The open page must be that platform's create form, with no listing id, an absence phrase, and the organization name empty or equal to the order. An unknown text box, a different organization, a login wall, a robot check, or an SMS code stops the draft before a partial write.

Each platform create form lists only the text fields that cabinet can write. 2GIS does not write description. Google does not write services, WhatsApp, or Instagram. Name, address, phone, and category are required, plus hours and services where that form has them. Every written value comes from an approved client fact, or the company name already on the order. Files and generated pictures are not uploaded. A second run with the same values does not add another `change_log` row. Human tasks `2gis_create_review`, `yandex_create_review`, and `google_create_review` are the approval checkpoint. The Bek improvement plan still does not schedule creation by itself.

E6, content drafts (12.10.2026):

`prepare-content` writes text drafts and one abstract cover from approved client facts. It does not publish them and does not open a cabinet. A draft stays `done` until `approve_content` sets `checked`. The listing planner can use a checked description only when the client has no description fact and the current listing description is empty. Yandex and Google can plan that description. 2GIS description stays read-only. Checked illustrative covers keep the existing content path. Review replies are drafted only when `intake.checklist.review_replies` is `authorized`, and a rating of 3 or lower also needs `review_reply_consent` before that draft can be checked. Replies are not listing fields.

The same approved facts, prompt version, and limits do not call the model again. A change of those facts clears the approval and stores a new version. Illustrations use `OfflineIllustrationProvider` unless a caller passes another provider. The offline provider writes a labeled PNG and does not call a network API. Kinds that would invent premises, staff, products, or completed work are refused in code and open `content_refused`.

Length caps live in `config/platforms.yaml` `text_limits`: Google description 750 characters (Business Profile description), Yandex and 2GIS drafts 1000 until a cabinet help page is cited, service text 300, review replies 600. A longer draft is `rejected`. Claims must point at an approved fact and quote that fact. Unapproved values are not sent to the model.

E8, completion verification (12.10.2026):

`verify-order` reads the stored order. It does not open 2GIS, Yandex, or Google, and it does not change a submission. A Save or Create click, a dry-run, and a prepared draft stay unpublished. `published_verified` requires a `card_snapshots` row with `purpose=report`, collected after `submitted_at`, status `verified`, a screenshot file on disk, and a source (`url`, `seen_at`) for each approved field whose value matches. That snapshot is stored evidence. `live_verified` stays false. Evidence older than 14 days, a missing screenshot, a live flag on the snapshot, a changed package, or a changed content approval blocks that mark. The client report is `reports` period `setup`, plus `completion.json` and `completion.html`. PDF is not generated. A second run with the same evidence keeps one row and the same files. Passwords, tokens, and credential ciphertext are not copied into the report.

E9.1, local read-only admin (12.10.2026):

`/api/v1/admin` reads stored companies, batches, Bek audits, orders, content, listing packages, human tasks, and completion-report metadata. It does not add an architecture §10 Bek route, submit a listing, or open a browser. The panel is off until `ZHOL_ADMIN_TOKEN` is set, and Compose publishes the API and the panel on `127.0.0.1` only. `docker-compose.offline.yml` points the API at the golden stub config so a local panel can start while `config/models.yaml` still fail-closes on empty R6 prices. A stored submission status of `published` is shown as verified stored evidence with `live_publication: false`.

E9.2, local admin actions (13.10.2026):

The same `/api/v1/admin` token can start a fixture or manual scout batch, import a Bek workbook from an uploaded file, create a local order, prepare content with an offline fact-quoting provider, dry-run a listing plan, approve one content version or one change-package revision, simulate an offline submission, and write a completion report. Writes require the header `X-Zhol-Request: admin`. Authentication stays a bearer token in the browser tab, not a cookie. Uploads are size-limited `.xlsx` or saved HTML, stored under `data/admin` with generated names. Downloads resolve only inside that directory. Scout batches are PostgreSQL jobs of type `admin.scout_batch` on the existing `main` queue. E9.2 ran that type inside the API process. E9.3 moves it to the main worker. Live discovery, live cabinets, Bek calls, and paid model providers stay closed. `config/models.yaml` still fail-closes on empty R6 prices.

E9.3, worker-owned scout batches (13.10.2026):

`admin.scout_batch` stays on the existing `main` PostgreSQL queue. The API only enqueues it. The main worker claims it, together with `snapshot.verify`. Pause, stop, and resume are stored on the batch row, so a new worker can see them after a restart. A job whose lease has expired is failed by the existing recovery and then retried on the same batch. The panel prepares, approves, and simulates each platform on its own. An approval for one platform does not approve another. Offline simulation still has `published: false` and `live_verified: false`.

E9.6, Russian panel and local session (13.10.2026):

E10.3, reusable browser collector (15.10.2026):

The saved-page walker (`walk_saved`, `LocalHtmlPage`) is now the browser collector used by discovery jobs. It accepts a city, category, optional query, and limit, keeps a checkpoint, and closes the local page session when the walk ends or fails. There is no Selenium collector in this repository. Live adapters for 2GIS, Yandex, and Google are not registered: `saved_discovery_only` still raises before a page opens, and `browser_2gis` is still rejected before a job. The admin form no longer shows that disabled method. It lists a browser source only when `panel_browser_modes()` returns one, which it does not. Saved HTML is not labeled as a live result.

E10.2, browser collection (15.10.2026):

`browser_2gis` is shown in the collection form and is not available. The 2GIS service agreement (https://law.2gis.ru/rules) forbids automated scripts, programs, and bots that collect or export data from 2GIS services. The project does not open 2gis.kz for a city or category search, does not ship a Selenium or Playwright catalog crawler, and does not bypass a robot check. A request with `source_mode=browser_2gis` is rejected before a job or a company row is created. Fixture collection and `live_2gis` stay as they were. The permitted live path remains the official Catalog API with `DGIS_CATALOG_KEY`.

E10.1, official 2GIS Catalog API (14.10.2026):

Website catalog extraction stays refused. `source_mode=live` still stops before a page loads. A separate `live_2gis` batch reads the official Catalog API at `catalog.api.2gis.com` and only when `DGIS_CATALOG_KEY` is set. The key comes from platform.2gis.ru. A demo key is quota-limited, and commercial collection needs 2GIS authorization. The key is not stored in the repository, the database, or the setup response. Region ids are resolved by the city name through the Regions API and are not hardcoded. Contacts that the key is not allowed to return stay null. Yandex, Google, Bek, and paid models stay disconnected. Fixture batches still read the saved catalog and do not call the API.

The offline Compose overlay sets `ZHOL_LOCAL_ACCESS=1` and shares `ZHOL_LOCAL_GATE` with the web process only. The Vite proxy adds that gate on the server. The browser never receives the admin token or the gate. `POST /api/v1/admin/local-session` then sets an HttpOnly `SameSite=Strict` cookie. Writes still require `X-Zhol-Request: admin`. The default Compose file does not set local access, so a bearer token remains mandatory and a cookie is ignored. A public client address is rejected. Ports stay on `127.0.0.1`. `config/models.yaml` still fail-closes on empty R6 prices.

E10.5, existing Selenium collector behind a grant (15.10.2026):

The Almaty Selenium collector at the repository root is the parser. Zhol calls `search.run_search` and `card.read_company` from `admin.scout_batch` when `source_mode=browser_2gis`. Excel is not an intermediate store. The only configured city slug is `almaty`. A verbal permission does not open Chrome. Live execution stays off unless `ZHOL_2GIS_BROWSER_GRANT` is exactly `written-browser-retention-commercial`, which stands for a written authorization covering automated browser collection, retention, and commercial use. That variable is not set in Compose. A robot check stops the batch. Captcha bypass, proxy rotation, stealth drivers, and fingerprint spoofing are not implemented. `live_2gis` remains the Catalog API path. Yandex, Google, Bek, payments, and paid models are unchanged.

E10.18, local browser collector (17.10.2026):

Chrome readiness and source permission are separate. `ZHOL_BROWSER_ENGINE`, a Chrome binary, or the collector image can make the engine available without authorizing a site. `ZHOL_2GIS_BROWSER_GRANT` is still required before any navigation to 2gis.kz. A loopback `ZHOL_BROWSER_BASE_URL` (`127.0.0.1`, `localhost`, `::1`, or `host.docker.internal`) or a local directory in `ZHOL_BROWSER_HTML_ROOT` does not use that grant. A public host in the base URL is ignored. The admin mode, start, stop, and resume follow that source rule. Catalog API key is not required. A robot check still stops the batch. No captcha bypass, proxy rotation, or stealth driver is added. The existing application database is not the database for this local run.

E10.20, local Selenium load guard (17.10.2026):

The load run uses the existing Selenium parser and the `collector` queue against `tests/fixtures/selenium-load/server.py`. While `ZHOL_BROWSER_BASE_URL` is set, `open_url` refuses any host other than `127.0.0.1`, `localhost`, `::1`, and `host.docker.internal`, including a redirect onto 2gis.kz, Yandex, or Google. Chromium in that mode also maps those live map hosts to `0.0.0.0`. This is not a stealth profile, a proxy, or a captcha bypass. The written grant is still required when the base URL is absent. Pause remains resumable and Stop remains terminal. A browser crash still marks the batch interrupted and the job failed; the existing retry schedule is unchanged. After `driver.quit()`, the collector reaps Chromium processes that were reparented to the worker. The application database is not migrated or written by this run.

E10.23, batch limit and browser session cleanup (17.10.2026):

The batch limit is the number of distinct company cards that batch may open. A company already stored in PostgreSQL spends one of those slots when this batch opens its card. It is not refreshed for free, and it is not a reason to open every earlier card. A retry of the same card does not spend a second slot. Snapshots are written only for cards the batch actually opens, and older snapshots stay. Chrome startup and quit no longer wait on a dead driver: a failed start or a disconnected session kills the Chromium processes for that profile, including after the driver never returned a session. The application database is not migrated or written by this change.

E10.22, collection metrics and temporary failures (17.10.2026):

The admin batch response copies `collection_batches.progress.metrics` when those counters were stored. It does not fill a missing counter with zero. Card rows are counted into saved, already present, waiting for retry, and permanently rejected. A card error is `page_timeout`, `network_failure`, or `browser_session` for at most three attempts, and stays `pending` until that limit. `captcha`, `access_restriction`, `missing_identity`, and `invalid_card` are rejected on the first record and are not opened again. `seen_ids` still changes only in the transaction that commits the company. A pass that still has a retryable card ends `interrupted`, so resume can open it. `metrics.retries` is the queue job attempt minus one. The application database is not migrated or written by this change.

E10.21, incremental Selenium persistence (17.10.2026):

Each card that the existing parser accepts is committed before the next card is opened. The company row, the platform snapshot, the batch item, and `seen_ids` are one database transaction. A checkpoint cannot list a company that was not committed. A crash keeps those rows. The same batch and company do not get a second snapshot; a later batch still adds its own snapshot. A card with a name and a firm id is stored when the address link is missing: the address stays null and the source says `not_shown`. A finished page with no name and no firm id fails after the short heading grace. A page that is still loading keeps the full card timeout. An access block is recorded and is not bypassed. Stop stays terminal. The job still dies after three attempts. The application database is not migrated or written by this change.

E10.6, collector queue (15.10.2026):

`browser_2gis` batches are enqueued on the `collector` queue. The main worker and the saved-page browser worker do not claim that queue. Compose service `collector` uses `Dockerfile.collector` (Chromium, Selenium, the existing parser modules) and is behind the `collector` profile, so a normal `docker compose up` does not start it. The grant variable is still absent. A second running job cannot take a batch that already has an owner. Checkpoints stay in `collection_batches.progress`. Chrome quits in `finally`, and SIGTERM asks the active batch to stop first. Migration `a10e5c4d6b21` is the Alembic head and is not applied to the existing application database from this change.

E10.11, collection defects (15.10.2026):

The collection limit counts accepted 2GIS branches. After the limit, Yandex and Google hits that match an accepted branch are still attached. A Yandex-only or Google-only hit is a blocked batch item with `id_2gis` `trace:<platform>:<external id>` and no company row. Creating a company without a 2GIS id is not done here; that identity model is still open. A name and address with two different phones is `ambiguous`, stored on the branch as an alternative, and is not merged. `card_snapshots.match_status` stays `found`, `other_address`, or `not_found`. Ambiguous evidence uses hold `ambiguous_match`, and the company page says the match is unresolved. Permitted Catalog and Selenium calls use `rate_limit_seconds` from `config/platforms.yaml` and a shared pace file. Saved HTML does not sleep. A robot check or access block marks the saved walk interrupted and does not bypass the check.

E10.13, platform-independent company identity (16.10.2026):

`companies.id` is the stable company id. `id_2gis` may be empty when the company was found on Yandex or Google. An external id is unique per platform (`uq_company_platform_ids_platform_external`). A company is created only when no stored platform id matches and the name, address, and phones are not an unresolved match. The same normalized name and address with a shared phone, or the same external id, attach to the existing company. The same name at a different house is another company. A shared phone at a different address, a name match without an address, or two possible companies stays a blocked item and a human task. Similar names are not merged. The collection limit counts accepted identities from any platform. Evidence for an already accepted identity is still attached after the limit. A Yandex-only or Google-only company gets a `collected` snapshot from the saved page. Existing verified snapshots are left as they are. Migration `c3e7a1b84d02` is the Alembic head and is not applied to the existing application database from this change. Live Yandex and Google collection is not implemented.

E10.14, safe downgrade (16.10.2026):

Downgrade of `c3e7a1b84d02` does not delete companies or child rows. If any company has an empty `id_2gis`, downgrade stops before dropping the platform-id index or changing the column, and explains that NOT NULL cannot be restored. When every company has a 2GIS id, downgrade drops the index and restores NOT NULL. No fake 2GIS id is written. The application database stays on `a10e5c4d6b21` until a separate approval.

E10.25, manual 2GIS verification (17.10.2026):

A robot check in the existing Selenium collector no longer ends the batch as a non-retryable interruption. The batch becomes `awaiting_manual_verification`. Companies, snapshots, and `seen_ids` already committed stay in place. The operator opens one visible Chrome window, completes the check by hand, and then resumes the same batch. The program does not click, solve, hide, or bypass the check. If the page still shows the check, access is blocked, or the source is not permitted, the batch stops with an explanation and Chrome is closed. The Compose collector has no display, so it does not open an invisible window: the batch stays waiting and says the window must be opened on a computer where Chrome can show it. Stop remains terminal. Pause of a running batch and resume of a paused or interrupted batch stay as they were. `written-browser-retention-local` is still only local non-commercial collection and storage. It is not the commercial grant. Migration `d4f8a2c19e30` is the Alembic head and is not applied to the existing application database from this change.

E10.26, same Chrome session for a manual check (17.10.2026):

The robot check stays in the Selenium session that already opened the page. On macOS that Chrome is started with a window, and System Events brings forward only the process belonging to that session. The program does not launch a second Chrome, does not attach to another profile, and does not click the check. After the operator finishes, the same page is read again. Collection continues in that window only when the check is gone. A headless session, or a process without a display, is closed and left waiting, because a new window would be a different session. Stop and a timeout still quit that Chrome. The application database is not migrated.

E10.24, separate 2GIS browser scopes (17.10.2026):

`ZHOL_2GIS_BROWSER_GRANT` now has two exact values. `written-browser-retention-local` is local non-commercial collection and PostgreSQL storage. `written-browser-retention-commercial` remains collection, storage, and commercial use. A local value does not count as the commercial value, and any other text stays closed. Compose sets the local value and `ZHOL_BROWSER_ENGINE=1` on `api` and `collector` only. Yandex and Google city search still read saved HTML. That import is not live collection, and the 2GIS grant does not enable those platforms. No captcha bypass is added. The application database is not the database for a live probe.

E10.27, Yandex and Google live collection stays closed (18.10.2026):

`browser_yandex` and `browser_google` are listed in the collection form and are not available. Yandex Maps terms, clause 4.6 (https://yandex.ru/legal/maps_termsofuse/, in force 23.09.2026), allow the service data only for personal non-commercial use and forbid copying it without written permission from the rights holder. Google Maps additional terms, section 3 (4 June 2025), forbid copying content and building a business database from Maps. Google Maps Platform Terms, section 3.2.3 No Scraping, forbid saving business names and addresses outside the service. The 2GIS grant does not apply. A request with `source_mode=browser_yandex` or `browser_google` is rejected before a job or a company row is created. The Selenium adapters read only a loopback fixture in tests. They do not open yandex.kz, yandex.ru, or Google Maps, and they do not bypass a robot check. Saved HTML import is unchanged and is not reported as a live result. Migration `e5a9b3c20f41` is the Alembic head and is not applied to the existing application database from this change.

E10.28, separate Yandex and Google browser scopes (18.10.2026):

`browser_yandex` and `browser_google` stay closed until the operator sets that source's own variable. `ZHOL_YANDEX_BROWSER_GRANT` and `ZHOL_GOOGLE_BROWSER_GRANT` are not `ZHOL_2GIS_BROWSER_GRANT`. Each accepts only `written-browser-retention-local` (local non-commercial collection and PostgreSQL storage) or `written-browser-retention-commercial` (collection, storage, and commercial use). A local value is not a commercial grant. Any other text, an empty value, and the other source's variable leave that site closed. Compose passes the variables through and does not choose a scope. The authorization document is not stored. A robot check is not solved. Loopback fixtures remain the test path and are not reported as live collection. The application database is not migrated by this change.

E10.29, Google local scope for the panel and the macOS collector (18.10.2026):

`ZHOL_GOOGLE_BROWSER_GRANT` stays independent of `ZHOL_2GIS_BROWSER_GRANT` and `ZHOL_YANDEX_BROWSER_GRANT`. Compose gives the API and the collector the local scope when the variable is empty: `written-browser-retention-local`. That is local non-commercial collection and PostgreSQL storage, not commercial use. Yandex has no default. The macOS LaunchAgent reads each grant from the running API container and does not copy one source's value onto another. An empty or unrecognized Yandex or Google value leaves that source closed and does not stop the 2GIS worker. A card whose address shows another locality, or more than one locality, or no locality, is not stored as Алматы. Migration `e5a9b3c20f41` is ready on an isolated copy and is not applied to the application database.

E10.30, explicit Google scope and application-database map modes (18.10.2026):

The operator approved applying `d4f8a2c19e30` and then `e5a9b3c20f41` to the application database. Those revisions only widen check constraints on `collection_batches`. They do not delete or rewrite companies, snapshots, or platform ids. An empty `ZHOL_GOOGLE_BROWSER_GRANT` is not a grant: Compose passes the variable through and does not substitute `written-browser-retention-local`. The operator environment sets that local scope explicitly. It means local non-commercial collection and PostgreSQL storage, not commercial use. `ZHOL_2GIS_BROWSER_GRANT` and `ZHOL_YANDEX_BROWSER_GRANT` stay on their own values. The macOS collector still reads each grant from the running API container.

E10.31, Yandex selectors from saved rendered HTML (18.10.2026):

The Yandex Selenium reader was checked against previously saved rendered HTML. No new request was sent to Yandex. On that search page the result link is `a.link-overlay` inside `li.search-snippet-view`. There is no `.search-list-view` element and no `a.search-pagination__page` control. Organization cards are `aside.sidebar-view`, not `article.orgpage-view`. The visible name, address, phone, rating text, and rating count use the classes on those pages. The loopback fixture selectors stay accepted. A live document that is HTTP 429 `limited` is still an access restriction.

P1 release blockers, explicit configuration and map completion (18.10.2026):

Compose no longer fills `POSTGRES_PASSWORD` or `ZHOL_2GIS_BROWSER_GRANT`. An empty grant does not authorize 2ГИС, Yandex, or Google. The API and workers refuse to start when `DATABASE_URL` is missing. They do not change a password that is already set. Running containers and the application database are not recreated by this change. A Yandex page without a next-page link and without `data-results-complete` is an incomplete list, not the whole result. A Google feed that stops growing is `stall`, and the batch stays `interrupted`. A headed map session that shows a robot check waits on that same window. A headless session records that manual verification is unavailable and does not resume as if the check had passed. The program does not solve or bypass the check.

Local macOS startup, existing database kept (18.10.2026):

The database user, database name, password, and 2GIS local scope already present in the running containers were written into `.env`. The password was not replaced. Google and Yandex stay on their own local scopes. Containers were not recreated and no migration was applied. `Запустить Zhol.command` is the operator step that rebuilds the local images and recreates the services onto the existing `pgdata` volume. That command starts the backup service and does not start the Docker collector. The LaunchAgent remains the visible Chrome collector. Alembic head stays `e5a9b3c20f41`.

Local launcher did not reach Compose (18.10.2026):

`Запустить Zhol.command` checks `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` before any `docker compose` command. Those keys were absent until 19:31. After that, the launcher did not run: it would have rewritten `.env`, and the image stamp would have changed. The stamp is still the 13 October value, the workers are still the exited 17 October containers, and the backup service was never created. The repaired launcher keeps an existing database container, starts the API, panel, both workers, and the backup service, and writes failures to `~/Library/Logs/Zhol/startup.log`. It was not used to recreate the running project.

Identity, different phones at one address (25.10.2026):

`relate()` stays `ambiguous` when the name and address fit and both cards have phones that do not overlap, so a second company is not created. `choose()` keeps a single search hit as `same` with `phone_match` false: the snapshot can be reviewed, and `company_platform_ids` is not written. Two such hits stay unresolved and no card is chosen. A shared phone written as `+7` and `8` is still one card. A card with no phone on one side stays `same` with `phone_match` false. A newer snapshot from another platform is not used as this platform's address. No migration is added.

Collection export size (25.10.2026):

`GET /api/v1/jobs/{id}/export` and `GET /api/v1/export` still stop at 5000 rows. The count is checked before the remaining rows are read. CSV, JSON, and Excel are written row by row. Passing the cap returns the same Russian error and does not add a table or a migration.

Indexed company lookup and exports past 5000 rows (25.10.2026):

`maps` and discovery now share `classify_links`. A confirmed platform id stays on its company. Otherwise one `same` company is attached only when no other candidate is `same` or `ambiguous`. Search `choose()` can still open one clear card when the results also contain a phone branch. Existing company, snapshot, and platform-id rows are not rewritten. `python -m zhol.cli identity-report` prints pairs of snapshots on one company that are not a confirmed phone match and then rolls back.

Migration `b6d2f4a81c30` adds `company_match_keys` plus indexes for `lower(companies.city)`, the newest snapshot of a company and platform, the job claim and lease queries, and batch items ordered by id. Downgrade drops those objects and does not change company rows. The lookup reads the keys instead of every company in the city. It was not applied to the working database.

CSV and JSON exports no longer stop at 5000 rows. Rows are written to a temporary file and the file is removed after the response or on error. Pages use the company id or batch-item id inside one repeatable-read transaction. Excel still stops at 1 048 575 data rows, which is the worksheet limit. Cells that start with `=`, `+`, `-`, or `@` are prefixed so a spreadsheet does not treat them as formulas. Each export page is dropped from the session after it is written, so the identity map does not keep every company until the file is finished.

Match keys follow ORM writes (25.10.2026):

`company_match_keys` is updated in the same transaction when a company or a snapshot is inserted, updated, or a snapshot is deleted through the SQLAlchemy unit of work. Direct SQL, `bulk_insert_mappings`, and `executemany` do not update the keys. That path is unsupported until `python -m zhol.cli rebuild-match-keys`. The command replaces every key from the current companies and the newest snapshot of each platform. It is safe to run again. It does not merge, split, or delete companies. The catalog lookup is not cached, so a process that keeps running across a migration sees the new table on the next write. Stop the API before `upgrade` or `downgrade`, then start it again.

Collection result, mode labels, and run timings (26.10.2026):

The 2GIS collector stores the stop reason as the text of `search.py`, so the panel did not recognise the limit and showed a batch that reached its limit as completed with warnings. `zhol/agents/scout/collection_outcome.py` reads both the text and the map collectors' codes. The panel and `/api/v1/jobs` use the same result: `completed` (end of results confirmed), `limit_reached`, `partial` (rejected cards or end not confirmed), `paused`, `blocked` (browser check or access restriction), `stopped`, `interrupted`, `failed`. Reaching the limit is not a warning, and only a confirmed end says the results ran out. The job `status` field and its controls are unchanged. The job document gains `result`, `collection_mode`, `timings`, `created_at`, `started_at`, `finished_at`, and `duration_seconds`.

The publication simulation banner is no longer shown on collection pages, and the text "Живой сбор города: нет" is removed. It referred to a city-wide crawl that is not offered, and read as "this collection is not live". The batch payload says where the cards were read: live site, localhost, saved HTML, test catalog, or demo. Collection always runs one query in one city. The demo banner still depends only on `meta.demo`. The collector's execution mode and its permissions are not changed.

`average_persistence_ms` used to stop before the company and snapshot writes and the commit. A card commit is now timed with `perf_counter` from opening the session until the commit finishes, including one retry. The duration is written in the next card's transaction, and the last one is written once at the end of the run, so no write is added per card. The run also records the browser start, link discovery (up to the engine's first progress callback), card reading, pauses between cards, waiting for a browser check, and the whole run. These are stored in `progress.timings` with `persistence_scope = "commit"`. A batch without that scope shows its old persistence figure as not fully measured. The first timed run of such a batch drops the old figure. Yandex and Google record only the browser start, the persistence time, and the whole run, because their discovery and card reading are not split. `search.py` is not changed. No migration is added.
