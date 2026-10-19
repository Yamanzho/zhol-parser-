"""Local HTTP site for the Selenium load run. It never calls a live map site."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

LOG = Path("/tmp/zhol-browser-lab/requests.log")
PAGE_SIZE = 10


@dataclass(frozen=True)
class Card:
    firm_id: str
    name: str
    address: str | None
    phone: str | None
    whatsapp: str | None
    instagram: str | None
    invalid_social: str | None = None
    empty_name: bool = False
    malformed: bool = False
    restricted: bool = False
    query: str = "Салон красоты"


def _id(number: int) -> str:
    return f"700000010000{number:05d}"


def _phone(number: int) -> str:
    return f"+7 701 {number // 100:03d} {number % 100:02d} 00"


def _cards() -> list[Card]:
    rows: list[Card] = []
    for offset in range(1, 51):
        number = 11000 + offset
        shared = offset <= 4
        rows.append(
            Card(
                firm_id=_id(number),
                name="Салон Сирень" if shared else f"Салон Лаборатория {offset:02d}",
                address=None if offset == 12 else f"улица Тестовая, {offset}",
                phone=None if offset in {10, 11} else _phone(number),
                whatsapp=None if offset in {13, 15} else f"https://wa.me/7{number:010d}",
                instagram=None if offset == 14 else f"https://instagram.com/lab.salon.{offset:02d}",
                invalid_social="https://example.invalid/not-a-contact" if offset == 15 else None,
            )
        )
    rows.extend(
        [
            Card(_id(12001), "Салон Пустой Телефон", "улица Тестовая, 101", None, "https://wa.me/77010001201", "https://instagram.com/lab.empty.phone", query="Проверка полей"),
            Card(_id(12002), "Салон Пустой Адрес", None, _phone(12002), "https://wa.me/77010001202", "https://instagram.com/lab.empty.address", query="Проверка полей"),
            Card(_id(12003), "Салон Без Соцсетей", "улица Тестовая, 103", _phone(12003), None, None, query="Проверка полей"),
            Card(_id(12004), "Салон Битая Ссылка", "улица Тестовая, 104", _phone(12004), None, None, invalid_social="https://example.invalid/not-a-contact", query="Проверка полей"),
            Card(_id(12005), "Салон Пустое Имя", "улица Тестовая, 105", _phone(12005), "https://wa.me/77010001205", "https://instagram.com/lab.empty.name", empty_name=True, query="Проверка полей"),
            Card(_id(12006), "Салон Сломанная Карточка", "улица Тестовая, 106", _phone(12006), None, None, malformed=True, query="Проверка полей"),
            Card(_id(12007), "Салон Закрытый", "улица Тестовая, 107", _phone(12007), None, None, restricted=True, query="Проверка доступа"),
        ]
    )
    for offset in range(1, 21):
        number = 13000 + offset
        rows.append(
            Card(
                _id(number),
                f"Салон Пауза {offset:02d}",
                f"улица Продолжения, {offset}",
                _phone(number),
                f"https://wa.me/7{number:010d}",
                f"https://instagram.com/lab.pause.{offset:02d}",
                query="Салон пауза",
            )
        )
    for offset in range(1, 13):
        number = 14000 + offset
        rows.append(
            Card(
                _id(number),
                f"Салон Восстановление {offset:02d}",
                f"улица Восстановления, {offset}",
                _phone(number),
                f"https://wa.me/7{number:010d}",
                f"https://instagram.com/lab.recover.{offset:02d}",
                query="Салон восстановление",
            )
        )
    for offset in range(1, 9):
        number = 15000 + offset
        rows.append(
            Card(
                _id(number),
                f"Салон Остановка {offset:02d}",
                f"улица Остановки, {offset}",
                _phone(number),
                f"https://wa.me/7{number:010d}",
                f"https://instagram.com/lab.stop.{offset:02d}",
                query="Салон остановка",
            )
        )
    return rows


CARDS = _cards()
BY_ID = {card.firm_id: card for card in CARDS}
QUERIES = {
    "Салон красоты": [card for card in CARDS if card.query == "Салон красоты"],
    "Проверка полей": [card for card in CARDS if card.query == "Проверка полей"],
    "Салон пауза": [card for card in CARDS if card.query == "Салон пауза"],
    "Салон восстановление": [card for card in CARDS if card.query == "Салон восстановление"],
    "Салон остановка": [card for card in CARDS if card.query == "Салон остановка"],
    "Проверка доступа": [card for card in CARDS if card.query == "Проверка доступа"],
}


def catalog_path() -> Path:
    return Path(__file__).with_name("catalog.json")


def write_catalog() -> None:
    payload = {
        "queries": {name: [card.firm_id for card in cards] for name, cards in QUERIES.items()},
        "cards": [
            {
                "firm_id": card.firm_id,
                "name": card.name,
                "address": card.address,
                "phone": card.phone,
                "whatsapp": card.whatsapp,
                "instagram": card.instagram,
                "invalid_social": card.invalid_social,
                "empty_name": card.empty_name,
                "malformed": card.malformed,
                "restricted": card.restricted,
                "query": card.query,
            }
            for card in CARDS
        ],
    }
    catalog_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _page_items(cards: list[Card], page: int) -> list[Card]:
    start = (page - 1) * PAGE_SIZE
    items = cards[start : start + PAGE_SIZE]
    if items:
        items = [items[0], *items]
    if page == 3 and cards:
        items = [cards[0], *items]
    return items


def search_html(query: str, page: int) -> bytes | None:
    cards = QUERIES.get(query)
    if not cards:
        return None
    pages = (len(cards) + PAGE_SIZE - 1) // PAGE_SIZE
    if page < 1 or page > pages:
        return None
    encoded = query.replace(" ", "%20")
    links = []
    for card in _page_items(cards, page):
        links.append(f'<div><a href="/almaty/firm/{card.firm_id}">{card.name}</a></div>')
    next_links = []
    if page < pages:
        next_links.append(f'<a href="/almaty/search/{encoded}/page/{page + 1}">{page + 1}</a>')
    body = (
        "<!DOCTYPE html><html><body>"
        f"<h1>{query}</h1>"
        + "".join(links)
        + "".join(next_links)
        + "</body></html>"
    )
    return body.encode("utf-8")


def card_html(card: Card) -> bytes:
    if card.malformed:
        return b"<!DOCTYPE html><html><body><div>broken card</div></body></html>"
    if card.restricted:
        return (
            "<!DOCTYPE html><html><body><h1>Доступ ограничен</h1>"
            "<p>Доступ запрещён. Обход не выполняется.</p></body></html>"
        ).encode("utf-8")
    name = "" if card.empty_name else card.name
    parts = [
        "<!DOCTYPE html><html><body><article>",
        f"<h1><span>{name}</span></h1>",
    ]
    if card.address:
        parts.append(f'<div><a href="/almaty/geo/{card.firm_id}">{card.address}</a></div>')
    parts.append("<div>Бостандыкский район, Алматы</div>")
    parts.append(f'<a href="/almaty/firm/{card.firm_id}">карточка</a>')
    if card.phone:
        digits = "".join(ch for ch in card.phone if ch.isdigit())
        parts.append(f'<a href="tel:+{digits}">{card.phone}</a>')
    if card.whatsapp:
        parts.append(f'<a href="{card.whatsapp}" aria-label="WhatsApp">WhatsApp</a>')
    if card.instagram:
        parts.append(f'<a href="{card.instagram}" aria-label="Instagram">Instagram</a>')
    if card.invalid_social:
        parts.append(f'<a href="{card.invalid_social}" aria-label="WhatsApp">WhatsApp</a>')
    parts.append("</article></body></html>")
    return "".join(parts).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        _log(path)
        if path == "/redirect-external":
            self.send_response(302)
            self.send_header("Location", "https://2gis.kz/almaty")
            self.end_headers()
            return
        firm = path.rstrip("/").rsplit("/firm/", 1)
        if len(firm) == 2 and firm[1].isdigit():
            card = BY_ID.get(firm[1])
            body = None if card is None else card_html(card)
        elif "/search/" in path:
            body = _search_body(path)
        else:
            body = None
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _search_body(path: str) -> bytes | None:
    tail = unquote(path.split("/search/", 1)[1]).strip("/")
    if "/page/" in tail:
        query, number = tail.rsplit("/page/", 1)
        page = int(number) if number.isdigit() else 0
    else:
        query, page = tail, 1
    return search_html(query, page)


def _log(path: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {path}\n")


def main() -> None:
    write_catalog()
    LOG.write_text("", encoding="utf-8")
    server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    print("listening 8765", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
