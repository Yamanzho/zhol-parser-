# Архитектура «Жол»

Версия 2 от 08.10.2026.

## 0. Старшинство документов

| Вопрос | Главный документ |
|---|---|
| Бизнес-правила, продукт, цены, запреты агентам | ТЗ (`AGENTS.md`) |
| Технические имена, таблицы, статусы, API, события, цепочка задач | Этот файл |
| Имена полей, значения, переменные промтов | `04-slovar-dannyh.md` |
| Кто что проверяет, консилиум | `03-proverka-i-konsilium.md` |
| Что делать на этапе и как его сдавать | `stages/Sx.md` |

Противоречие между ними: записать в `docs/decisions.md` и спросить Тимура. Не выбирать молча.

**Уточнения относительно ТЗ** (ТЗ описывает смысл, здесь точная реализация):
- снимок получает статус `verified` после проверки кодом, а не только после выборочной перепроверки (раздел 4.1);
- заказ создаётся, когда клиент согласился, до счёта (раздел 9);
- счёт выставляет Жол действием через Syntony, в том числе ежемесячный (раздел 9);
- проверка каждой задачи идёт по `03-proverka-i-konsilium.md`, а не только Критиком в шести точках;
- данные бизнеса клиента хранятся на уровне компании, а не заказа.

## 1. Стек

| Часть | Выбор |
|---|---|
| Язык | Python 3.12 |
| Веб и API | FastAPI |
| База | PostgreSQL 16, миграции Alembic |
| Очередь | Таблица `jobs` в Postgres, воркеры с `SELECT ... FOR UPDATE SKIP LOCKED` |
| Браузер | Playwright (Chromium), постоянные профили |
| Модели | Anthropic SDK и OpenAI SDK, Gemini через Google SDK, одна обёртка |
| Файлы | Папка `FILES_DIR`, выдача наружу по подписанным ссылкам |
| Уведомления | Telegram Bot API |
| Запуск | Docker Compose: `db`, `api`, `worker`, `browser-worker`, `scheduler` |
| Тесты | pytest |

## 2. Структура репозитория

```
zhol/
  AGENTS.md  CLAUDE.md  docker-compose.yml  .env.example
  config/
    models.yaml           # модели, семейства, цены, умеет ли картинки
    review_policy.yaml    # кто проверяет каждую точку (03, раздел 9)
    platforms.yaml        # адреса, селекторы, editable_fields, media по площадкам
    sla.yaml  scoring.yaml  pricing.yaml   # pricing: цены ТЗ раздел 4
  prompts/                # рабочие промты
  council/checklists/     # чек-листы точек проверки
  zhol/
    db/  jobs/  llm/  review/  guards.py  timeutil.py
    agents/{scout,auditor,executor,creator,manager}/
    browser/{twogis,yandex,google}/
    integrations/{syntony.py,telegram.py,invoices.py}
    api/  reports/  files.py  secrets.py  cli.py
  tests/golden/<имя промта или точки>/{cases,expected}/
  source/                 # материалы заказчика (см. START.md)
  docs/plan/  docs/acceptance/  docs/decisions.md  docs/backlog.md  docs/runbook.md  docs/syntony-contract.md
```

## 3. Схема базы

```sql
-- Компании и их карточки
create table companies (
  id bigserial primary key,
  id_2gis text unique not null,
  name text not null,
  city text not null default 'Алматы',
  niche text,
  whatsapp text, instagram text,
  dnc boolean not null default false,
  source text not null,              -- etalon_2026_09_30 | base_550_contacts | inbound | manual
  created_at timestamptz default now()
);

create table company_platform_ids (  -- id организации на каждой площадке
  company_id bigint references companies,
  platform text not null check (platform in ('2gis','yandex','google')),
  external_id text,                  -- id организации на площадке
  public_url text,
  cabinet_url text,                  -- адрес в кабинете, когда доступ есть
  access_status text not null default 'none', -- none | invited | verified | lost
  primary key (company_id, platform)
);

create table company_profiles (      -- данные бизнеса от клиента, с версиями
  id bigserial primary key,
  company_id bigint references companies,
  version int not null,
  data jsonb not null,               -- 04-slovar-dannyh.md раздел 2
  created_at timestamptz default now(),
  unique (company_id, version)
);

create table card_snapshots (
  id bigserial primary key,
  company_id bigint references companies,
  platform text not null check (platform in ('2gis','yandex','google')),
  match_status text not null check (match_status in ('found','other_address','not_found')),
  url text,
  fields jsonb not null,             -- 04 раздел 1, null = не видно
  field_sources jsonb not null,      -- ключ -> {url, seen_at, screenshot, read_by: code|model}
  screenshot_path text,
  purpose text not null,             -- prospect | order_fresh | report | subscription_start
  collected_at timestamptz not null,
  collected_by text not null,        -- scout | manual_import
  status text not null default 'collected',
    -- collected | verified | rejected | failed
  recheck_count int not null default 0,
  fail_reason text
);

create table audits (
  id bigserial primary key,
  company_id bigint references companies,
  snapshot_ids bigint[] not null,
  score_total int not null,
  score_groups jsonb not null,
  derived jsonb not null,            -- reply_share, fresh_review_days
  discrepancies jsonb not null,      -- 04 раздел 1.4
  gaps jsonb not null,               -- [{feature, points_lost, field_ref}]
  strength jsonb,                    -- {text, field_ref}
  observations jsonb,                -- [{text, field_ref, evidence_url}] evidence_url ставит код из field_sources
  onepager_path text,
  status text not null default 'draft', -- draft | checked | rejected | human
  prompt_version text, model text,
  created_at timestamptz default now()
);

-- Сделки, заказы, оплаты
create table deals (
  id bigserial primary key,
  syntony_deal_id text unique not null,
  company_id bigint references companies,
  created_at timestamptz default now()
);

create table orders (
  id bigserial primary key,
  deal_id bigint references deals,
  company_id bigint references companies,
  kind text not null check (kind in ('setup','subscription')),
  branches int not null default 1,
  amount_kzt int not null,
  stage text not null,               -- раздел 4.2 или 4.3
  stage_before_human text, stage_before_revision text,
  period_start date, period_end date,-- для подписки: период 30 дней с даты оплаты
  changes_used int not null default 0,
  revision_until timestamptz,        -- конец 3 дней бесплатных уточнений после сдачи
  created_at timestamptz default now()
);

create table invoices (
  id bigserial primary key,
  order_id bigint references orders,
  part text not null check (part in ('prepay','final','monthly')),
  amount_kzt int not null,
  status text not null,              -- requested | issued | paid | unpaid | failed | cancelled
  provider text not null,            -- stub | syntony
  external_id text,
  paid_via text,                     -- webhook | check | human_confirm
  confirmed_by text, confirm_note text,
  idempotency_key text unique not null,
  created_at timestamptz default now(), updated_at timestamptz
);

-- Материалы и доступы клиента
create table intake (
  order_id bigint primary key references orders,
  checklist jsonb not null,          -- пункт -> {status: missing|received|not_applicable|verified, source_ref}
  updated_at timestamptz default now()
);

create table client_messages (       -- сообщения клиента, на которые ссылается source_ref (msg:<id>)
  id text primary key,               -- id сообщения в Syntony
  company_id bigint references companies,
  order_id bigint references orders,
  text text, received_at timestamptz
);

create table client_files (
  id bigserial primary key,
  company_id bigint references companies,
  order_id bigint references orders,
  kind text not null,                -- photo_real | video_real | logo | price | other
  path text not null, original_name text, mime text, size_bytes bigint,
  message_id text,                   -- из какого сообщения
  received_at timestamptz default now()
);

create table credentials (           -- логин и пароль клиента, только шифром
  id bigserial primary key,
  company_id bigint references companies,
  platform text not null,
  login_hint text,                   -- первые символы логина, для человека
  ciphertext bytea not null,
  created_at timestamptz default now(),
  deleted_at timestamptz
);

create table browser_profiles (
  id bigserial primary key,
  platform text not null,
  account_label text not null,       -- duna-main | client-<company_id>
  path text not null,                -- в BROWSER_PROFILES_DIR
  status text not null default 'needs_login', -- ok | needs_login | blocked
  last_check_at timestamptz,
  unique (platform, account_label)
);

-- Пакет правок
create table change_packages (
  id bigserial primary key,
  order_id bigint references orders,
  version int not null,
  status text not null default 'draft',
    -- draft | in_review | checked | sent_to_client | approved | partially_approved | rejected | superseded
  summary_for_client text,
  approval_message_id text,          -- сообщение клиента с утверждением
  created_at timestamptz default now(),
  unique (order_id, version)
);

create table change_items (
  id bigserial primary key,
  package_id bigint references change_packages,
  n int not null,                    -- номер для клиента: «утверждаю всё, кроме 4 и 7»
  platform text not null,
  field text not null,               -- ключ из platforms.yaml editable_fields или review_reply (ответ на отзыв 4-5: after = текст, source_ref = review:<id>)
  before jsonb, after jsonb,
  source_ref jsonb not null,
  is_nap boolean not null,           -- ставит код по полю (name, address, phones, category)
  nap_consent_ref text,
  counts_to_limit boolean not null,  -- ставит код: true для всех правок подписки, кроме ответов на отзывы
  status text not null default 'draft',
    -- draft | approved | rejected_by_client | waiting_nap_consent | applying | applied | apply_failed
    -- | stopped_unknown_ui | moderation_pending | moderation_ok | moderation_rejected | moderation_pending_long
    -- | waiting_owner_verification (create_card)
  idempotency_key text unique not null, -- sha256(order_id, platform, field, after)
  moderation_checks int not null default 0
);

create table change_log (
  id bigserial primary key,
  change_item_id bigint references change_items,
  action text not null,              -- apply | verify | moderation_check | rollback | create_card
  screenshot_before text, screenshot_after text,
  result text not null,              -- ok | already | failed | stopped_unknown_ui
  details jsonb,
  at timestamptz default now()
);

-- Отзывы
create table reviews (
  id bigserial primary key,
  company_id bigint references companies,
  order_id bigint references orders,  -- в рамках какого заказа отвечаем
  platform text not null,
  external_id text not null,
  rating int, text text, author_label text, posted_at timestamptz,
  reply_text text, reply_language text,
  reply_status text not null default 'new',
    -- new | draft | in_review | needs_client_approval | approved | published | rejected_by_client | skipped
  complaint_status text not null default 'none',
    -- none | proposed | both_agree | filed | accepted | declined
  complaint_text text,
  unique (platform, external_id)
);

-- Контент
create table content_items (
  id bigserial primary key,
  order_id bigint references orders,
  kind text not null,                -- cover | promo_banner | price_image | video
  platform text,
  brief jsonb,
  source_file_ids bigint[],
  result_path text,
  status text not null default 'todo' -- todo | done | in_review | checked | rejected | human
);

-- Проверка и консилиум: council_runs, council_items, review_quality (03, раздел 9)

-- Отчёты
create table reports (
  id bigserial primary key,
  order_id bigint references orders,
  period text not null,              -- setup | 2026-11-08..2026-12-07
  data jsonb not null,
  summary text,
  pdf_path text, xlsx_path text,
  status text not null default 'draft' -- draft | in_review | checked | sent
);

-- Служебное
create table human_tasks (
  id bigserial primary key,
  reason text not null,
  object_type text, object_id bigint,
  details jsonb,
  status text not null default 'open', -- open | taken | closed
  resolution text,
  created_at timestamptz default now(), closed_at timestamptz
);

create table jobs (
  id bigserial primary key,
  type text not null,
  queue text not null default 'main', -- main | browser
  payload jsonb not null,
  status text not null default 'queued', -- queued | running | done | failed | dead
  attempts int not null default 0,
  run_after timestamptz default now(),
  dedup_key text unique,             -- одна живая задача на объект и тип
  last_error text,
  created_at timestamptz default now()
);

create table llm_calls (
  id bigserial primary key,
  prompt_name text, prompt_version text, model text,
  input_hash text, ok boolean, schema_ok boolean,
  tokens_in int, tokens_out int, cost_usd numeric(10,4),
  latency_ms int, at timestamptz default now()
);

create table events_out (            -- события в Syntony (журнал и повтор)
  id bigserial primary key,
  deal_id bigint references deals,
  event text not null, payload jsonb not null,
  status text not null default 'pending', -- pending | sent | failed | stub_logged
  attempts int not null default 0, at timestamptz default now()
);
```

## 4. Статусы и переходы

Переход статуса делает только функция `transition(obj, to)`, которая проверяет таблицу ниже. Прямой `UPDATE` статуса в коде запрещён (проверка в `/code-review`).

### 4.1. Снимок

`collected` → `verified` (проверка кодом: у каждого непустого поля есть источник, типы по словарю, нет признаков формулы)
→ дополнительно у 10% компаний (все три снимка) задача `review.snapshot_recheck`: расхождение больше одного поля → `rejected`
и `scout.collect` заново. Два `rejected` подряд по компании → `human_task`. Страница не загрузилась → `failed` с причиной, 3 попытки, потом `human_task`.
Аудит ждёт перепроверки, только если компания попала в выборку.

### 4.2. Разовый заказ (`orders.stage`, `kind = setup`)

Формат: из какого этапа, в какой, при каком условии. Других переходов нет.

| Из | В | Условие перехода |
|---|---|---|
| (нет) | `created` | Бек вызвал `POST /orders` |
| `created` | `prepay_invoiced` | Счёт `prepay` получил статус `issued` (Syntony вернул номер; в режиме `stub` человек закрыл задачу «выставить счёт», указав номер счёта Kaspi) |
| `prepay_invoiced` | `prepaid` | Счёт `prepay` `paid` (вебхук, `check()` или подтверждение владельца) |
| `prepaid` | `intake` | Сразу: открыт чек-лист, событие `intake_missing` |
| `intake` | `package` | Каждый пункт материалов `received` или `not_applicable`. Пункты доступов к карточкам в это условие **не входят**: их проверяет только переход в `applying` |
| `package` | `client_review` | Пакет прошёл проверку (`checked`) и, если это один из первых 10 пакетов, Тимур его одобрил |
| `client_review` | `package` | Клиент просит переделать (`POST /package/rework`) |
| `client_review` | `applying` | Клиент утвердил всё или часть, и доступ Duna `verified` на площадках утверждённых правок (кроме правок `create_card`) |
| `applying` | `moderation` | Каждая утверждённая правка `applied`, `already` или `apply_failed` с закрытой задачей человека |
| `moderation` | `report` | Каждая правка `moderation_ok`, `moderation_rejected` или `moderation_pending_long`. Правки `create_card` и `waiting_nap_consent` не ждём |
| `report` | `delivered` | Отчёт прошёл проверку, событие `report_ready`, `revision_until` = сейчас + 3 дня |
| `delivered` | `final_invoiced` | Счёт `final` `issued`. Выставляется сразу после сдачи, не дожидаясь конца 3 дней |
| `final_invoiced` | `closed` | Счёт `final` `paid` |
| `delivered`, `final_invoiced`, `closed` | `package` | Доработка: `POST /package/rework`, пока сейчас < `revision_until`. Прежний этап пишется в `orders.stage_before_revision` |
| `report` (после доработки) | прежний этап | Отчёт доработки прошёл проверку: заказ возвращается в `stage_before_revision` |
| любой, кроме `closed`, `cancelled` | `cancelled` | `POST /orders/{id}/cancel` или вебхук `deal.lost`. Задачи заказа останавливаются; уже внесённые правки остаются и попадают в итоговый отчёт, если они есть |
| любой | `human` | Задача человеку по заказу. Прежний этап в `orders.stage_before_human` |
| `human` | прежний этап или `cancelled` | Человек закрыл задачу в админке с решением |

Этапы ожидания клиента (`created`, `prepay_invoiced`, `intake`, `client_review`, `final_invoiced`) не считаются просрочкой исполнителя.

**Правки вне машины этапов.** Две ситуации, когда правки вносятся, не меняя этап заказа:
- согласие на название, адрес, телефон или рубрику пришло позже (`POST /package/approval` с `nap_consent`): эти правки сразу получают
  `executor.apply`, затем `executor.moderation_check`; результат попадает в следующий отчёт (доработки или подписки);
- `create_card`: карточка создаётся, подтверждение владельца (звонок, открытка) делает человек; статус правки `waiting_owner_verification`,
  заказ её не ждёт, в отчёте «ждёт подтверждения владельца».

### 4.3. Подписка (`kind = subscription`)

Один заказ на один период 30 дней с даты оплаты. Следующий период: новый заказ с тем же `deal_id`.
Подписка продаётся только если в сделке есть разовый заказ в статусе `closed`; иначе `POST /orders` отвечает «передать человеку» (тест N28).

| Из | В | Условие перехода |
|---|---|---|
| (нет) | `created` | Первый период: Бек `POST /orders`. Следующий: scheduler за 3 дня до `period_end` текущего |
| `created` | `invoiced` | Счёт `monthly` `issued` |
| `invoiced` | `active` | Счёт `monthly` `paid` и наступил `period_start`. `period_start` = дата оплаты (первый период) или `period_end` прошлого (следующие); `period_end` = +30 дней. Оплата раньше начала периода: заказ ждёт в `invoiced`, scheduler переводит его в `active` в `period_start`. В каждый момент у сделки не больше одного `active` заказа |
| `invoiced` | `paused` | Прошло 5 дней после `period_end` прошлого периода, а счёт не оплачен |
| `paused` | `active` | Счёт оплачен; `period_start` = дата оплаты, `period_end` = +30 дней |
| `active` | `closed` | Наступил `period_end` и отчёт за период отправлен |
| любой, кроме `closed`, `cancelled` | `cancelled` | `POST /orders/{id}/cancel` или `deal.lost` |

Сумма периода: 20 000 ₸ × число филиалов (ТЗ раздел 4).

**Между периодами** (прошлый `closed`, новый `invoiced` или `paused`): отзывы собираются и привязываются к новому заказу,
ответы готовятся и проверяются, но публикуются только после перехода в `active`. Срок ответа на отзыв в это время не идёт.

## 5. Цепочка задач

Каждая строка: кто ставит задачу и по какому событию. `queue=browser` выполняет `browser-worker` (одна задача на профиль одновременно).

| Задача | Очередь | Ставит | Делает | По завершении |
|---|---|---|---|---|
| `scout.collect` (company_id, purpose) | browser | CLI, `POST /companies`, `order.prepare_package`, `report.prepare`, переход подписки в `active` | Три снимка | `snapshot.verify` |
| `snapshot.verify` | main | `scout.collect` | Проверка кодом, `verified`; отбор в выборку 10% | выборка: `review.snapshot_recheck`; иначе по `purpose`: `prospect` и `subscription_start` → `auditor.audit`; `order_fresh` → `order.prepare_package`; `report` → `report.build` |
| `review.snapshot_recheck` | browser | `snapshot.verify` | Повторное чтение 5 полей и точка `scout_fields` | дальше, как `snapshot.verify` для своего `purpose` |
| `auditor.audit` | main | `snapshot.verify` | Балл, пробелы, расхождения; наблюдения только для `prospect` | `prospect`: `review.run(observations)`; `subscription_start`: только запись балла начала периода |
| `review.run` (point, object) | main | любая задача агента | Движок проверки (`03`) | `pass` → следующий шаг точки; `return` → исправление по `03` раздел 5, шаг 6; `human` → `human_task` |
| `intake.parse` | main | `POST /materials` | Двойное чтение материалов (точка `intake_double_read`) | `intake.check` |
| `intake.check` | main | `intake.parse` | Что ещё не хватает | не хватает: событие `intake_missing`; всё есть и заказ в `intake`: переход в `package` и `order.prepare_package` |
| `intake.access_check` | browser | `POST /orders/{id}/access-granted` | Видна ли организация клиента в кабинете Duna | `company_platform_ids.access_status`; доступ `verified`: повторная проверка перехода в `applying` и `executor.apply` для ждущих одобренных ответов и жалоб этой площадки |
| `order.prepare_package` | main | `intake.check` | Ставит `scout.collect(order_fresh)` и `reviews.collect` | `order.check_package_ready` |
| `order.check_package_ready` | main | `order.prepare_package`, `snapshot.verify(order_fresh)`, `reviews.collect` (отзывов 0), итог `review.run` точек `review_reply_*` (любой вердикт) | Идемпотентная проверка: свежие снимки `verified` и у каждого ответа на отзыв есть итог проверки. Отзывов 0: ответы готовы сразу. Ответы с итогом `human` в пакет не входят | всё готово и пакета этой версии ещё нет: `executor.package` |
| `reviews.collect` | browser | `order.prepare_package`; scheduler раз в день (подписка) | Отзывы за 30 дней без ответа / новые отзывы | `executor.review_reply` на каждый |
| `executor.review_reply` | main | `reviews.collect` | Ответ | `review.run(review_reply_positive/negative)`; для оценок 3 и ниже ещё `executor.complaint` |
| `executor.complaint` | main | `executor.review_reply` | Двойное решение (точка `complaint`) | оба «да»: жалоба в `/approvals` клиента |
| `executor.package` | main | `order.check_package_ready`, `POST /package/rework` | Новая версия пакета, прошлая `superseded`. Ответы на отзывы 4-5 добавляет код как пункты пакета с полем `review_reply` (ниже) | нужен контент: `creator.make` на каждый, пакет ждёт; иначе `review.run(package)` |
| `creator.make` | main | `executor.package` | Картинка или видео | `review.run(content)`; когда весь контент версии пакета готов: `review.run(package)` |
| `executor.apply` (список правок) | browser | переход заказа в `applying`; одобрение в `/approvals`; позднее `nap_consent`; `pass` точки `change_request`; `pass` точки `review_reply_positive` для заказа подписки в `active`. Всегда только для площадок, где `access_status = verified`; остальное ждёт и запускается повторно из `intake.access_check` | Внесение правок, публикация ответов, подача жалоб | `review.run(applied)` по каждой правке; `executor.moderation_check` через 24 ч |
| `executor.moderation_check` | browser | `executor.apply` | Сверка публичной карточки | до 3 раз через 24 ч, потом `moderation_pending_long` |
| `executor.change_request` | main | `POST /orders/{id}/change-requests` | Просьба клиента → правки (новая версия пакета заказа периода, сразу `approved` для всех правок, кроме названия, адреса, телефона, рубрики) | `review.run(change_request)` |
| `report.prepare` | main | заказ перешёл в `report`; `period_end` подписки | Ставит `scout.collect(report)` | (через `snapshot.verify`) `report.build` |
| `report.build` | main | `snapshot.verify(report)` | Данные отчёта, резюме, PDF, XLSX. Сбор не запускает | `review.run(report)` |
| `invoice.issue` | main | `POST /orders/{id}/invoices`; scheduler (подписка) | Действие счёта (раздел 9) | событие `invoice_issued` или задача «выставить счёт» |
| `manager.sla_scan` | main | scheduler каждые 15 мин | Просрочки | повтор задачи агента, тревога |
| `manager.digest` | main | scheduler 9:00 | Сводка | Telegram |
| `review.weekly_audit` | main | scheduler по понедельникам | 10% работ уровня «один проверяющий» → консилиум | `review_quality` |
| `review.monthly_quality` | main | scheduler 1-го числа | Разбор качества по моделям и точкам | сообщение Тимуру |

Контент, готовый после того, как клиент уже получил пакет: новая версия пакета содержит только новые пункты и уходит клиенту на утверждение отдельно; утверждённые пункты прошлой версии остаются в силе.

Ошибки задачи: 3 попытки с паузой 1, 5, 30 минут, потом `dead` и `human_task`. `dedup_key` не даёт поставить вторую живую задачу на тот же объект.

## 6. Обёртка над моделями

```python
result = llm.run(prompt="auditor-observations", input={...}, images=[...])
# result.data прошёл JSON-схему; result.model, result.prompt_version пишутся в объект и llm_calls
```

- Модель берётся из `config/models.yaml`. Для промтов с картинками обёртка проверяет `vision: true` у модели.
- Ответ не прошёл схему: один повтор с текстом ошибки, потом исключение.
- Нет переменной шаблона: исключение до вызова.
- Температура из файла промта.
- **Проверка при запуске сервиса:** для каждой точки `review_policy.yaml` хотя бы один проверяющий не из семейства автора;
  у моделей для картинок `vision: true`; у всех моделей есть цена. Не выполнено: сервис не стартует (негативный тест N19).

`config/models.yaml`:

```yaml
models:
  claude-sonnet-5-5:          {family: claude, vision: true,  usd_per_mtok_in: <из прайса>, usd_per_mtok_out: <из прайса>}
  claude-haiku-4-5-20251001:  {family: claude, vision: true,  usd_per_mtok_in: <...>, usd_per_mtok_out: <...>}
  claude-opus-5-5:            {family: claude, vision: true,  usd_per_mtok_in: <...>, usd_per_mtok_out: <...>}
  <модель GPT>:               {family: gpt,    vision: true,  ...}
  <модель Gemini>:            {family: gemini, vision: true,  ...}
authors:                      # кто пишет работу
  scout-extract: claude-haiku-4-5-20251001
  scout-match: claude-haiku-4-5-20251001
  auditor-observations: claude-sonnet-5-5
  intake-extract: claude-sonnet-5-5
  executor-package: claude-sonnet-5-5
  executor-review-reply: claude-sonnet-5-5
  executor-complaint: claude-sonnet-5-5
  executor-change-request: claude-sonnet-5-5
  executor-fix-reply: claude-sonnet-5-5
  creator-brief: claude-sonnet-5-5
  report-writer: claude-sonnet-5-5
  manager-digest: claude-haiku-4-5-20251001
  council-secretary: claude-sonnet-5-5
usd_to_kzt: 460
```

Названия моделей GPT и Gemini и цены всех моделей Ербол берёт из официальной документации и прайсов на дату работы,
записывает ссылку и дату в `docs/decisions.md` (решение Р6 в `00-README.md`).

## 7. Формат файла промта

```markdown
---
name: auditor-observations
version: 1
agent: auditor            # scout | auditor | executor | creator | manager | council
temperature: 0.5
---
## system
...
## user
...{{переменные}}... (источники переменных: 04-slovar-dannyh.md раздел 3)
## schema
```json
{ ... }
```
```

Изменение текста = `version + 1`, через скилл `zhol-prompt`.

## 8. Движок проверки

`zhol/review/`: одна функция `review.run(point, object_type, object_id)`. Уровни, состав, круги, подсчёт голосов и вердикт
описаны в `03-proverka-i-konsilium.md`. Движок строится в S0 и проверяется на синтетических случаях; точки подключаются на своих этапах.

## 9. Заказ, счёт, оплата

Решение: **заказ создаётся, когда клиент согласился; счёт выставляет Жол действием; исполняет Syntony.**

1. Клиент согласился в переписке → Бек вызывает `POST /api/v1/orders` (`syntony_deal_id`, `id_2gis` или ссылка 2ГИС, филиалов, `kind`).
   Жол создаёт `deal` (если нет), `order`, возвращает `order_id`. Бек хранит `order_id` в сделке Syntony.
2. Бек вызывает `POST /api/v1/orders/{id}/invoices` (`part`). Сумму считает Жол по прайсу (ТЗ раздел 4), не Бек и не модель.
   `kind=subscription` без закрытого разового заказа в сделке: отказ «передать человеку».
3. Задача `invoice.issue` вызывает `InvoiceProvider.issue()`:
   - `syntony`: запрос в Syntony, ответ `issued` с номером или `failed`;
   - `stub`: `human_task` «выставить счёт Kaspi на N ₸, заказ X», статус `requested`. Человек выставляет счёт в Kaspi и закрывает задачу в админке,
     указав номер счёта: статус `issued`.
4. Оплата отмечается одним из трёх путей, других в коде нет (негативный тест N6):
   - вебхук Syntony `payment.paid` с проверенной подписью;
   - `InvoiceProvider.check()` вернул `paid`;
   - **подтверждение человека** в админке: только роль `owner`, обязательный комментарий (номер чека Kaspi), запись `paid_via=human_confirm`.
     Этот путь нужен, пока Syntony не подключён, и для разбора сбоев. Скриншот клиента в чате оплатой не считается.
5. Ежемесячный счёт подписки Жол выставляет сам (scheduler) тем же действием.

## 10. API

Авторизация: `X-Api-Key` для Syntony; вебхуки с подписью HMAC-SHA256 в `X-Signature`. Админка: вход по логину и паролю (S9).
Все ответы для Бека содержат `order_id` и понятные подписи для клиента.

| Метод | Путь | Кто | Что делает |
|---|---|---|---|
| GET | `/api/v1/export/mailing?since=` | Syntony | Компании с проверенным аудитом для рассылки (ТЗ 6.1) |
| POST | `/api/v1/companies` | Бек | Входящий лид: ссылка 2ГИС → компания и `scout.collect(prospect)` |
| GET | `/api/v1/companies/{id_2gis}/audit` | Бек | Аудит и наблюдения |
| POST | `/api/v1/orders` | Бек | Создать заказ (раздел 9) |
| POST | `/api/v1/orders/{id}/invoices` | Бек | Счёт `prepay`, `final` или первый `monthly` |
| GET | `/api/v1/orders/{id}/status` | Бек | Этап заказа, счета, что ждём от клиента |
| GET | `/api/v1/orders/{id}/intake` | Бек | Чего не хватает, текстом для клиента |
| POST | `/api/v1/orders/{id}/materials` | Бек | Сообщения (`message_id`, текст) и файлы клиента |
| POST | `/api/v1/orders/{id}/access-granted` | Бек | Клиент говорит, что добавил Duna менеджером (площадка) |
| POST | `/api/v1/credentials` | Бек | Логин и пароль клиента в хранилище, ответ только `credential_id` |
| GET | `/api/v1/orders/{id}/package` | Бек | Пакет: пронумерованные правки с подписями, картинка «было и станет» |
| POST | `/api/v1/orders/{id}/package/approval` | Бек | `{"approve": "all"}` или `{"approve_except": [4,7]}` или `{"approve_only": [1,2]}`, `message_id`; согласия на NAP: `{"nap_consent": [3], "message_id": ...}` |
| POST | `/api/v1/orders/{id}/package/rework` | Бек | Клиент просит переделать: текст и `message_id` |
| GET | `/api/v1/orders/{id}/approvals` | Бек | Что ждёт согласия клиента вне пакета: ответы на отзывы с оценкой 3 и ниже или с жалобой, жалобы на отзывы (`object_type`: `review_reply` или `complaint`), в разовой работе и в подписке |
| POST | `/api/v1/approvals/{id}` | Бек | Решение клиента: `approve`, `reject`, `edit` с текстом |
| GET | `/api/v1/orders/{id}/report` | Бек | Подписанная ссылка на PDF |
| POST | `/api/v1/orders/{id}/change-requests` | Бек | Просьба клиента по подписке (`message_id`, текст, файлы) |
| POST | `/api/v1/orders/{id}/cancel` | Бек | Отказ от подписки или заказа |
| GET | `/api/v1/deals/{syntony_deal_id}/current-order` | Бек | Текущий заказ сделки (для подписки: заказ текущего или следующего периода). Запросы Бека по закрытому заказу подписки отклоняются с указанием текущего `order_id` |
| POST | `/api/v1/human-tasks` | Бек | Задача человеку |
| POST | `/webhooks/syntony` | Syntony | `payment.paid`, `dnc.set`, `deal.lost` |
| GET | `/files/{token}` | кто угодно со ссылкой | Файл по подписанной ссылке, срок 30 дней |

**Вебхуки Syntony → Жол:**
- `payment.paid`: `{syntony_deal_id, order_id, invoice_external_id, part, amount_kzt, paid_at}`;
- `dnc.set`: `{syntony_deal_id или whatsapp}` → `companies.dnc = true`, исходящие события по компании отменяются;
- `deal.lost`: `{syntony_deal_id, reason}` → незавершённые заказы сделки `cancelled`.

**События Жол → Syntony** (`events_out`, в режиме `stub` журнал + Telegram ответственному):
`subscription_order_created` (с `order_id` нового периода), `invoice_issued` (с `order_id`), `invoice_failed`, `intake_missing`, `package_ready`, `review_approval_needed`, `work_applied`, `report_ready`,
`subscription_period_closed`, `subscription_paused`. Состав каждого события в `docs/syntony-contract.md` (S3).

Какие тесты из `source/duna-agent-integration-notes-v1.md` (раздел «Проверки перед продом») обеспечивает Жол:
3 и 4 (аудит с наблюдениями есть или его нет), 5 (`dnc`), 6 (счёт только по вызову после согласия), 7 (оплата только подтверждённая).
Остальные тесты на стороне Бека и Syntony.

## 11. Файлы и ссылки

- Файлы лежат в `FILES_DIR`. Наружу только через `/files/{token}`: подписанная ссылка (HMAC, срок 30 дней), `PUBLIC_BASE_URL` в `.env`.
- Загрузка от клиента: проверка типа по содержимому, лимит размера 50 МБ, имя файла не используется как путь.

## 12. Доступы и профили браузера

- Профиль браузера на пару (площадка, аккаунт): `duna-main` для аккаунта Duna и `client-<company_id>`, если клиент дал логин и пароль.
- Пароль расшифровывается только командой `python -m zhol.cli browser-login --platform X --account Y`, которую запускает человек:
  команда открывает браузер, подставляет логин и пароль, человек проходит код подтверждения вместе с клиентом. Агенты пароль не получают никогда.
- После входа агент работает через профиль. Профиль недействителен: `browser_profiles.status = needs_login` и `human_task`.
- Отключение клиента: `credentials.deleted_at`, профиль `client-<id>` удаляется, `human_task` «убрать доступ Duna в кабинетах клиента».

## 13. Время и расписание

- Часовой пояс `Asia/Almaty`, рабочие дни пн-пт.
- Все функции, зависящие от времени, берут его из `timeutil.now()`. В тестах и приёмке время подменяется: `python -m zhol.cli ... --now 2026-11-01T09:00`.
  Так проверяются сводка в 9:00, ожидание модерации 24-72 ч, конец периода подписки без реального ожидания.

## 14. Переменные окружения (`.env.example`)

```
DATABASE_URL=
FILES_DIR=/data/files
BROWSER_PROFILES_DIR=/data/profiles
PUBLIC_BASE_URL=
FILE_LINK_SECRET=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
TELEGRAM_BOT_TOKEN=
RESPONSIBLE_TG_CHAT_ID=
SYNTONY_MODE=stub              # stub | live
SYNTONY_BASE_URL=
SYNTONY_API_KEY=
SYNTONY_WEBHOOK_SECRET=
ZHOL_API_KEY=
INVOICE_PROVIDER=stub          # stub | syntony
SECRETS_KEY=
TZ=Asia/Almaty
```
