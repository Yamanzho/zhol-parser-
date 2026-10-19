from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser


class PageNotLoaded(Exception):
    def __init__(
        self,
        message: str = "",
        screenshot: str | None = None,
        url: str | None = None,
        access: str | None = None,
        outcome: str | None = None,
    ) -> None:
        self.screenshot = screenshot
        self.url = url
        self.access = access
        self.outcome = outcome
        super().__init__(message)


class UnexpectedPage(Exception):
    def __init__(
        self,
        message: str = "",
        screenshot: str | None = None,
        url: str | None = None,
        access: str | None = None,
        outcome: str | None = None,
    ) -> None:
        self.screenshot = screenshot
        self.url = url
        self.access = access
        self.outcome = outcome
        super().__init__(message)


class AccessRestricted(Exception):
    """The public page asked for a check or blocked the browser. Do not retry past the queue limit."""

    def __init__(
        self,
        message: str = "",
        screenshot: str | None = None,
        url: str | None = None,
        access: str | None = None,
        outcome: str | None = None,
    ) -> None:
        self.screenshot = screenshot
        self.url = url
        self.access = access
        self.outcome = outcome
        super().__init__(message)


@dataclass
class PageDocument:
    page: str | None
    platform: str | None
    external_id: str | None
    fields: dict[str, str]
    hrefs: dict[str, str]
    candidates: list[dict] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)


class _Parser(HTMLParser):
    def __init__(self, markers: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.markers = markers
        self.page: str | None = None
        self.platform: str | None = None
        self.external_id: str | None = None
        self.fields: dict[str, str] = {}
        self.hrefs: dict[str, str] = {}
        self.candidates: list[dict] = []
        self._cand: dict | None = None
        self._cand_depth = 0
        self._field: str | None = None
        self._field_depth = 0
        self._buf: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if self.page is None and self.markers["page_attr"] in values:
            self.page = values[self.markers["page_attr"]] or None
        if self.platform is None and self.markers["platform_attr"] in values:
            self.platform = values[self.markers["platform_attr"]] or None
        if self._cand is None and self.markers["candidate_attr"] in values:
            self._cand = {
                "candidate_id": values[self.markers["candidate_attr"]],
                "external_id": values.get(self.markers["external_id_attr"]) or None,
                "url": values.get("href") or None,
                "fields": {},
                "hrefs": {},
            }
            self.candidates.append(self._cand)
            self._cand_depth = 1
        elif self._cand is not None:
            self._cand_depth += 1
        if self._cand is None and self.external_id is None and self.markers["external_id_attr"] in values:
            self.external_id = values[self.markers["external_id_attr"]] or None
        if self._field is None and self.markers["field_attr"] in values:
            self._field = values[self.markers["field_attr"]]
            self._field_depth = 1
            self._buf = []
            self._href = values.get("href") or None
        elif self._field is not None:
            self._field_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._field is not None:
            self._field_depth -= 1
            if self._field_depth == 0:
                text = "".join(self._buf).strip()
                target = self._cand["fields"] if self._cand is not None else self.fields
                hrefs = self._cand["hrefs"] if self._cand is not None else self.hrefs
                target[self._field] = text
                if self._href:
                    hrefs[self._field] = self._href
                self._field = None
                self._buf = []
                self._href = None
        if self._cand is not None:
            self._cand_depth -= 1
            if self._cand_depth == 0:
                self._cand = None

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._buf.append(data)


def parse_html(html: str, markers: dict[str, str]) -> PageDocument:
    parser = _Parser(markers)
    parser.feed(html)
    parser.close()
    return PageDocument(
        page=parser.page,
        platform=parser.platform,
        external_id=parser.external_id,
        fields=parser.fields,
        hrefs=parser.hrefs,
        candidates=parser.candidates,
    )
