#!/bin/sh
# Примеры вызовов. Рабочий режим требует токен. Демо на 127.0.0.1:8765 токен не требует.
# Не публикуйте API в сеть: по умолчанию он слушает только localhost.

API="${ZHOL_API:-http://127.0.0.1:18080}"
TOKEN="${ZHOL_ADMIN_TOKEN:-}"
AUTH=""
if [ -n "$TOKEN" ]; then
  AUTH="Authorization: Bearer ${TOKEN}"
fi

curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"platform":"2gis","city":"Алматы","query":"салон красоты","limit":5,"idempotency_key":"demo-salon-1"}' \
  "$API/api/v1/jobs"

echo
echo "Статус: curl -sS -H \"$AUTH\" $API/api/v1/jobs/1"
echo "Результат: curl -sS -H \"$AUTH\" \"$API/api/v1/jobs/1/results?limit=50&offset=0\""
echo "Пауза: curl -sS -X POST -H \"$AUTH\" -H \"X-Zhol-Request: admin\" $API/api/v1/jobs/1/pause"
echo "Продолжить: curl -sS -X POST -H \"$AUTH\" -H \"X-Zhol-Request: admin\" $API/api/v1/jobs/1/resume"
echo "Остановить: curl -sS -X POST -H \"$AUTH\" -H \"X-Zhol-Request: admin\" $API/api/v1/jobs/1/stop"
echo "Excel: curl -sS -H \"$AUTH\" -o companies.xlsx \"$API/api/v1/jobs/1/export?format=xlsx\""
echo "Описание API в рабочем режиме: $API/docs"
