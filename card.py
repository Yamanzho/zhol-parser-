import base64
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from selenium.webdriver.support.ui import WebDriverWait

from browser_ops import BrowserTimeout
from browser_ops import WebDriverException
from browser_ops import eval_js
from browser_ops import is_browser_crash
from browser_ops import open_url
from browser_ops import wait_js

log = logging.getLogger("gis.card")

START_URL = "https://2gis.kz/almaty"
CARD_TIMEOUT_MS = 20_000
PHONE_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 30_000
CARD_RELOADS = 2
HEADING_GRACE_MS = 5_000
CONFIRM_MESSAGE = "2GIS требует подтверждение в браузере."
DATA_DIR = Path("data")

POSTAL_CODE = re.compile(r"\d{5,6}(?:/[A-Za-z0-9]+)?")
TRUNCATED_PHONE = re.compile(r"\.{2,}|…")
FIRM_ID = re.compile(r"/firm/(\d+)")

WHATSAPP_HOSTS = {"wa.me", "whatsapp.com", "api.whatsapp.com"}
INSTAGRAM_HOSTS = {"instagram.com"}
TRACKER_HOSTS = {"link.2gis.com", "link.2gis.ru"}

# Разметка снята с публичной карточки в браузере:
# h1 > span — название;
# a[href*="/geo/"] — адрес, следующий div — строка с городом в конце;
# button «Показать телефон» раскрывает соседний a[href^="tel:"];
# контакты Instagram/WhatsApp — ссылки link.2gis.com с адресом в пути;
# блок со span «Реклама» не относится к контактам карточки.
CARD_DOM_JS = r"""
function gisNormalize(value) {
  return (value || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}

function gisFirmIds(node) {
  const ids = new Set();
  for (const link of node.querySelectorAll('a[href*="/firm/"]')) {
    const match = (link.getAttribute("href") || "").match(/\/firm\/(\d+)/);
    if (match) ids.add(match[1]);
  }
  return ids;
}

function gisFindCard() {
  const heading = document.querySelector("h1");
  if (!heading) return null;
  const branchMatch = (location.pathname || "").match(/\/firm\/(\d+)/);
  const branchId = branchMatch ? branchMatch[1] : "";
  let node = heading;
  let withAddress = null;
  let identified = null;
  while (node) {
    if (node.querySelector("h1")) {
      const ids = gisFirmIds(node);
      const hasAddress = !!node.querySelector('a[href*="/geo/"]');
      if (hasAddress) {
        if (ids.size <= 1) return node;
        if (!withAddress) withAddress = node;
      }
      if (!identified && ids.size === 1 && (!branchId || ids.has(branchId))) {
        identified = node;
      }
    }
    node = node.parentElement;
  }
  return withAddress || identified;
}

function gisInAd(link, card) {
  let node = link.parentElement;
  while (node && node !== card) {
    const hasBadge = [...node.querySelectorAll("span")].some(
      (span) => gisNormalize(span.textContent) === "Реклама"
    );
    const hasPhone = !!node.querySelector('a[href^="tel:"]');
    if (hasBadge && !hasPhone && gisNormalize(node.innerText).length < 800) {
      return true;
    }
    node = node.parentElement;
  }
  return false;
}

function gisMarkCard() {
  const card = gisFindCard();
  document.querySelectorAll("[data-gis-parser-card]").forEach((node) => {
    node.removeAttribute("data-gis-parser-card");
  });
  if (card) card.setAttribute("data-gis-parser-card", "1");
  return !!card;
}

function gisIsShowPhone(button) {
  const text = gisNormalize(button.textContent);
  return text === "Показать телефон" || text === "Показать телефоны";
}

function gisLocalityLine(geo) {
  let node = geo.parentElement;
  while (node) {
    const sibling = node.nextElementSibling;
    if (sibling && sibling.tagName === "DIV" && !sibling.querySelector("a, button")) {
      const text = gisNormalize(sibling.textContent);
      if (text) return text;
    }
    node = node.parentElement;
  }
  return "";
}

function gisShowPhoneCount() {
  const card = gisFindCard();
  if (!card) return -1;
  return [...card.querySelectorAll("button")].filter(gisIsShowPhone).length;
}

function gisExtract() {
  const card = gisFindCard();
  if (!card) return null;
  const heading = card.querySelector("h1");
  const nameSpan = heading.querySelector("span");
  const geo = card.querySelector('a[href*="/geo/"]');
  let address = null;
  let cityLine = "";
  const addressSeen = !!geo;
  if (geo) {
    const text = gisNormalize(geo.textContent);
    address = text || null;
    cityLine = gisLocalityLine(geo);
  }
  const phones = [...card.querySelectorAll('a[href^="tel:"]')].map((link) => ({
    text: gisNormalize(link.innerText),
  }));
  const links = [...card.querySelectorAll("a[href]")].map((link) => ({
    href: link.href,
    aria: link.getAttribute("aria-label"),
    text: gisNormalize(link.innerText).slice(0, 120),
    inAd: gisInAd(link, card),
  }));
  const rubrics = [];
  for (const link of card.querySelectorAll('a[href*="rubricId/"]')) {
    const label = gisNormalize(link.textContent);
    if (label && rubrics.indexOf(label) < 0) rubrics.push(label);
  }
  return {
    name: gisNormalize(nameSpan ? nameSpan.textContent : heading.textContent),
    category: rubrics.length ? rubrics.join(", ") : null,
    address,
    addressSeen,
    cityLine,
    phones,
    links,
    firmIds: [...gisFirmIds(card)],
    showPhoneCount: gisShowPhoneCount(),
  };
}

window.gisFindCard = gisFindCard;
window.gisMarkCard = gisMarkCard;
window.gisShowPhoneCount = gisShowPhoneCount;
window.gisExtract = gisExtract;
true;
"""

# Снимок страницы, если h1 не появился. Капча — только явные признаки,
# а не само отсутствие заголовка.
PAGE_PROBE_JS = r"""
const body = document.body;
const text = body ? (body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1500) : "";
const frames = [];
for (const frame of document.querySelectorAll("iframe, frame")) {
  frames.push(frame.getAttribute("src") || "");
}
const nodes = document.querySelectorAll(
  "[class*='captcha'], [id*='captcha'], .g-recaptcha, .smart-captcha, #cf-challenge-running, #challenge-form"
);
return {
  url: location.href || "",
  title: document.title || "",
  text,
  frames,
  captchaNodes: nodes.length,
};
"""

_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "smartcaptcha",
    "не робот",
    "я не робот",
    "капча",
    "подтвердите, что",
    "проверка безопасности",
    "verify you are human",
    "challenges.cloudflare",
    "ddos-guard",
)

# A block is not a captcha. The collector records it and does not try to get around it.
_ACCESS_MARKERS = (
    "доступ ограничен",
    "доступ запрещ",
    "access denied",
    "too many requests",
)


class ExtractionError(Exception):
    """Карточку не удалось прочитать. Это не пустой контакт."""


def branch_id_from_url(url: str) -> str | None:
    match = FIRM_ID.search(url or "")
    return match.group(1) if match else None


def _hostname(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _social_kind(url: str) -> str | None:
    host = _hostname(url)
    if host in WHATSAPP_HOSTS:
        return "whatsapp"
    if host in INSTAGRAM_HOSTS:
        return "instagram"
    return None


def _decode_tracker_blob(blob: str) -> str | None:
    normalized = unquote(blob).replace("-", "+").replace("_", "/")
    candidates = [normalized]
    leading = re.match(r"[A-Za-z0-9+/=]+", normalized)
    if leading and leading.group(0) != normalized:
        candidates.append(leading.group(0))
    for candidate in candidates:
        padding = "=" * ((4 - len(candidate) % 4) % 4)
        try:
            raw = base64.b64decode(candidate + padding, validate=True)
            first_line = raw.split(b"\n", 1)[0].decode("utf-8").strip()
        except Exception:
            continue
        target = urlparse(first_line)
        if target.scheme in {"http", "https"} and target.netloc and not any(char.isspace() for char in first_line):
            return first_line
    return None


def embedded_tracker_url(href: str) -> str | None:
    """Первая строка адреса внутри ссылки link.2gis.com, если она однозначна."""
    if _hostname(href) not in TRACKER_HOSTS:
        return None
    parts = [part for part in urlparse(href).path.split("/") if part]
    if not parts:
        return None
    return _decode_tracker_blob(parts[-1])


def resolve_social_link(href: str) -> tuple[str | None, str | None]:
    """Возвращает (kind, url). (None, None), если ссылка не WhatsApp и не Instagram."""
    direct = _social_kind(href)
    if direct:
        return direct, href
    if _hostname(href) not in TRACKER_HOSTS:
        return None, None
    embedded = embedded_tracker_url(href)
    if not embedded:
        return None, None
    kind = _social_kind(embedded)
    if not kind:
        return None, None
    return kind, embedded


def city_from_locality(line: str | None) -> str | None:
    """Город из строки рядом с адресом.

    На карточках встречались два вида строки:
    «Мирас м-н, Бостандыкский район, Алматы» и
    «Алмалинский район, Алматы, 050012/A05B8E6».
    """
    parts = [part.strip() for part in (line or "").split(",") if part.strip()]
    if not parts:
        return None
    if len(parts) >= 2 and POSTAL_CODE.fullmatch(parts[-1]):
        return parts[-2]
    return parts[-1]


def _is_truncated_phone(text: str) -> bool:
    return bool(TRUNCATED_PHONE.search(text or ""))


def _is_complete_phone(text: str) -> bool:
    if not text or _is_truncated_phone(text):
        return False
    digits = re.sub(r"\D", "", text)
    return len(digits) >= 10


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def build_record(raw: dict, source_url: str, branch_id: str) -> dict:
    errors: list[dict] = []
    unresolved: list[dict] = []

    name = raw.get("name") or None
    if not name:
        errors.append({"field": "name", "message": "В карточке не найдено название"})

    card_ids = raw.get("firmIds") or []
    if branch_id not in card_ids:
        errors.append(
            {
                "field": "branch_id",
                "message": "Идентификатор в ссылке не совпал с карточкой на странице",
            }
        )

    phones: list[str] = []
    saw_truncated = False
    for phone in raw.get("phones") or []:
        text = phone.get("text") or ""
        if _is_truncated_phone(text):
            saw_truncated = True
            continue
        if _is_complete_phone(text):
            phones.append(text)
    if saw_truncated or (raw.get("showPhoneCount") or 0) > 0:
        errors.append(
            {
                "field": "phones",
                "message": "Телефон остался скрытым или сокращённым после раскрытия",
            }
        )

    whatsapp: list[str] = []
    instagram: list[str] = []
    for link in raw.get("links") or []:
        if link.get("inAd"):
            continue
        href = link.get("href") or ""
        aria = link.get("aria") or ""
        kind, resolved = resolve_social_link(href)
        expects_social = aria in {"WhatsApp", "Instagram"}
        if kind and resolved:
            if kind == "whatsapp":
                whatsapp.append(resolved)
            else:
                instagram.append(resolved)
            continue
        if expects_social or (_hostname(href) in TRACKER_HOSTS and aria in {"WhatsApp", "Instagram"}):
            unresolved.append(
                {
                    "href": href,
                    "label": aria or link.get("text") or None,
                    "message": "Ссылку нельзя однозначно отнести к WhatsApp или Instagram",
                }
            )

    city_line = raw.get("cityLine") or ""
    city = city_from_locality(city_line)
    if city_line and not city:
        errors.append(
            {
                "field": "city",
                "message": "Строка рядом с адресом есть, но город из неё не прочитан",
            }
        )

    category = " ".join(str(raw.get("category") or "").split())
    return {
        "name": name,
        "category": category or None,
        "category_seen": "category" in raw,
        "phones": _dedupe(phones),
        "whatsapp": _dedupe(whatsapp),
        "instagram": _dedupe(instagram),
        "city": city,
        "address": raw.get("address") or None,
        "address_seen": bool(raw.get("addressSeen")),
        "two_gis_url": source_url,
        "branch_id": branch_id,
        "diagnostics": {
            "unresolved_links": unresolved,
            "errors": errors,
        },
    }


def _install_dom_helpers(driver) -> None:
    eval_js(driver, CARD_DOM_JS)


def _captcha_marker(snapshot: dict | None) -> str | None:
    """Причина, по которой страница считается капчей. None — обычный таймаут."""
    if not snapshot:
        return None
    if snapshot.get("captchaNodes"):
        return "captcha-node"
    parts = [
        snapshot.get("url") or "",
        snapshot.get("title") or "",
        snapshot.get("text") or "",
        " ".join(snapshot.get("frames") or []),
    ]
    blob = " ".join(parts).casefold()
    for marker in _CAPTCHA_MARKERS:
        if marker in blob:
            return marker
    return None


def _page_snapshot(driver) -> dict | None:
    try:
        snapshot = eval_js(driver, "return (() => {\n" + PAGE_PROBE_JS + "\n})();")
    except WebDriverException as exc:
        if is_browser_crash(exc):
            raise
        log.exception("Не прочитано состояние страницы")
        return None
    return snapshot if isinstance(snapshot, dict) else None


CARD_SIGNALS_JS = r"""
function gisVisible(node) {
  if (!node) return false;
  const style = getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0
    && rect.width > 0 && rect.height > 0;
}
const readyState = document.readyState;
const headingNodes = [...document.querySelectorAll("h1, [role='heading']")];
const h1Nodes = headingNodes.filter((node) => node.tagName === "H1");
const visibleH1 = h1Nodes.some(gisVisible);
const headingText = headingNodes.some((node) => (node.textContent || "").replace(/\s+/g, " ").trim().length > 0);
const address = !!document.querySelector('a[href*="/geo/"]');
const firmLink = !!document.querySelector('a[href*="/firm/' + branchId + '"]');
const phone = !!document.querySelector('a[href^="tel:"]') || [...document.querySelectorAll("button")].some((node) => {
  const text = (node.textContent || "").replace(/\s+/g, " ").trim();
  return text === "Показать телефон" || text === "Показать телефоны";
});
const onFirm = location.pathname.includes("/firm/" + branchId);
const card = typeof gisFindCard === "function" && gisFindCard() !== null;
let signal = "";
if (readyState !== "loading" && onFirm && visibleH1) signal = "h1";
else if (readyState !== "loading" && onFirm && (address || phone)) signal = "firm-content";
else if (readyState !== "loading" && onFirm && headingText) signal = "org-title";
else if ((readyState === "interactive" || readyState === "complete") && card) signal = "card-container";
return {
  signal,
  readyState,
  url: location.href || "",
  title: document.title || "",
  h1: h1Nodes.length > 0,
  visibleH1,
  headingText,
  firmLink,
  card: !!card,
  text: ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").trim().slice(0, 500),
};
"""


def _card_signals(driver, branch_id: str) -> dict:
    try:
        payload = eval_js(
            driver,
            "return (function(branchId) {\n" + CARD_SIGNALS_JS + "\n})(arguments[0]);",
            branch_id,
        )
    except WebDriverException as exc:
        if is_browser_crash(exc):
            raise
        log.exception("Не прочитаны признаки карточки")
        return {}
    return payload if isinstance(payload, dict) else {}


def _await_ready_state(driver) -> bool:
    try:
        WebDriverWait(driver, CARD_TIMEOUT_MS / 1000).until(
            lambda current: current.execute_script("return document.readyState") != "loading"
        )
    except BrowserTimeout:
        return False
    except WebDriverException:
        raise
    return True


def _access_marker(snapshot: dict | None) -> str | None:
    if not snapshot:
        return None
    blob = " ".join(
        [
            str(snapshot.get("url") or ""),
            str(snapshot.get("title") or ""),
            str(snapshot.get("text") or ""),
        ]
    ).casefold()
    for marker in _ACCESS_MARKERS:
        if marker in blob:
            return marker
    return None


def _settled_without_identity(signals: dict, snapshot: dict | None) -> bool:
    """A finished short page with no company name and no firm link will not become a card."""
    if signals.get("readyState") != "complete":
        return False
    if signals.get("signal") or signals.get("h1") or signals.get("visibleH1") or signals.get("headingText"):
        return False
    if signals.get("card") or signals.get("firmLink"):
        return False
    text = str((snapshot or {}).get("text") or signals.get("text") or "")
    return len(text) <= 400


def _await_card(driver, url: str) -> str:
    """ready, captcha, or slow. Missing identity and an access block raise ExtractionError.

    A page that is still loading keeps the full card timeout. A finished page with no
    company name and no firm link fails after the short heading grace, so a malformed
    card is not waited out three full times. The timeout itself is unchanged.
    """
    branch_id = branch_id_from_url(url) or ""
    if not _await_ready_state(driver):
        return "slow"
    deadline = time.monotonic() + CARD_TIMEOUT_MS / 1000
    empty_since: float | None = None
    while time.monotonic() < deadline:
        signals = _card_signals(driver, branch_id)
        snapshot = _page_snapshot(driver)
        if _captcha_marker(snapshot):
            return "captcha"
        if _access_marker(snapshot):
            raise ExtractionError(f"Доступ к карточке ограничен, обход не выполняется: {url}")
        if signals.get("signal"):
            _wait_for_late_heading(driver)
            return "ready"
        if _settled_without_identity(signals, snapshot):
            if empty_since is None:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since >= HEADING_GRACE_MS / 1000:
                raise ExtractionError(f"На странице нет названия или идентификатора компании: {url}")
        else:
            empty_since = None
        time.sleep(0.25)
    signals = _card_signals(driver, branch_id)
    if signals.get("readyState") != "complete" or signals.get("h1") or signals.get("card") or signals.get("firmLink"):
        return "slow"
    raise ExtractionError(f"На странице нет названия или идентификатора компании: {url}")


def _wait_for_late_heading(driver) -> None:
    try:
        WebDriverWait(driver, HEADING_GRACE_MS / 1000).until(
            lambda current: bool(
                eval_js(
                    current,
                    """
                    return [...document.querySelectorAll("h1")].some((node) => {
                      const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
                      return text.length > 0;
                    });
                    """,
                )
            )
        )
    except BrowserTimeout:
        log.info("Заголовок h1 не появился за короткое ожидание")
    except WebDriverException:
        raise


def _reopen_card(driver, url: str) -> None:
    try:
        open_url(driver, url, NAV_TIMEOUT_MS)
    except WebDriverException as exc:
        if is_browser_crash(exc):
            raise
        log.warning("Повторное открытие не удалось %s: %s", url, exc)


def _log_card_failure(driver, url: str) -> None:
    branch_id = branch_id_from_url(url) or ""
    signals = _card_signals(driver, branch_id)
    debug_dir = Path("logs") / "debug"
    screenshot = ""
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        path = debug_dir / f"{branch_id or 'card'}_{stamp}.png"
        driver.save_screenshot(str(path))
        screenshot = str(path)
    except WebDriverException as exc:
        if is_browser_crash(exc):
            raise
        log.exception("Скриншот карточки не сохранён")
    except OSError:
        log.exception("Скриншот карточки не сохранён")
    log.warning(
        "Карточка не готова requested=%s current=%s title=%s ready=%s h1=%s card=%s text=%s screenshot=%s",
        url,
        signals.get("url") or getattr(driver, "current_url", ""),
        signals.get("title") or "",
        signals.get("readyState") or "",
        signals.get("h1"),
        signals.get("card"),
        signals.get("text") or "",
        screenshot,
    )


def _wait_for_card(driver, pause, url: str) -> None:
    """Дождаться карточки. Enter — только если на странице реально капча.

    Медленная страница повторяется в пределах CARD_TIMEOUT_MS. Страница без названия
    и идентификатора, обрыв браузера и ограничение доступа сюда не входят: у них
    свои сообщения, и ограничение доступа не обходится.
    """
    reloads = 0
    while True:
        _install_dom_helpers(driver)
        outcome = _await_card(driver, url)
        if outcome == "ready":
            return
        if outcome == "captcha":
            snapshot = _page_snapshot(driver)
            marker = _captcha_marker(snapshot)
            current = ""
            title = ""
            if isinstance(snapshot, dict):
                current = snapshot.get("url") or ""
                title = snapshot.get("title") or ""
            log.warning(
                "Капча на карточке marker=%s url=%s current=%s title=%s",
                marker,
                url,
                current,
                title,
            )
            print(CONFIRM_MESSAGE)
            pause("После прохождения нажмите Enter, чтобы продолжить.\n")
            _reopen_card(driver, url)
            continue
        _log_card_failure(driver, url)
        log.info("Страница карточки загружается слишком долго url=%s", url)
        if reloads >= CARD_RELOADS:
            log.warning("Карточка пропущена после %s попыток: %s", CARD_RELOADS, url)
            print(f"Карточка пропущена после {CARD_RELOADS} попыток")
            raise ExtractionError(f"Страница карточки загружается слишком долго: {url}")
        reloads += 1
        print(f"Повторная загрузка карточки {reloads}/{CARD_RELOADS}")
        log.info("Повторная загрузка карточки %s/%s url=%s", reloads, CARD_RELOADS, url)
        _reopen_card(driver, url)


def _reveal_phones(driver) -> None:
    for _ in range(5):
        _install_dom_helpers(driver)
        marked = eval_js(driver, "return window.gisMarkCard();")
        if not marked:
            raise ExtractionError("Область карточки пропала до раскрытия телефона")
        count = eval_js(driver, "return window.gisShowPhoneCount();")
        if count == 0:
            return
        if count < 0:
            raise ExtractionError("Область карточки пропала до раскрытия телефона")
        try:
            wait_js(
                driver,
                """
                const card = document.querySelector("[data-gis-parser-card]");
                if (!card) return false;
                return [...card.querySelectorAll("button")].some((node) => {
                  const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
                  return text === "Показать телефон" || text === "Показать телефоны";
                });
                """,
                PHONE_TIMEOUT_MS,
            )
            _click_show_phone(driver)
            wait_js(
                driver,
                "return window.gisShowPhoneCount() >= 0 && window.gisShowPhoneCount() < arguments[0];",
                PHONE_TIMEOUT_MS,
                count,
            )
        except BrowserTimeout as exc:
            raise ExtractionError("Кнопка «Показать телефон» не раскрыла номер") from exc


def _click_show_phone(driver) -> None:
    """Клик по «Показать телефон» внутри уже отмеченной карточки."""
    clicked = eval_js(
        driver,
        """
        const card = document.querySelector("[data-gis-parser-card]");
        if (!card) return false;
        const button = [...card.querySelectorAll("button")].find((node) => {
          const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
          return text === "Показать телефон" || text === "Показать телефоны";
        });
        if (!button) return false;
        button.click();
        return true;
        """,
    )
    if not clicked:
        raise BrowserTimeout("Кнопка «Показать телефон» не найдена")


def _read_card(driver) -> dict | None:
    _install_dom_helpers(driver)
    return eval_js(driver, "return window.gisExtract();")


def read_company(driver, url: str, pause) -> dict:
    branch_id = branch_id_from_url(url)
    if not branch_id:
        raise ExtractionError("В ссылке нет идентификатора филиала (/firm/<id>)")

    open_url(driver, url, NAV_TIMEOUT_MS)
    _wait_for_card(driver, pause, url)
    reveal_error = None
    try:
        _reveal_phones(driver)
    except ExtractionError as exc:
        reveal_error = str(exc)

    raw = _read_card(driver)
    if not raw:
        raise ExtractionError("Не удалось прочитать область текущей карточки")
    record = build_record(raw, url, branch_id)
    if reveal_error:
        record["diagnostics"]["errors"].append({"field": "phones", "message": reveal_error})
    record["branch_id"] = str(record["branch_id"])
    return record


def collect_company(driver, url: str, pause) -> tuple[dict, Path]:
    record = read_company(driver, url, pause)
    path = _write_record(record)
    return record, path


def _write_record(record: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"company_{record['branch_id']}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def format_result(record: dict, path: Path) -> str:
    lines = [
        f"name: {record['name']}",
        f"phones: {json.dumps(record['phones'], ensure_ascii=False)}",
        f"whatsapp: {json.dumps(record['whatsapp'], ensure_ascii=False)}",
        f"instagram: {json.dumps(record['instagram'], ensure_ascii=False)}",
        f"city: {record['city']}",
        f"address: {record['address']}",
        f"two_gis_url: {record['two_gis_url']}",
        f"branch_id: {record['branch_id']}",
        f"Файл: {path}",
    ]
    unresolved = record["diagnostics"]["unresolved_links"]
    if unresolved:
        lines.append(f"Неразрешённые ссылки: {len(unresolved)} (см. diagnostics в файле)")
    for error in record["diagnostics"]["errors"]:
        lines.append(f"Ошибка ({error['field']}): {error['message']}")
    return "\n".join(lines)
