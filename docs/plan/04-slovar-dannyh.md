# Словарь данных

Единственный источник имён полей, значений и переменных промтов. Если имя поля или значение где-то в пакете
отличается от этого файла, правильно здесь.

## 1. Поля снимка карточки

Снимок хранится по площадкам: `card_snapshots.fields` (JSONB) с ключами ниже. `null` = «не видно на странице», это не 0.
`field_ref` в аудите и пакетах пишется как `<площадка>.<ключ>`, например `2gis.prices`.

### 1.1. 2ГИС (`platform = '2gis'`)

| Ключ | Тип | Значения | Колонка эталона CSV | Как читается |
|---|---|---|---|---|
| `rating` | число | 0-5 | `2гис_рейтинг` | Рейтинг в карточке |
| `ratings_count` | целое | | `2гис_оценок` | Число оценок |
| `reviews_count` | целое | | `2гис_отзывов` | Число отзывов с текстом |
| `photos_count` | целое | | `2гис_фото` | Счётчик фото |
| `reviews_shown` | целое | | `2гис_показано_отзывов` | Сколько отзывов показано на странице (до 50) |
| `replies_count` | целое | | `2гис_ответов` | Сколько из показанных имеют официальный ответ |
| `last_review_date` | дата | ГГГГ-ММ-ДД | `2гис_последний_отзыв` | Дата самого свежего из показанных |
| `is_ad` | boolean | | `2гис_реклама` (0/1) | Рекламная карточка |
| `branches` | целое | | `2гис_филиалов` | Число филиалов сети |
| `has_phone` | boolean | | `2гис_телефон` (0/1) | |
| `has_whatsapp` | boolean | | `2гис_whatsapp` (0/1) | |
| `has_instagram` | boolean | | `2гис_instagram` (0/1) | |
| `link` | строка | адрес или пусто | `2гис_ссылка` | Сайт или ссылка на запись |
| `prices` | строка | `company_price`, `photo_only`, `empty` | `2гис_цены`: «прайс компании», «фото прайса», «пусто» | Вкладка цен |
| `online_booking` | boolean | | `2гис_онлайн_запись` (0/1) | Кнопка или ссылка онлайн-записи |
| `name` | строка | | `название` | |
| `address` | строка | | `адрес_2гис` | |
| `category` | строка | | (нет в CSV) | Рубрика |
| `phones` | список строк | | `контактные_телефоны` | |
| `whatsapp` | строка | номер | `номер_whatsapp` | |
| `instagram` | строка | аккаунт | `аккаунт_instagram` | |

### 1.2. Яндекс (`platform = 'yandex'`)

| Ключ | Тип | Значения | Колонка эталона CSV |
|---|---|---|---|
| `match_status` (колонка таблицы, не поле JSON) | строка | `found`, `other_address`, `not_found` | `яндекс_статус`: «найдена», «найдена, адрес другой», «не найдена» |
| `rating` | число | | `яндекс_рейтинг` |
| `ratings_count` | целое | | `яндекс_оценок` |
| `reviews_count` | целое | | `яндекс_отзывов` |
| `photos_count` | целое | | `яндекс_фото` |
| `prices` | строка | `yes`, `yes_outdated`, `no` | `яндекс_цены`: «есть», «есть, давно не обновлялись», «нет» |
| `news_count` | целое | | `яндекс_новости` |
| `owner_maintains` | boolean | | `яндекс_владелец_следит` (0/1) |
| `name`, `address`, `category`, `phones` | | | (нет в CSV) |

### 1.3. Google (`platform = 'google'`)

| Ключ | Тип | Значения | Колонка эталона CSV |
|---|---|---|---|
| `match_status` (колонка таблицы) | строка | `found`, `other_address`, `not_found` | `google_статус`: «найдена», «найдена, адрес или филиал другой», «не найдена» |
| `rating` | число | | `google_рейтинг` |
| `reviews_count` | целое | | `google_отзывов` |
| `website` | строка | | `google_сайт` |
| `owner_claimed` | boolean или null | | `google_права_заявлены` (1/0/пусто; пусто = карточка не найдена) |
| `name`, `address`, `category`, `phones` | | | (нет в CSV) |

`match_status` хранится в колонке `card_snapshots.match_status`. Промт `scout-match` отвечает `same`, `other_address`, `not_match`;
код переводит: `same` → `found`, `other_address` → `other_address`, все `not_match` → `not_found`.

### 1.4. Поля, которые считает код (не собираются)

| Поле | Где хранится | Как считается |
|---|---|---|
| `reply_share` (доля ответов) | `audits.derived` | `2gis.replies_count / 2gis.reviews_shown`; `reviews_shown = 0` → 0 |
| `fresh_review_days` | `audits.derived` | `collected_at` снимка 2ГИС минус `2gis.last_review_date` |
| `discrepancies` (расхождения) | `audits.discrepancies` (список) | Код сравнивает `name`, `address`, `category` между найденными карточками после нормализации (регистр, кавычки, «ул.», «просп.», пробелы). Каждое расхождение: поле, площадки, значения. По методике 1.1 считаются только название, адрес и рубрика |

В эталоне колонка `расхождения` заполнена вручную свободным текстом и включает расхождения рейтинга. При импорте эталона (S0)
текст сохраняется в `audits.discrepancies` как `[{"legacy_text": "..."}]` только для сверки балла 50 из 50.
Новые аудиты считают расхождения кодом по правилу выше. Разница балла на этом основании в S2 допустима и объясняется в отчёте.

### 1.5. Адаптер для `score()`

`score()` из `source/audit_card.py` читает плоскую строку с русскими колонками. В S0 пишется адаптер
`to_score_row(snapshots, derived, discrepancies) -> dict` по таблицам 1.1-1.4 (обратный перевод значений: `company_price` →
«прайс компании» и т. д.), а логика `score()` переносится без изменений. Тест: импорт эталона → адаптер → `score()` = `балл_итого` у 50 из 50.

Поле `дата_просмотра` эталона = `collected_at` снимка 2ГИС.

## 2. Данные бизнеса клиента

Хранятся на уровне компании, а не заказа: `company_profiles` (версии, последняя действующая). Заполняются из `intake-extract`
и подтверждений клиента. Используются пакетом, ответами на отзывы, контентом и подпиской.

Ключи: `name`, `address`, `entrance_hint`, `hours`, `phones`, `messengers`, `website`, `socials`, `payment_methods`,
`services[]` (`name`, `category`, `price`, `price_from`, `price_to`, `duration_min`), `features`, `brand` (`logo_file_id`,
`colors[]`: два-три цвета HEX, если клиент дал; иначе берутся с логотипа кодом), `category`, `description`.
Каждое значение: `value`, `source_ref`, `needs_confirmation`. `category` и `description` хранятся только если их дал клиент.
В E7.2 описание пишется в кабинет Яндекс Бизнеса. В 2ГИС и Google описание остаётся только для чтения. Счётчик цен Яндекса
не пишется: цена услуги берётся из утверждённого списка услуг.

`source_ref` имеет вид `msg:<id сообщения Syntony>` или `file:<client_files.id>` или `field:<путь в profile>`.

## 3. Переменные промтов: откуда берутся

| Промт | Переменная | Источник |
|---|---|---|
| `scout-extract` | `platform`, `url`, `seen_at` | Задача сбора |
| | `fields_list` | Ключи раздела 1.1, 1.2 или 1.3 для этой площадки, кроме тех, что код уже прочитал из разметки |
| | `page_text` | Текст блоков страницы, который собрал браузерный сценарий |
| `scout-match` | `name_2gis`, `address_2gis`, `phones_2gis` | Снимок 2ГИС |
| | `candidates_json` | До 5 результатов поиска: `candidate_id`, название, адрес, телефоны, url |
| `auditor-observations` | `name`, `niche`, `city` | `companies` |
| | `score_total`, `score_groups_json`, `gaps_json` | Результат `score()` и расчёта пробелов (код) |
| | `fields_json` | Поля всех трёх снимков + `derived` + `discrepancies` |
| `intake-extract` | `known_from_cards_json` | Поля `name`, `address`, `category`, `phones`, `hours` из свежих снимков |
| | `materials_json` | Сообщения и файлы из `POST /materials`: каждый элемент с `msg:<id>` или `file:<id>` |
| `executor-package` | `business_data_json` | Действующая версия `company_profiles` |
| | (ответы на отзывы) | В промт не передаются: код добавляет проверенные ответы 4-5 в пакет пунктами `review_reply` |
| | `files_json` | `client_files` компании и готовый контент: id, тип, описание |
| | `snapshots_json` | Свежие снимки (не старше 24 ч) |
| | `audit_json` | Последний аудит: балл, пробелы, расхождения |
| | `platform_fields_json` | Список редактируемых полей и ограничений площадки из `config/platforms.yaml` (раздел `editable_fields`) |
| `executor-review-reply` | `business_data_json` | `company_profiles` |
| | `rating`, `author_label`, `posted_at`, `review_text` | `reviews` |
| `executor-complaint` | `address` | Адрес из `company_profiles`, если нет, из снимка этой площадки |
| `executor-change-request` | `message_id`, `message_text`, `files_json` | `POST /change-requests` |
| | `changes_used` | Счётчик правок текущего периода подписки (код) |
| `creator-brief` | `constraints_json` | `config/platforms.yaml`, раздел `media` площадки |
| | `brand_json` | `company_profiles.brand` |
| `report-writer` | `report_data_json` | Данные отчёта, которые собрал код (S7) |
| `manager-digest` | `digest_data_json` | SQL-выборки управленца (S9) |
| `council-*` | `checklist` | `council/checklists/<точка>.md` |
| | `object_json` | Проверяемый объект без рассуждений автора |
| | `materials_json` | Всё, на что объект ссылается (`source_ref`, `field_ref`, файлы, скриншоты) |
| `council-r1`, `council-r2`, `council-recheck` | `point` | Имя точки из `review_policy.yaml` |
| `council-r2` | `own_issues_json` | Замечания этого участника из круга 1 |
| | `others_issues_labeled_json` | Замечания остальных участников круга 1 с метками A1, B2 (без названий моделей) |
| `council-recheck` | `round` | Номер круга: 2 или 3 |
| | `previous_issues_json` | Принятые группы прошлого круга с номерами `n` |
| | `author_reply_json` | Ответ автора по каждому `n` (`executor-fix-reply.replies`) |
| `council-secretary` | `all_issues_json` | Все замечания кругов 1 и 2 с id, меткой участника, важностью, местом, текстом, правкой |
| `executor-fix-reply` | `original_prompt_name` | Имя промта, которым сделана работа |
| | `issues_json` | Принятые группы с номерами `n` |
| | `object_json`, `materials_json` | То же, что получил консилиум |
| `executor-package` | `name`, `branches` | `companies`, `orders.branches` |
| `executor-complaint` | `platform`, `name`, `rating`, `review_text` | `reviews`, `companies` |
| `executor-change-request` | `name`, `business_data_json`, `snapshots_json` | Как в `executor-package` |
| | `platform_fields_json` | `config/platforms.yaml`, раздел `editable_fields` |
| `creator-brief` | `kind`, `platform`, `purpose` | `content_items` (задача от `executor-package`, поле `content_needed`) |
| | `business_data_json`, `files_json` | Как в `executor-package` |
| `creator-text` | `kind`, `platform`, `purpose`, `language` | Задача черновика: вид текста, площадка, цель, язык `ru` или `kk` |
| | `business_data_json` | Только утверждённые факты профиля и название с городом компании |
| | `missing_json` | Пути пропущенных и неподтверждённых фактов, без их значений |
| | `constraints_json` | `config/platforms.yaml`, раздел `text_limits` |
| `report-writer` | `name`, `period` | `companies`; `reports.period` (`setup` или даты периода) |
| `manager-digest` | `date` | `timeutil.now()`, дата в Алматы |

Если переменной нет во входе, обёртка падает с ошибкой до вызова модели.
