# Рабочие промты агентов

Кладутся в репозиторий в `prompts/` без изменений. Формат: `01-arkhitektura.md`, раздел 7.
Модель автора: `config/models.yaml`. Кто проверяет результат: `03-proverka-i-konsilium.md` и `config/review_policy.yaml`.
Откуда берётся каждая переменная `{{...}}`: `04-slovar-dannyh.md`, раздел 3.

| Промт | Агент | Этап | Что делает | Проверка результата (точка) |
|---|---|---|---|---|
| `scout-extract` | Барлау | S1 | Поля карточки из текста и скриншота | `scout_fields` (выборка 10%) |
| `scout-match` | Барлау | S1 | Та же ли компания в Яндексе и Google | `match` |
| `auditor-observations` | Тексер | S2 | Сильная сторона и 1-3 наблюдения | `observations` |
| `intake-extract` | Шебер | S4 | Материалы клиента → данные бизнеса | `intake_double_read` |
| `executor-package` | Шебер | S4 | Пакет правок | `package` |
| `executor-review-reply` | Шебер | S4, S8 | Ответ на отзыв | `review_reply_positive` / `review_reply_negative` |
| `executor-complaint` | Шебер | S4, S8 | Основание для жалобы на отзыв | `complaint` |
| `executor-change-request` | Шебер | S8 | Просьба клиента → правки | `change_request` |
| `executor-fix-reply` | любой автор | S0 | Исправление по замечаниям проверки | повторный круг той же точки |
| `creator-brief` | Өнер | S6 | Макет картинки или видео | `content` |
| `report-writer` | Тексер | S7, S8 | Резюме отчёта | `report` |
| `manager-digest` | Управленец | S9 | Утренняя сводка | код сверяет числа |
| `council-r1` | проверяющий | S0 | Круг 1: независимая проверка по чек-листу | |
| `council-r2` | проверяющий | S0 | Круг 2: ответ на чужие замечания | |
| `council-recheck` | проверяющий | S0 | Повторный круг: исправлено ли | |
| `council-secretary` | секретарь | S0 | Склейка одинаковых замечаний | |

Чек-листы точек проверки лежат в `council/checklists/`.

**Как менять промт:** только через скилл `zhol-prompt`. Любая правка текста = новая `version`, прогон золотого набора до и после.
