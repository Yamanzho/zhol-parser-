---
name: intake-extract
version: 1
agent: executor
temperature: 0.2
---
## system
Ты разбираешь материалы, которые прислал клиент (салон, барбершоп и т. п.), в структурные данные бизнеса для карточек на картах.
Материалы: текст сообщений, ответы на вопросы, фото прайса, файлы.

Правила:
1. Записывай только то, что есть в материалах. Ничего не додумывай: ни услуг, ни цен, ни графика.
2. У каждого значения укажи `source_ref` в формате `msg:<id сообщения>` или `file:<id файла>`, откуда взято.
3. Не уверен в прочтении (размытое фото, неоднозначная запись «от 5000», непонятная единица): `needs_confirmation: true`
   и вопрос клиенту в `questions`.
4. Цены в тенге целым числом. «от 5000» записывай как `price_from: 5000`. Диапазон: `price_from` и `price_to`.
5. Если материалы противоречат друг другу (два разных графика), не выбирай сам: оба варианта в `conflicts`.
6. Отвечай только JSON по схеме.

## user
Компания: {{name}}
Уже известно из карточек: {{known_from_cards_json}}
Материалы клиента:
{{materials_json}}
(фото приложены, у каждого указан file_id)

## schema
```json
{
  "type": "object",
  "required": ["business_data", "questions", "conflicts"],
  "properties": {
    "business_data": {
      "type": "object",
      "properties": {
        "name": {"$ref": "#/$defs/val"},
        "address": {"$ref": "#/$defs/val"},
        "entrance_hint": {"$ref": "#/$defs/val"},
        "hours": {"$ref": "#/$defs/val"},
        "phones": {"$ref": "#/$defs/val"},
        "messengers": {"$ref": "#/$defs/val"},
        "website": {"$ref": "#/$defs/val"},
        "socials": {"$ref": "#/$defs/val"},
        "payment_methods": {"$ref": "#/$defs/val"},
        "services": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["name", "source_ref", "needs_confirmation"],
            "properties": {
              "name": {"type": "string"},
              "category": {"type": ["string", "null"]},
              "price": {"type": ["integer", "null"]},
              "price_from": {"type": ["integer", "null"]},
              "price_to": {"type": ["integer", "null"]},
              "duration_min": {"type": ["integer", "null"]},
              "source_ref": {"type": "string"},
              "needs_confirmation": {"type": "boolean"}
            },
            "additionalProperties": false
          }
        },
        "features": {"$ref": "#/$defs/val"},
        "brand_colors": {"$ref": "#/$defs/val"}
      },
      "additionalProperties": false
    },
    "questions": {"type": "array", "items": {"type": "string"}},
    "conflicts": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["field", "variants"],
        "properties": {
          "field": {"type": "string"},
          "variants": {"type": "array", "items": {"type": "object"}}
        }
      }
    }
  },
  "$defs": {
    "val": {
      "type": ["object", "null"],
      "required": ["value", "source_ref", "needs_confirmation"],
      "properties": {
        "value": {},
        "source_ref": {"type": "string"},
        "needs_confirmation": {"type": "boolean"}
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false
}
```
