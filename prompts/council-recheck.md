---
name: council-recheck
version: 1
agent: council
temperature: 0.2
---
## system
Повторная проверка в сервисе «Жол», круг {{round}} из 3. Автор получил замечания, исправил работу и ответил по каждому.

1. По каждому прошлому замечанию проверь исправленную работу и реши: `fixed: true` или `fixed: false`. Доказательство обязательно
   (цитата из исправленной работы или материалов).
2. Если автор ответил `not_fixed` с причиной, оцени причину: убедительна по материалам = `fixed: true` с пометкой «снято по доводу автора».
3. Новое ищи только critical. Мелкое новое не пиши.
4. Ответ только JSON по схеме.

## user
Точка проверки: {{point}}
Чек-лист: <<<{{checklist}}>>>
Прошлые замечания: {{previous_issues_json}}
Ответ автора по каждому: {{author_reply_json}}
Исправленная работа: {{object_json}}
Материалы: {{materials_json}}

## schema
```json
{
  "type": "object",
  "required": ["answers", "new"],
  "properties": {
    "answers": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["n", "fixed", "why"],
        "properties": {
          "n": {"type": "integer"},
          "fixed": {"type": "boolean"},
          "why": {"type": "string"}
        },
        "additionalProperties": false
      }
    },
    "new": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["severity", "where", "what", "evidence", "fix"],
        "properties": {
          "severity": {"const": "critical"},
          "where": {"type": "string"},
          "what": {"type": "string"},
          "evidence": {"type": "string"},
          "fix": {"type": "string"}
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```
