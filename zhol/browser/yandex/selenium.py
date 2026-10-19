"""Yandex Maps card and search selectors. They are not the 2GIS selectors.

A loopback fixture may use these selectors without a grant.
A public host is accepted only when ZHOL_YANDEX_BROWSER_GRANT names that source's scope.
"""

from __future__ import annotations

from urllib.parse import quote

from zhol.agents.scout.browser_yandex import accept_url

READY_SEARCH_JS = "return !!document.querySelector('.search-list-view, li.search-snippet-view')"
READY_CARD_JS = (
    "return !!document.querySelector("
    "'article.orgpage-view, aside.sidebar-view._name_orgpage, "
    "a.card-title-view__title-link, h1.orgpage-header-view__header')"
)
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
  const list = document.querySelector(".search-list-view");
  const root = list || document;
  if (!list && !document.querySelector("li.search-snippet-view, a.search-snippet-view__link")) {
    return {items: [], pageNumbers: [], duplicates: 0, heading: "", done: false, hasMore: false};
  }
  const items = [];
  const seen = new Set();
  let duplicates = 0;
  const links = root.querySelectorAll(
    "a.search-snippet-view__link[href], li.search-snippet-view a.link-overlay[href]"
  );
  for (const link of links) {
    const href = link.getAttribute("href") || "";
    const match = href.match(/\/org\/[^/?#]+\/(\d+)/);
    if (!match) continue;
    if (seen.has(match[1])) {
      duplicates += 1;
      continue;
    }
    seen.add(match[1]);
    let listName = (link.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
    if (!listName) listName = (link.innerText || "").replace(/\s+/g, " ").trim();
    if (!listName) {
      const item = link.closest("li.search-snippet-view");
      const title = item ? item.querySelector(".search-business-snippet-view__title") : null;
      listName = title ? (title.innerText || "").replace(/\s+/g, " ").trim() : "";
    }
    items.push({
      externalId: match[1],
      href: href,
      listName: listName
    });
  }
  const pageNumbers = [];
  for (const link of document.querySelectorAll("a.search-pagination__page[href]")) {
    const found = (link.getAttribute("href") || "").match(/\/page\/(\d+)/);
    if (found) pageNumbers.push(Number(found[1]));
  }
  const heading = document.querySelector("h1");
  const complete = document.querySelector("[data-results-complete='1']");
  const moreControl = document.querySelector("button.scroll__button, .search-list-view__more");
  return {
    items: items,
    pageNumbers: pageNumbers,
    duplicates: duplicates,
    heading: heading ? heading.innerText.replace(/\s+/g, " ").trim() : "",
    done: complete !== null,
    hasMore: pageNumbers.length > 0 || moreControl !== null
  };
})();
"""
READ_CARD_JS = r"""
return (function () {
  /* zhol-card-extract yandex */
  const closestCard = function (node) {
    return node ? node.closest("aside.sidebar-view, article") : null;
  };
  let root = document.querySelector("article.orgpage-view");
  if (!root) root = document.querySelector("aside.sidebar-view._name_orgpage");
  if (!root) root = closestCard(document.querySelector("a.card-title-view__title-link"));
  if (!root) root = closestCard(document.querySelector("h1.orgpage-header-view__header"));
  if (!root) return null;
  const textOf = function (selector) {
    const node = root.querySelector(selector);
    return node ? (node.innerText || "").replace(/\s+/g, " ").trim() : "";
  };
  const firstText = function (selectors) {
    for (let i = 0; i < selectors.length; i += 1) {
      const text = textOf(selectors[i]);
      if (text) return text;
    }
    return "";
  };
  const hrefs = [];
  for (const link of root.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href") || "";
    if (href) hrefs.push(href);
  }
  const phones = [];
  const addPhone = function (value) {
    const label = (value || "").replace(/\s+/g, " ").trim();
    if (label && phones.indexOf(label) < 0) phones.push(label);
  };
  for (const link of root.querySelectorAll("a[href^='tel:']")) addPhone(link.innerText);
  for (const node of root.querySelectorAll("[itemprop='telephone'], .orgpage-phones-view__phone-number")) {
    addPhone(node.innerText);
  }
  const website = root.querySelector(
    "a.orgpage-contacts-view__website[href], a.business-urls-view__link[href]"
  );
  const access = root.querySelector("[data-access]");
  let reviews = textOf(".business-rating-badge-view__reviews");
  if (!reviews) {
    const nodes = root.querySelectorAll(".business-header-rating-view__text");
    for (const node of nodes) {
      const text = (node.innerText || "").replace(/\s+/g, " ").trim();
      if (/\d/.test(text)) {
        reviews = text;
        break;
      }
    }
  }
  return {
    name: firstText([
      "h1.orgpage-header-view__header",
      "a.card-title-view__title-link",
      "h1[itemprop='name']"
    ]),
    address: firstText([
      "a.orgpage-header-view__address",
      "a.business-contacts-view__address-link"
    ]),
    category: firstText([
      ".orgpage-categories-info-view",
      "a.business-categories-view__category"
    ]),
    rating: firstText([
      ".business-rating-badge-view__rating-text",
      ".business-rating-badge-view__rating"
    ]),
    reviews: reviews,
    phones: phones,
    website: website ? (website.getAttribute("href") || "") : "",
    text: root.innerText || "",
    hrefs: hrefs,
    access: access ? (access.getAttribute("data-access") || "") : "",
    url: location.href
  };
})();
"""


def search_page_url(origin: str, query: str, page: int, *, city: str = "Алматы") -> str:
    from zhol.agents.scout.localities import yandex_place

    accept_url(origin)
    encoded = quote(query, safe="")
    _name, place = yandex_place(city)
    path = f"/maps/{place.region_id}/{place.slug}/search/{encoded}"
    if page > 1:
        path += f"/page/{page}"
    url = origin.rstrip("/") + path
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
    url = origin.rstrip("/") + f"/maps/org/salon/{external_id}/"
    accept_url(url)
    return url
