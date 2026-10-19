"""Local HTML for the Yandex and Google Selenium adapters.

The pages use each source's own selectors. They do not call a live map site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class Card:
    number: int
    name: str
    address: str
    phone: str | None
    website: str | None
    whatsapp: str | None
    instagram: str | None

    @property
    def yandex_id(self) -> str:
        return f"880000010{self.number:02d}"

    @property
    def google_id(self) -> str:
        return f"ChIJAlmatySalon{self.number:02d}"


def cards() -> list[Card]:
    rows = []
    for number in range(1, 13):
        missing = number == 3
        phone = None if missing else f"+7 701 101 {number:02d} {number:02d}"
        digits = None if phone is None else "7" + "".join(ch for ch in phone if ch.isdigit())[1:]
        rows.append(
            Card(
                number=number,
                name="Салон Север" if number == 1 else f"Салон Алматы {number:02d}",
                address=f"улица Абая, {number}",
                phone=phone,
                website=None if missing else f"https://salon{number:02d}.example/card",
                whatsapp=None if digits is None else f"https://wa.me/{digits}",
                instagram=None if missing else f"https://instagram.com/salon.almaty.{number:02d}",
            )
        )
    return rows


def _yandex_link(card: Card) -> str:
    return (
        f'<a class="search-snippet-view__link" href="/maps/org/salon/{card.yandex_id}/">{card.name}</a>'
    )


def _google_link(card: Card) -> str:
    return (
        '<div style="height:60px">'
        f'<a class="maps-place-link" href="/maps/place/data=!1s{card.google_id}">{card.name}</a>'
        "</div>"
    )


def yandex_one_page() -> str:
    """A single result page that says the list ended. A missing pager is not this signal."""
    card = cards()[0]
    return f"""<!DOCTYPE html><html><body>
<h1>Одна страница</h1>
<section class="search-list-view" data-results-complete="1">{_yandex_link(card)}</section>
</body></html>"""


def google_stall() -> str:
    """A feed that does not grow and does not say it ended."""
    return f"""<!DOCTYPE html><html><body>
<div role="feed" id="feed">{_google_link(cards()[0])}</div>
</body></html>"""


def yandex_search(page: int) -> str:
    found = cards()
    if page <= 1:
        chosen = [found[0], found[0], *found[1:6]]
        pager = '<a class="search-pagination__page" href="/maps/162/almaty/search/page/2">2</a>'
    else:
        chosen = found[6:]
        pager = ""
    links = "\n".join(_yandex_link(card) for card in chosen)
    return f"""<!DOCTYPE html><html><body>
<h1>Салон красоты</h1>
<section class="search-list-view">{links}</section>
<nav>{pager}</nav>
</body></html>"""


def yandex_card(card: Card) -> str:
    phone = ""
    if card.phone:
        phone = f'<div class="card-phones-view"><a href="tel:{card.phone}">{card.phone}</a></div>'
    website = ""
    if card.website:
        website = f'<a class="orgpage-contacts-view__website" href="{card.website}">сайт</a>'
    extra = ""
    if card.whatsapp:
        extra += f'<a href="{card.whatsapp}">WhatsApp</a>'
    if card.instagram:
        extra += f'<a href="{card.instagram}">Instagram</a>'
    return f"""<!DOCTYPE html><html><body>
<article class="orgpage-view">
<h1 class="orgpage-header-view__header">{card.name}</h1>
<div class="orgpage-categories-info-view">Салон красоты</div>
<div class="business-rating-badge-view__rating">4.6</div>
<div class="business-rating-badge-view__reviews">12 отзывов</div>
<a class="orgpage-header-view__address" href="/maps/geo/1">{card.address}</a>
<div>Алматы</div>
{phone}{website}{extra}
</article>
</body></html>"""


def google_search() -> str:
    found = cards()
    first = "".join(_google_link(card) for card in found[:4])
    second = "".join(_google_link(card) for card in (found[0], *found[4:7]))
    third = "".join(_google_link(card) for card in found[7:])
    chunks = json.dumps([second, third], ensure_ascii=False)
    return f"""<!DOCTYPE html><html><body>
<h1>Салон красоты</h1>
<div role="feed" id="feed" style="height:200px;overflow:auto">{first}</div>
<script>
const chunks = {chunks};
let cursor = 0;
const feed = document.getElementById("feed");
feed.addEventListener("scroll", function () {{
  if (cursor >= chunks.length) {{
    feed.setAttribute("data-feed-complete", "1");
    return;
  }}
  feed.insertAdjacentHTML("beforeend", chunks[cursor]);
  cursor += 1;
  if (cursor >= chunks.length) feed.setAttribute("data-feed-complete", "1");
}});
</script>
</body></html>"""


def google_card(card: Card) -> str:
    phone = ""
    if card.phone:
        phone = f'<button type="button" data-item-id="phone:tel">{card.phone}</button>'
    website = ""
    if card.website:
        website = f'<a data-item-id="authority" href="{card.website}">сайт</a>'
    extra = ""
    if card.whatsapp:
        extra += f'<a href="{card.whatsapp}">WhatsApp</a>'
    if card.instagram:
        extra += f'<a href="{card.instagram}">Instagram</a>'
    return f"""<!DOCTYPE html><html><body>
<div class="place-card">
<h1 class="DUwDvf">{card.name}</h1>
<button class="DkEaL" type="button">Салон красоты</button>
<div class="F7nice"><span aria-hidden="true">4.6</span><span class="review-count">(12)</span></div>
<button type="button" data-item-id="address">{card.address}</button>
<div>Алматы</div>
{phone}{website}{extra}
</div>
</body></html>"""


def captcha_page() -> str:
    return """<!DOCTYPE html><html><body>
<main class="search-list-view">
<h1>Подтвердите, что вы не робот</h1>
<div data-access="captcha">SmartCaptcha</div>
</main>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        body = _body(path)
        if body is None:
            self.send_error(404)
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _body(path: str) -> str | None:
    if "капча" in path.casefold():
        return captcha_page()
    if path.startswith("/maps/org/salon/"):
        external_id = path.strip("/").split("/")[-1]
        card = next((item for item in cards() if item.yandex_id == external_id), None)
        return None if card is None else yandex_card(card)
    if path.startswith("/maps/place/"):
        external_id = path.split("1s", 1)[-1].strip("/")
        card = next((item for item in cards() if item.google_id == external_id), None)
        return None if card is None else google_card(card)
    if path.startswith("/maps/search/"):
        if "лента-застой" in path:
            return google_stall()
        return google_search()
    if path.startswith("/maps/162/almaty/search/"):
        if "одна-страница" in path:
            return yandex_one_page()
        page = 2 if "/page/2" in path else 1
        if "/page/" in path and page == 1:
            return None
        return yandex_search(page)
    return None
