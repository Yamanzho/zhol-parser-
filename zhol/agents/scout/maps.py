"""Public Yandex and Google read for the two reference companies already collected on 2GIS."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

from zhol.agents.scout.collect import Identity, collect_matched
from zhol.agents.scout.extract import extract_card
from zhol.agents.scout.fields import field_keys
from zhol.agents.scout.match import Candidate, candidates_from_document, choose
from zhol.browser.config import load_platforms
from zhol.browser.fixtures import LoadedPage
from zhol.browser.page import AccessRestricted, PageDocument, PageNotLoaded, UnexpectedPage
from zhol.browser.twogis.capture import BrowserUnavailable
from zhol.browser.twogis.visible import _STREET
from zhol.timeutil import now

MAP_SIZE = 2
PLATFORMS = ("yandex", "google")
OUTPUT = Path("data/scout-pilot/e3")
REPORT = Path("docs/pilot/e3-maps.md")
MAILING_PATH = Path("data/export/mailing.xlsx")
_FLOOR = re.compile(r"\s+\d+\s+этаж\s*$", re.IGNORECASE)


def _query(identity: Identity) -> str:
    address = identity.address or ""
    street = _STREET.search(address)
    if street:
        address = address[street.start() :]
    address = _FLOOR.sub("", address).strip()
    return " ".join(part for part in (identity.name, address) if part)


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _notes(folder: Path) -> dict[str, str]:
    path = folder / "notes.txt"
    if not path.is_file():
        return {}
    notes = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            notes[key] = value
    return notes


def _identities_from_db(ids: list[str]) -> dict[str, tuple[Identity, str]]:
    from sqlalchemy import select

    from zhol.db.base import get_session_factory
    from zhol.db.models import CardSnapshot, Company
    from zhol.export.mailing import READY, _blocked

    factory = get_session_factory()
    found: dict[str, tuple[Identity, str]] = {}
    with factory() as session:
        for id_2gis in ids:
            company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
            if company is None:
                continue
            rows = session.scalars(
                select(CardSnapshot).where(CardSnapshot.company_id == company.id, CardSnapshot.platform == "2gis")
            ).all()
            ready = [row for row in rows if row.status in READY and not _blocked(row)]
            if not ready:
                continue
            latest = max(ready, key=lambda row: (row.collected_at, row.id))
            fields = latest.fields or {}
            phones = list(fields.get("phones") or [])
            address = fields.get("address")
            if isinstance(address, str):
                address = _FLOOR.sub("", address).strip() or address
            found[id_2gis] = (Identity(fields.get("name") or company.name, address, phones), company.name)
    return found


def _known_urls(ids: list[str]) -> dict[tuple[str, str], str]:
    from sqlalchemy import select

    from zhol.db.base import get_session_factory
    from zhol.db.models import Company, CompanyPlatformId

    factory = get_session_factory()
    found: dict[tuple[str, str], str] = {}
    with factory() as session:
        rows = session.execute(
            select(Company.id_2gis, CompanyPlatformId.platform, CompanyPlatformId.public_url).join(
                CompanyPlatformId, CompanyPlatformId.company_id == Company.id
            ).where(Company.id_2gis.in_(ids))
        ).all()
    for id_2gis, platform, url in rows:
        if platform in PLATFORMS and url:
            found[(id_2gis, platform)] = url
    return found


def _decision_from_card(identity: Identity, page: LoadedPage):
    document = page.document
    if document is None:
        return None
    fields = document.fields or {}
    phones = [item.strip() for item in str(fields.get("phones") or "").split(",") if item.strip()]
    candidate = Candidate(
        str(document.external_id or "1"),
        document.external_id,
        page.url,
        fields.get("name"),
        fields.get("address"),
        phones,
    )
    return choose(identity, [candidate])


def _export_mailing() -> str:
    from zhol.db.base import get_session_factory
    from zhol.export.mailing import load_rows, write_xlsx

    try:
        factory = get_session_factory()
    except RuntimeError as exc:
        return str(exc)
    with factory() as session:
        rows = load_rows(session)
    count = write_xlsx(rows, MAILING_PATH)
    return f"{MAILING_PATH} строк {count}"


class _Ready:
    def __init__(self, search: LoadedPage, card: LoadedPage | None) -> None:
        self.search_page = search
        self.card_page = card
        self.screenshot = None

    def search(self, platform: str, id_2gis: str, url: str) -> LoadedPage:
        return self.search_page

    def card(self, platform: str, external_id: str, url: str) -> LoadedPage:
        if self.card_page is None:
            raise PageNotLoaded(f"{platform} card was not opened")
        return self.card_page


def _store(
    id_2gis: str,
    platform: str,
    identity: Identity,
    search_page: LoadedPage,
    card_page: LoadedPage | None,
    *,
    collection_key: str,
) -> str:
    try:
        from zhol.db.base import get_session_factory
        from zhol.db.models import Company

        factory = get_session_factory()
    except RuntimeError as exc:
        return str(exc)
    from sqlalchemy import select

    from zhol.agents.scout.persist import store_listed_services
    from zhol.browser.config import load_platforms

    try:
        with factory() as session, session.begin():
            company = session.scalar(select(Company).where(Company.id_2gis == id_2gis))
            if company is None:
                return "компании нет в базе, снимок не записан"
            outcome = collect_matched(
                session,
                company=company,
                identity=identity,
                purpose="prospect",
                collection_key=collection_key,
                pages=_Ready(search_page, card_page),  # type: ignore[arg-type]
                cfg=load_platforms(),
                platforms=(platform,),
                persist_misses=False,
            )[0]
            if outcome.snapshot is None:
                return f"снимок не записан: {outcome.verdict}" + (f", {outcome.hold}" if outcome.hold else "")
            services = "услуги не показаны"
            if card_page is not None and card_page.document is not None:
                raw = (card_page.document.notes or {}).get("service_names") or ""
                names = [item.strip() for item in raw.split(";") if item.strip()]
                forbidden = [company.name]
                shown = card_page.document.fields.get("name")
                if shown:
                    forbidden.append(shown)
                services = store_listed_services(
                    session,
                    company.id,
                    names,
                    source_ref=f"field:{platform}.services",
                    forbidden=forbidden,
                )
            return f"снимок {outcome.snapshot.id}, статус {outcome.snapshot.status}, совпадение {outcome.verdict}; {services}"
    except Exception as exc:
        return f"снимок не записан: {type(exc).__name__}: {exc}"


def run_maps(
    ids: list[str],
    *,
    output: Path = OUTPUT,
    report_path: Path = REPORT,
    identities: dict[str, Identity] | None = None,
    known_urls: dict[tuple[str, str], str] | None = None,
    opener: Any = None,
    sleeper: Callable[[float], None] = time.sleep,
    exporter: Callable[[], str] | None = None,
    storer: Callable[..., str] | None = None,
) -> int:
    """Yandex then Google for exactly two companies. A robot check stops the next page."""
    if len(ids) != MAP_SIZE:
        print(f"maps read accepts exactly {MAP_SIZE} company ids")
        return 2
    cfg = load_platforms()
    named = identities
    labels: dict[str, str] = {}
    if named is None:
        try:
            loaded = _identities_from_db(ids)
        except RuntimeError as exc:
            print(exc)
            return 2
        named = {key: value[0] for key, value in loaded.items()}
        labels = {key: value[1] for key, value in loaded.items()}
        known_urls = _known_urls(ids)
    known_urls = known_urls or {}
    lines = [
        "# Пилот Яндекс и Google: Badi и Velara",
        "",
        f"Дата: {now().isoformat()}",
        "",
        "Открыты публичные страницы Яндекса и Google в одном обычном видимом браузере.",
        "Проверка на робота программой не решалась. Объявления компаний не менялись.",
        "",
    ]
    browser = None
    if opener is None:
        from zhol.browser.maps.capture import MapsBrowser

        browser = MapsBrowser(cfg, headed=True)
        opener = browser.__enter__()
    exit_code = 0
    blocked = False
    collected = False
    last = False
    try:
        for platform in PLATFORMS:
            lines.extend(["", f"# {platform}", ""])
            for id_2gis in ids:
                identity = named.get(id_2gis)
                title = labels.get(id_2gis) or (identity.name if identity else id_2gis)
                lines.extend(["", f"## {platform} {id_2gis}", "", f"Название: {title}", ""])
                if identity is None:
                    lines.extend(["В базе нет снимка 2ГИС, поиск не запускался.", ""])
                    exit_code = 2
                    continue
                if blocked:
                    lines.extend(["Не открывалась: предыдущая страница ограничила доступ.", ""])
                    exit_code = 2
                    continue
                query = _query(identity)
                url = cfg["platforms"][platform]["search_url"].format(query=quote_plus(query))
                folder = output / id_2gis / platform
                if last:
                    sleeper(float(cfg["rate_limit_seconds"]))
                last = True
                try:
                    search_page = opener.search(platform, id_2gis, url, folder / "search")
                except (AccessRestricted, BrowserUnavailable) as exc:
                    blocked = True
                    exit_code = 2
                    lines.extend(
                        [
                            f"Доступ: {getattr(exc, 'access', None) or type(exc).__name__}",
                            f"Итог: {getattr(exc, 'outcome', None) or 'stopped'}",
                            f"Причина: {exc}",
                            f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                            "Поля компании не читались.",
                            "",
                        ]
                    )
                    continue
                except (PageNotLoaded, UnexpectedPage) as exc:
                    exit_code = 1
                    lines.extend(
                        [
                            f"Доступ: {getattr(exc, 'access', None) or type(exc).__name__}",
                            f"Итог: stopped",
                            f"Причина: {exc}",
                            f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                            "Поля компании не читались.",
                            "",
                        ]
                    )
                    continue
                document = search_page.document
                decision = choose(identity, candidates_from_document(document)) if document is not None else None
                preopened = None
                stored_card_seen = False
                known = known_urls.get((id_2gis, platform))
                if known and (decision is None or decision.verdict not in ("same", "ambiguous")):
                    try:
                        preopened = opener.card(platform, "", known, folder / "card")
                    except (AccessRestricted, BrowserUnavailable) as exc:
                        blocked = True
                        exit_code = 2
                        lines.extend(
                            [
                                f"Сохранённый адрес {platform} не открыт: {getattr(exc, 'access', None) or type(exc).__name__}",
                                f"Итог: {getattr(exc, 'outcome', None) or 'stopped'}",
                                f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                                "Поля компании не читались.",
                                "",
                            ]
                        )
                        continue
                    except (PageNotLoaded, UnexpectedPage):
                        preopened = None
                    card_decision = _decision_from_card(identity, preopened) if preopened is not None else None
                    if card_decision is not None and card_decision.verdict == "same" and card_decision.candidate is not None:
                        candidate = card_decision.candidate
                        search_page = LoadedPage(
                            url=search_page.url,
                            html=search_page.html,
                            raw_path=search_page.raw_path,
                            file_name=search_page.file_name,
                            screenshot_path=search_page.screenshot_path,
                            kind=search_page.kind,
                            document=PageDocument(
                                page="card",
                                platform=platform,
                                external_id=candidate.external_id,
                                fields={},
                                hrefs={},
                                candidates=[
                                    {
                                        "candidate_id": candidate.candidate_id,
                                        "external_id": candidate.external_id,
                                        "url": candidate.url,
                                        "fields": {
                                            "name": candidate.name or "",
                                            "address": candidate.address or "",
                                            "phones": ", ".join(candidate.phones),
                                        },
                                    }
                                ],
                            ),
                        )
                        decision = card_decision
                    else:
                        stored_card_seen = True
                        shown = card_decision.verdict if card_decision is not None else "not_match"
                        lines.extend(
                            [
                                f"Сохранённый адрес открыт: {known}",
                                f"Совпадение по сохранённой карточке: {shown}",
                                "Карточка не записана.",
                                "",
                            ]
                        )
                        preopened = None
                if decision is None or decision.verdict != "same" or decision.candidate is None:
                    verdict = "not_match" if decision is None else decision.verdict
                    hold = "ambiguous_match" if verdict == "ambiguous" else ""
                    if verdict == "ambiguous":
                        (storer or _store)(
                            id_2gis, platform, identity, search_page, None, collection_key=f"e3-maps:{id_2gis}"
                        )
                    lines.extend(
                        [
                            f"Совпадение: {verdict}",
                            f"Эскалация: {hold or 'нет'}",
                            f"Скриншот поиска: {search_page.screenshot_path}",
                            "Сохранённая карточка не совпала." if stored_card_seen else "Карточка не открывалась.",
                            "",
                        ]
                    )
                    if verdict == "ambiguous":
                        exit_code = exit_code or 0
                    continue
                card_folder = folder / "card"
                try:
                    card_page = preopened or opener.card(
                        platform, decision.candidate.external_id or "", decision.candidate.url or url, card_folder
                    )
                except (AccessRestricted, BrowserUnavailable) as exc:
                    blocked = True
                    exit_code = 2
                    lines.extend(
                        [
                            f"Совпадение: {decision.verdict}",
                            f"Доступ к карточке: {getattr(exc, 'access', None) or type(exc).__name__}",
                            f"Итог: {getattr(exc, 'outcome', None) or 'stopped'}",
                            f"Скриншот: {getattr(exc, 'screenshot', None) or ''}",
                            "Поля компании не читались.",
                            "",
                        ]
                    )
                    continue
                except (PageNotLoaded, UnexpectedPage) as exc:
                    exit_code = 1
                    lines.extend([f"Карточка не прочитана: {exc}", f"Скриншот: {getattr(exc, 'screenshot', None) or ''}", ""])
                    continue
                assert card_page.document is not None
                seen = now().isoformat()
                spec = cfg["platforms"][platform]
                fields, sources = extract_card(
                    platform,
                    card_page.document,
                    url=card_page.url,
                    seen_at=seen,
                    screenshot=card_page.screenshot_path,
                    raw_path=card_page.raw_path,
                    code_fields=list(spec["code_fields"]),
                    model_fields=list(spec["model_fields"]),
                )
                stored = (storer or _store)(
                    id_2gis,
                    platform,
                    identity,
                    search_page,
                    card_page,
                    collection_key=f"e3-maps:{id_2gis}",
                )
                collected = True
                notes = _notes(card_folder)
                missing = [
                    f"{key}: {notes.get(key) or (sources.get(key) or {}).get('reason') or 'not_shown'}"
                    for key in field_keys(platform)
                    if fields.get(key) is None
                ]
                phone = "да" if decision.phone_match else "нет, нужна проверка человека"
                lines.extend(
                    [
                        "Доступ: card_visible",
                        "Итог: collected",
                        f"Совпадение: {decision.verdict}",
                        f"Общий телефон: {phone}",
                        f"Идентификатор: {card_page.document.external_id or decision.candidate.external_id or 'не показан'}",
                        f"Фактический адрес: {card_page.url}",
                        f"Время просмотра: {seen}",
                        f"Скриншот: {card_page.screenshot_path}",
                        f"База: {stored}",
                        "Пустые поля:",
                    ]
                )
                lines.extend(f"- {item}" for item in missing)
                lines.append("Прочитанные поля:")
                for key in field_keys(platform):
                    if fields.get(key) is not None:
                        lines.append(f"- {key}: {fields[key]}")
                lines.append("")
    finally:
        if browser is not None:
            browser.__exit__(None, None, None)
    exported = "файл не перезаписан: карточка не собрана"
    if collected:
        exported = (exporter or _export_mailing)()
    lines.extend(["", f"Excel: {exported}", ""])
    _write(report_path, lines)
    print(f"Отчёт: {report_path}")
    return exit_code
