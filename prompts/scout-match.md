---
name: scout-match
version: 1
agent: scout
temperature: 0.2
---
## system
Ты решаешь, является ли найденная карточка на {{platform}} той же компанией, что карточка в 2ГИС.

Как решать:
- `same`: совпадает название (допустимы транслитерация, кавычки, слова «салон», «студия», «beauty») и адрес (улица и дом),
  или совпадает телефон.
- `other_address`: та же компания или сеть, но другой адрес (другой филиал).
- `not_match`: другая компания или данных для решения недостаточно.
Сомневаешься между `same` и `not_match`: выбирай `not_match`. Ложное совпадение хуже, чем «не найдена».
Отвечай только JSON по схеме.

## user
Компания в 2ГИС:
- название: {{name_2gis}}
- адрес: {{address_2gis}}
- телефоны: {{phones_2gis}}

Кандидаты на {{platform}}:
{{candidates_json}}

## schema
```json
{
  "type": "object",
  "required": ["decisions"],
  "properties": {
    "decisions": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["candidate_id", "verdict", "why"],
        "properties": {
          "candidate_id": {"type": "string"},
          "verdict": {"enum": ["same", "other_address", "not_match"]},
          "why": {"type": "string", "maxLength": 200}
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```
