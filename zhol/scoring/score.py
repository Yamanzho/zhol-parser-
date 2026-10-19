from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=8)
def _load(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_scoring(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        root = Path(os.environ.get("ZHOL_ROOT") or Path.cwd())
        path = Path(os.environ.get("ZHOL_CONFIG_DIR") or root / "config") / "scoring.yaml"
    return _load(str(Path(path).resolve()))


def num(x, default=0.0):
    try:
        return float(str(x).replace(",", ".").replace("%", ""))
    except ValueError:
        return default


def yes(x):
    return str(x).strip().lower() in {"1", "да", "заявлены"}


def fresh_days(r):
    try:
        seen = date.fromisoformat(r["дата_просмотра"])
        last = date.fromisoformat(r["2гис_последний_отзыв"])
    except ValueError:
        return None
    return (seen - last).days


def _ladder(value: float, steps: list[list[float]]) -> int:
    for threshold, points in steps:
        if value >= threshold:
            return points
    return 0


def score(r, cfg: dict[str, Any] | None = None):
    """Балл по методике 1.1: четыре группы по 25."""
    cfg = cfg or load_scoring()
    rv, pr, ph, ct = cfg["reviews"], cfg["prices"], cfg["photos"], cfg["contacts"]
    ratings, rating = num(r["2гис_оценок"]), num(r["2гис_рейтинг"])
    share = num(r["2гис_доля_ответов"])
    share = share / 100 if share > 1 else share

    a = 0
    a += _ladder(ratings, rv["ratings_count"])
    a += _ladder(rating, rv["rating"])
    a += rv["fresh_points"] if fresh_days(r) is not None and fresh_days(r) <= rv["fresh_days_max"] else 0
    a += _ladder(share, rv["reply_share"]) or (rv["reply_share_positive_points"] if share > 0 else 0)
    a += rv["yandex_ratings_points"] if num(r["яндекс_оценок"]) >= rv["yandex_ratings_min"] else 0
    a += rv["google_reviews_points"] if num(r["google_отзывов"]) >= rv["google_reviews_min"] else 0

    b = pr["twogis_prices"].get(r["2гис_цены"], 0)
    b += pr["online_booking_points"] if yes(r["2гис_онлайн_запись"]) else 0
    b += pr["yandex_prices"].get(r["яндекс_цены"], 0)
    b += pr["yandex_news_points"] if num(r["яндекс_новости"]) > 0 else 0

    photos, ya_photos = num(r["2гис_фото"]), num(r["яндекс_фото"])
    c = _ladder(photos, ph["twogis_photos"])
    c += _ladder(ya_photos, ph["yandex_photos"])

    d = ct["flag_points"] * sum(yes(r[k]) for k in ct["flags"])
    link = r["2гис_ссылка"].strip().lower()
    d += ct["website_points"] if link and not any(x in link for x in ct["social_domains"]) else 0  # соцсеть не сайт
    d += ct["yandex_status"].get(r["яндекс_статус"], 0)
    d += ct["yandex_owner_points"] if yes(r["яндекс_владелец_следит"]) else 0
    g = r["google_статус"]
    d += ct["google_found_points"] if g == "найдена" else ct["google_found_other_points"] if g.startswith("найдена") else 0
    d += ct["google_claimed_points"] if g.startswith("найдена") and yes(r["google_права_заявлены"]) else 0
    # сравнивать есть с чем, только если карточка Google найдена
    d += ct["no_discrepancies_points"] if g.startswith("найдена") and not r["расхождения"].strip() else 0

    return {"отзывы": a, "цены": b, "фото": c, "контакты": d, "итого": a + b + c + d}
