#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

trap 'echo "Сбор прерван. Excel сохранён. Запустите снова: ./run_all.sh"; exit 130' INT

output="output/almaty_businesses.xlsx"
groups=(
  "Красота"
  "HoReCa"
  "Медицина"
  "Спорт"
  "Автомобильный бизнес"
  "Другие локальные услуги"
)

for group in "${groups[@]}"; do
  python collect_almaty.py \
    --group "$group" \
    --resume \
    --output "$output"
  status=$?
  if [[ "$status" -eq 0 ]]; then
    continue
  fi
  if [[ "$status" -eq 130 || "$status" -eq 2 ]]; then
    echo "Сбор прерван. Excel сохранён. Запустите снова: ./run_all.sh"
    exit "$status"
  fi
  echo "Группа «${group}» остановилась с ошибкой ${status}. Сбор не продолжен."
  exit "$status"
done

echo "FULL ALMATY COLLECTION FINISHED"
echo "Output: ${output}"
