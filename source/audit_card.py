#!/usr/bin/env python3
"""Агент-аудитор карточек на картах (пилот).

Берёт салоны из outputs/karty-salony-almaty-2026-10.csv, пересчитывает балл по
методике 1.1 (outputs/karty-metodika-2026-10.md), сверяет с баллом в базе и для
каждого салона пилота пишет одностраничник audit.html и текст первого сообщения.

Работает только со строками, собранными вручную 30.09 (слои эталона). В остальных
500 строках поля Яндекса и Google посчитаны формулой из 2ГИС, писать салону по ним нельзя.

Запуск:
  python3 karty-agents/audit_card.py            # сверка баллов + пилот
  python3 karty-agents/audit_card.py --check    # только сверка баллов
"""
import csv
import html
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT.parent / "outputs" / "karty-salony-almaty-2026-10.csv"
OUT = ROOT / "out"
PILOT = ROOT / "pilot.csv"

VERIFIED_LAYERS = {"верх (реклама)", "середина", "окраины"}
PILOT_SIZE = 25
SOCIAL = ("tiktok.com", "instagram.com", "facebook.com", "vk.com", "t.me")
MIN_RATINGS = 50  # «трафик есть»: от 50 оценок в 2ГИС


def num(x, default=0.0):
    try:
        return float(str(x).replace(",", ".").replace("%", ""))
    except ValueError:
        return default


def yes(x):
    return str(x).strip().lower() in {"1", "да", "заявлены"}


def load_verified():
    rows = list(csv.DictReader(DB.open(encoding="utf-8-sig")))
    return [r for r in rows if r["слой"] in VERIFIED_LAYERS]


def score(r):
    """Балл по методике 1.1: четыре группы по 25."""
    ratings, rating = num(r["2гис_оценок"]), num(r["2гис_рейтинг"])
    share = num(r["2гис_доля_ответов"])
    share = share / 100 if share > 1 else share

    a = 0
    a += 6 if ratings >= 200 else 4 if ratings >= 50 else 2 if ratings >= 10 else 0
    a += 5 if rating >= 4.8 else 4 if rating >= 4.5 else 2 if rating >= 4.0 else 0
    a += 4 if fresh_days(r) is not None and fresh_days(r) <= 30 else 0
    a += 6 if share >= 0.6 else 4 if share >= 0.2 else 2 if share > 0 else 0
    a += 2 if num(r["яндекс_оценок"]) >= 20 else 0
    a += 2 if num(r["google_отзывов"]) >= 20 else 0

    b = {"прайс компании": 10, "фото прайса": 4}.get(r["2гис_цены"], 0)
    b += 6 if yes(r["2гис_онлайн_запись"]) else 0
    b += {"есть": 5, "есть, давно не обновлялись": 3}.get(r["яндекс_цены"], 0)
    b += 4 if num(r["яндекс_новости"]) > 0 else 0

    photos, ya_photos = num(r["2гис_фото"]), num(r["яндекс_фото"])
    c = 15 if photos >= 50 else 10 if photos >= 20 else 5 if photos >= 5 else 0
    c += 10 if ya_photos >= 30 else 7 if ya_photos >= 10 else 4 if ya_photos >= 1 else 0

    d = 2 * sum(yes(r[k]) for k in ("2гис_телефон", "2гис_whatsapp", "2гис_instagram"))
    link = r["2гис_ссылка"].strip().lower()
    d += 2 if link and not any(x in link for x in SOCIAL) else 0  # соцсеть не сайт
    d += {"найдена": 5, "найдена, адрес другой": 2}.get(r["яндекс_статус"], 0)
    d += 2 if yes(r["яндекс_владелец_следит"]) else 0
    g = r["google_статус"]
    d += 5 if g == "найдена" else 2 if g.startswith("найдена") else 0
    d += 2 if g.startswith("найдена") and yes(r["google_права_заявлены"]) else 0
    # сравнивать есть с чем, только если карточка Google найдена
    d += 3 if g.startswith("найдена") and not r["расхождения"].strip() else 0

    return {"отзывы": a, "цены": b, "фото": c, "контакты": d, "итого": a + b + c + d}


def fresh_days(r):
    try:
        seen = date.fromisoformat(r["дата_просмотра"])
        last = date.fromisoformat(r["2гис_последний_отзыв"])
    except ValueError:
        return None
    return (seen - last).days


def findings(r):
    """Сильные стороны и точки роста: только то, что видно в строке базы."""
    good, gaps = [], []
    ratings, rating = int(num(r["2гис_оценок"])), num(r["2гис_рейтинг"])
    share = num(r["2гис_доля_ответов"])
    share = share / 100 if share > 1 else share

    if ratings >= 50:
        good.append(f"{count(ratings, 'оценка', 'оценки', 'оценок')} в 2ГИС: клиенты вас находят и пишут о вас")
    if rating >= 4.8:
        good.append(f"рейтинг {r['2гис_рейтинг']} в 2ГИС, это уровень лучших салонов города")
    if num(r["2гис_фото"]) >= 50:
        good.append(f"{int(num(r['2гис_фото']))} фото в 2ГИС, карточка выглядит живой")
    if r["2гис_цены"] == "прайс компании":
        good.append("прайс в 2ГИС заполнен")
    if share >= 0.6:
        good.append("вы отвечаете на большинство отзывов в 2ГИС")

    if r["2гис_цены"] == "пусто":
        gaps.append(("в 2ГИС пустая вкладка цен", "заполним прайс в 2ГИС по вашим услугам"))
    elif r["2гис_цены"] == "фото прайса":
        gaps.append(("цены в 2ГИС висят фотографией прайса, поиск по услугам их не видит",
                     "перенесём прайс в 2ГИС текстом, по услугам"))
    if share == 0:
        gaps.append(("из последних отзывов в 2ГИС ни на один нет ответа салона",
                     "подготовим ответы на отзывы и шаблоны на будущее"))
    elif share < 0.2:
        gaps.append((f"ответ салона есть только у {round(share * 100)}% последних отзывов в 2ГИС",
                     "подготовим ответы на отзывы и шаблоны на будущее"))
    g = r["google_статус"]
    if g == "не найдена":
        gaps.append(("в Google Картах карточку по названию и адресу не нашли",
                     "создадим и подтвердим карточку в Google"))
    elif g.startswith("найдена") and g != "найдена":
        gaps.append(("в Google Картах адрес или филиал не совпадает с 2ГИС",
                     "приведём карточку Google к одному адресу"))
    elif not yes(r["google_права_заявлены"]):
        gaps.append(("права на карточку в Google не заявлены, её может править кто угодно",
                     "заявим права на карточку в Google"))
    y = r["яндекс_статус"]
    if y == "не найдена":
        gaps.append(("в Яндекс Картах карточку не нашли", "создадим карточку в Яндекс Бизнесе"))
    elif num(r["яндекс_оценок"]) == 0:
        gaps.append(("в Яндекс Картах у карточки нет ни одной оценки",
                     "поможем собрать первые отзывы в Яндексе без накруток"))
    if y.startswith("найдена") and not yes(r["яндекс_владелец_следит"]):
        gaps.append(("в Яндексе нет отметки «Владелец следит за актуальностью»",
                     "подтвердим карточку в Яндекс Бизнесе"))
    if r["расхождения"].strip():
        gaps.append((f"данные расходятся между картами: {r['расхождения'].strip()}",
                     "сведём название, адрес и телефон к одному виду"))
    if not yes(r["2гис_онлайн_запись"]):
        gaps.append(("в 2ГИС нет онлайн-записи", "подключим кнопку записи"))
    if num(r["2гис_фото"]) < 20:
        gaps.append((f"в 2ГИС всего {int(num(r['2гис_фото']))} фото",
                     "добавим фото зала, работ и команды"))
    return good[:3], gaps


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def count(n, one, few, many):
    return f"{n:,}".replace(",", "\u00a0") + " " + plural(n, one, few, many)


def message(r, good, gaps):
    n = int(num(r["2гис_оценок"]))
    ratings = count(n, "оценка", "оценки", "оценок")
    rating = r["2гис_рейтинг"].replace(".", ",")
    a, b = (gaps + [("", "")])[:2]
    points = a[0] if not b[0] else f"{a[0]}, а ещё {b[0]}"
    return (
        "Здравствуйте! Это Duna.kz, мы занимаемся маркетингом для бизнеса в Алматы.\n\n"
        f"Мы разбирали, как {'барбершопы' if r['тип'] == 'барбер' else 'салоны красоты'} выглядят на 2ГИС, Яндекс и Google Картах, "
        f"и посмотрели вашу карточку «{r['название']}». Вас явно находят: {ratings} "
        f"в 2ГИС и рейтинг {rating}. При этом {points}. Из-за таких мелочей часть людей, "
        f"которые уже ищут {'барбершоп' if r['тип'] == 'барбер' else 'салон'} рядом, уходит к соседям.\n\n"
        "Собрали короткий разбор вашей карточки на одну страницу: что уже хорошо и что "
        "можно поправить. Прислать?"
    )


def render_html(r, s, good, gaps):
    e = html.escape
    good_li = "".join(f"<li>{e(g)}</li>" for g in good) or "<li>карточка в 2ГИС есть и работает</li>"
    gap_rows = "".join(f"<tr><td>{e(g)}</td><td>{e(f)}</td></tr>" for g, f in gaps[:6])
    bars = "".join(
        f'<div class="bar"><span>{k.capitalize()}</span><div><i style="width:{v * 4}%"></i></div><b>{v}/25</b></div>'
        for k, v in s.items() if k != "итого"
    )
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Разбор карточки: {e(r['название'])}</title>
<style>
:root{{--ink:#14213d;--mute:#5b6475;--acc:#e07a1f;--bg:#fff;--line:#e6e8ee}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
.page{{width:800px;margin:0 auto;padding:40px}}
h1{{font-size:26px;margin:0 0 4px}} .sub{{color:var(--mute);margin-bottom:24px}}
.top{{display:flex;gap:32px;align-items:center;margin-bottom:24px}}
.total{{font-size:56px;font-weight:700;color:var(--acc);line-height:1}} .total small{{font-size:18px;color:var(--mute)}}
.bars{{flex:1}} .bar{{display:grid;grid-template-columns:90px 1fr 50px;gap:10px;align-items:center;margin:6px 0}}
.bar div{{background:var(--line);height:8px;border-radius:4px}} .bar i{{display:block;height:8px;border-radius:4px;background:var(--acc)}}
h2{{font-size:17px;margin:24px 0 8px}} ul{{margin:0;padding-left:20px}}
table{{width:100%;border-collapse:collapse}} td{{border-top:1px solid var(--line);padding:8px 6px;vertical-align:top}}
td:first-child{{width:55%}} th{{text-align:left;color:var(--mute);font-weight:500;padding:0 6px 6px}}
.foot{{margin-top:28px;color:var(--mute);font-size:13px}}
</style></head><body><div class="page">
<h1>{e(r['название'])}</h1>
<div class="sub">{e(r['адрес_2гис'])} · разбор карточек на картах · данные на {e(date.fromisoformat(r['дата_просмотра']).strftime('%d.%m.%Y'))}</div>
<div class="top"><div class="total">{s['итого']}<small>/100</small></div><div class="bars">{bars}</div></div>
<h2>Что уже хорошо</h2><ul>{good_li}</ul>
<h2>Что можно поправить</h2>
<table><tr><th>Что видно сейчас</th><th>Что сделаем</th></tr>{gap_rows}</table>
<div class="foot">Смотрели публичные страницы 2ГИС, Яндекс Карт и Google Карт в день просмотра.
«Не нашли» значит: карточка не нашлась по названию и адресу. Duna.kz</div>
</div></body></html>"""


def main():
    rows = load_verified()
    mismatches = []
    for r in rows:
        s = score(r)
        if s["итого"] != int(num(r["балл_итого"])):
            mismatches.append((r["название"], int(num(r["балл_итого"])), s["итого"]))
    print(f"Проверенных салонов: {len(rows)}. Балл совпал у {len(rows) - len(mismatches)}.")
    for name, old, new in mismatches:
        print(f"  расхождение: {name}: в базе {old}, пересчёт {new}")
    if "--check" in sys.argv:
        return

    cand = [r for r in rows if num(r["2гис_оценок"]) >= MIN_RATINGS and r["wa_me"].strip()]
    cand.sort(key=lambda r: (score(r)["итого"], -num(r["2гис_оценок"])))
    pilot = cand[:PILOT_SIZE]

    OUT.mkdir(exist_ok=True)
    fields = ["id_2gis", "название", "тип", "балл", "оценок_2гис", "wa_me", "instagram",
              "url_2gis", "точек_роста", "отправлено", "ответили", "договорились",
              "сумма_тг", "оплатили", "часов_на_исполнение", "заметки"]
    with PILOT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in pilot:
            s = score(r)
            good, gaps = findings(r)
            d = OUT / r["id_2gis"]
            d.mkdir(exist_ok=True)
            (d / "audit.html").write_text(render_html(r, s, good, gaps), encoding="utf-8")
            (d / "message.txt").write_text(message(r, good, gaps) + "\n", encoding="utf-8")
            w.writerow({"id_2gis": r["id_2gis"], "название": r["название"], "тип": r["тип"],
                        "балл": s["итого"], "оценок_2гис": r["2гис_оценок"], "wa_me": r["wa_me"],
                        "instagram": r["url_instagram"], "url_2gis": r["url_2gis"],
                        "точек_роста": len(gaps)})
    print(f"Пилот: {len(pilot)} салонов из {len(cand)} с трафиком и WhatsApp → {PILOT.relative_to(ROOT.parent)}")


if __name__ == "__main__":
    main()
