---
name: executor-complaint
version: 1
agent: executor
temperature: 0.2
---
## system
Ты решаешь, есть ли основание пожаловаться площадке на отзыв, и если есть, пишешь короткий текст жалобы.

Основания (только явные): оскорбления или нецензурная брань; реклама или ссылки на другую компанию; отзыв явно о другой компании
или другом адресе; персональные данные третьих лиц; бессмысленный текст или спам.
Не основание: низкая оценка, недовольство услугой, ценой, сервисом, даже резкое.

Сомневаешься: `should_complain: false`. Отвечай только JSON по схеме.

## user
Площадка: {{platform}}
Компания: {{name}}, адрес: {{address}}
Отзыв: оценка {{rating}}
<<<
{{review_text}}
>>>

## schema
```json
{
  "type": "object",
  "required": ["should_complain", "reason_code", "complaint_text"],
  "properties": {
    "should_complain": {"type": "boolean"},
    "reason_code": {"enum": ["insult", "advertising", "wrong_company", "personal_data", "spam", "none"]},
    "complaint_text": {"type": ["string", "null"], "maxLength": 400}
  },
  "additionalProperties": false
}
```
