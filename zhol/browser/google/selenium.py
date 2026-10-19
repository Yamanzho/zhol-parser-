"""Google Maps card and feed selectors. They are not the 2GIS or Yandex selectors.

A loopback fixture may use these selectors without a grant.
A public host is accepted only when ZHOL_GOOGLE_BROWSER_GRANT names that source's scope.
"""

from __future__ import annotations

from urllib.parse import quote

from zhol.agents.scout.browser_google import accept_url

READY_SEARCH_JS = 'return !!document.querySelector(\'div[role="feed"]\')'
READY_CARD_JS = "return !!document.querySelector('div.place-card, h1.DUwDvf')"
PAGE_TEXT_JS = """
return {
  text: document.body ? document.body.innerText : "",
  access: (function () {
    const node = document.querySelector("[data-access]");
    return node ? (node.getAttribute("data-access") || "") : "";
  })()
};
"""
READ_SEARCH_JS = r"""
return (function () {
  const feed = document.querySelector('div[role="feed"]');
  if (!feed) return {items: [], duplicates: 0, done: false, end: "missing"};
  const items = [];
  const seen = new Set();
  let duplicates = 0;
  for (const link of feed.querySelectorAll("a.maps-place-link[href], a.hfpxzc[href]")) {
    const href = link.getAttribute("href") || "";
    const match = href.match(/1s(ChIJ[A-Za-z0-9_\-]+)/) || href.match(/1s(0x[0-9a-f]+:0x[0-9a-f]+)/i);
    if (!match) continue;
    if (seen.has(match[1])) {
      duplicates += 1;
      continue;
    }
    seen.add(match[1]);
    items.push({
      externalId: match[1],
      href: href,
      listName: (link.innerText || "").replace(/\s+/g, " ").trim()
    });
  }
  const endNode = feed.querySelector("[data-feed-end='1']");
  const confirmed = feed.getAttribute("data-feed-complete") === "1" || endNode !== null;
  return {
    items: items,
    duplicates: duplicates,
    done: confirmed,
    end: confirmed ? "confirmed" : "open"
  };
})();
"""
SCROLL_JS = r"""
const feed = document.querySelector('div[role="feed"]');
if (!feed) return false;
feed.scrollTop = feed.scrollHeight;
feed.dispatchEvent(new Event("scroll"));
return true;
"""
READ_CARD_JS = r"""
return (function () {
  /* zhol-card-extract google */
  const root = document.querySelector("div.place-card") || document;
  const textOf = function (selector) {
    const node = root.querySelector(selector);
    const raw = node ? (node.innerText || "") : "";
    return raw.replace(/^[^\p{L}\p{N}+]+/u, "").replace(/\s+/g, " ").trim();
  };
  const hrefs = [];
  for (const link of root.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href") || "";
    if (href) hrefs.push(href);
  }
  const website = root.querySelector("a[data-item-id='authority'][href]");
  const access = root.querySelector("[data-access]");
  const reviews = root.querySelector("div.F7nice span.review-count");
  let reviewText = reviews ? (reviews.innerText || "").replace(/\s+/g, " ").trim() : "";
  if (!reviewText) {
    const row = root.querySelector("div.F7nice");
    const found = row ? (row.innerText || "").match(/\(([\d\s]+)\)/) : null;
    reviewText = found ? found[1] : "";
  }
  return {
    name: textOf("h1.DUwDvf"),
    address: textOf("button[data-item-id='address']"),
    category: textOf("button.DkEaL"),
    rating: textOf("div.F7nice span[aria-hidden='true']"),
    reviews: reviewText,
    phones: textOf("button[data-item-id^='phone']") ? [textOf("button[data-item-id^='phone']")] : [],
    website: website ? (website.getAttribute("href") || "") : "",
    text: root.innerText || "",
    hrefs: hrefs,
    access: access ? (access.getAttribute("data-access") || "") : "",
    url: location.href
  };
})();
"""


def search_page_url(origin: str, query: str, page: int, *, city: str = "") -> str:
    """Google Maps search is one scrolling feed. page is ignored. City is already in the query."""
    del page, city
    accept_url(origin)
    url = origin.rstrip("/") + "/maps/search/" + quote(query, safe="")
    accept_url(url)
    return url


def card_url(origin: str, external_id: str, href: str = "") -> str:
    if href.startswith(("http://", "https://")):
        accept_url(href)
        return href
    if href.startswith("/"):
        url = origin.rstrip("/") + href
        accept_url(url)
        return url
    accept_url(origin)
    url = origin.rstrip("/") + f"/maps/place/data=!1s{external_id}"
    accept_url(url)
    return url
