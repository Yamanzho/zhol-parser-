"""Deterministic companies for the local demo. Nothing here is a live listing."""

from __future__ import annotations

CATALOG = (
    {"name": "Салон Абая", "phone": "+7 701 111 22 33", "address": "проспект Абая, 10", "category": "салон красоты", "city": "Алматы", "platform": "2gis"},
    {"name": "Студия Норд", "phone": "+7 701 222 33 44", "address": "улица Гоголя, 5", "category": "салон красоты", "city": "Алматы", "platform": "2gis"},
    {"name": "Барбер Тау", "phone": "+7 702 333 44 55", "address": "улица Панфилова, 88", "category": "парикмахерская", "city": "Алматы", "platform": "google"},
    {"name": "Люкс Наиля", "phone": "+7 705 444 55 66", "address": "проспект Достык, 132", "category": "салон красоты", "city": "Алматы", "platform": "yandex"},
    {"name": "Форма Фитнес", "phone": "+7 707 555 66 77", "address": "улица Толе би, 101", "category": "фитнес-клуб", "city": "Алматы", "platform": "google"},
    {"name": "Дентал Самал", "phone": "+7 708 666 77 88", "address": "микрорайон Самал-2, 32", "category": "стоматология", "city": "Алматы", "platform": "2gis"},
    {"name": "Салон Есиль", "phone": "+7 717 111 00 21", "address": "проспект Туран, 19", "category": "салон красоты", "city": "Астана", "platform": "2gis"},
    {"name": "Барбер Сарыарка", "phone": "+7 717 222 00 32", "address": "улица Сарыарка, 4", "category": "парикмахерская", "city": "Астана", "platform": "google"},
    {"name": "Клиника Нура", "phone": "+7 717 333 00 43", "address": "проспект Республики, 24", "category": "медицинский центр", "city": "Астана", "platform": "yandex"},
    {"name": "Салон Орда", "phone": "+7 725 111 00 54", "address": "проспект Тауке хана, 12", "category": "салон красоты", "city": "Шымкент", "platform": "google"},
    {"name": "Фитнес Барыс", "phone": "+7 725 222 00 65", "address": "улица Байтурсынова, 7", "category": "фитнес-клуб", "city": "Шымкент", "platform": "2gis"},
    {"name": "Стоматология Рауан", "phone": None, "address": "улица Казыбек би, 15", "category": "стоматология", "city": "Шымкент", "platform": "yandex"},
)

CITIES = ("Алматы", "Астана", "Шымкент")


def same_job(job: dict, platform: str, city: str, query: str, limit: int) -> bool:
    return (
        job["platform"] == platform
        and str(job["city"]).casefold() == city.casefold()
        and str(job["query"]).casefold() == query.casefold()
        and int(job["limit"]) == limit
    )


def query_hit(row: dict, query: str) -> bool:
    folded = query.casefold()
    return folded in str(row["category"]).casefold() or folded in str(row["name"]).casefold()
