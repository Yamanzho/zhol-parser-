"""Cabinet form used by a prepare step.

Saved HTML is the test stand-in. A live page uses the same control names and never clicks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from zhol.browser.cabinet.session import BrowserCrash, CabinetStopped
from zhol.fulfillment.types import SubmitReaction

PUBLISH_BUTTONS = frozenset({"Опубликовать", "Publish"})
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
SECRET_WORDS = ("парол", "password", "код", "code", "смс", "sms")


def is_secret(name: str, input_type: str = "") -> bool:
    folded = name.casefold()
    if input_type.casefold() == "password":
        return True
    return any(word in folded for word in SECRET_WORDS)


@dataclass
class Control:
    name: str
    field: str | None
    value: str
    input_type: str
    secret: bool


class FixtureCabinet:
    """Saved cabinet page. Writes stay in memory and buttons are not clicked."""

    def __init__(
        self,
        html: str,
        markers: Mapping[str, str],
        *,
        submit_names: Callable[[], tuple[str, ...]],
    ) -> None:
        parsed = _Parser(markers)
        parsed.feed(html)
        parsed.close()
        self.page = parsed.page
        self.platform = parsed.platform
        self.external_id = parsed.external_id
        self.fields = parsed.fields
        self.controls = parsed.controls
        self.body = parsed.body
        self.writes: list[tuple[str, str]] = []
        self._clicks: list[str] = []
        self.submit_outcome = parsed.submit_outcome
        self.confirmation = parsed.confirmation
        self.request_sent = parsed.request_sent
        self.verification = parsed.verification
        self._markers = dict(markers)
        self._submit_names = submit_names

    def body_text(self) -> str:
        return self.body

    def marker(self, attr: str) -> str | None:
        if attr == self._markers.get("page_attr"):
            return self.page
        if attr == self._markers.get("platform_attr"):
            return self.platform
        if attr == self._markers.get("external_id_attr"):
            return self.external_id
        return None

    def field_text(self, field: str) -> str | None:
        return self.fields.get(field)

    def control_value(self, role: str, name: str) -> str | None:
        if role != "textbox" or is_secret(name):
            raise PermissionError("authentication fields are not read")
        control = self._control(name)
        if control is None:
            return None
        if control.secret:
            raise PermissionError("authentication fields are not read")
        return control.value

    def value(self, name: str) -> str | None:
        return self.control_value("textbox", name)

    def write_control(self, role: str, name: str, value: str) -> None:
        if role != "textbox" or name in self._submit_names() or is_secret(name):
            raise PermissionError("submit controls and authentication fields are not used")
        control = self._control(name)
        if control is None or control.secret:
            raise PermissionError("submit controls and authentication fields are not used")
        control.value = value
        self.writes.append((name, value))

    def clicked(self) -> tuple[str, ...]:
        return tuple(self._clicks)

    def simulate_submit(self, button: str) -> SubmitReaction:
        """Record one offline click and return the fake cabinet response."""
        if button in PUBLISH_BUTTONS:
            raise PermissionError("publish is not a submit action")
        if button not in self._submit_names():
            raise PermissionError("submit control is not configured")
        if button not in self.body:
            raise CabinetStopped("stopped_unknown_ui")
        self._clicks.append(button)
        outcome = self.submit_outcome or "submitted"
        if outcome == "browser_crash":
            raise BrowserCrash("the browser closed during submit")
        claimed = outcome == "published"
        mapped = "submitted" if claimed else outcome
        return SubmitReaction(
            button=button,
            clicked=True,
            outcome=mapped,
            confirmation=self.confirmation or None,
            request_sent=self.request_sent or "unknown",
            claimed_published=claimed,
        )

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)

    def redacted_html(self) -> str:
        lines = [
            "<!DOCTYPE html>",
            "<html>",
            (
                f'<body data-page="{escape(self.page or "")}" '
                f'data-platform="{escape(self.platform or "")}" '
                f'data-external-id="{escape(self.external_id or "")}">'
            ),
        ]
        for field, text in self.fields.items():
            lines.append(f'<p data-field="{escape(field)}">{escape(text)}</p>')
        for control in self.controls:
            shown = "" if control.secret else control.value
            lines.append(
                f'<input aria-label="{escape(control.name)}" value="{escape(shown)}">'
            )
        names = self._submit_names() if hasattr(self, "_submit_names") else ()
        body = getattr(self, "body", "")
        for name in names:
            if name and name in body:
                lines.append(f"<button>{escape(name)}</button>")
        lines.append("</body></html>")
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        return None

    def _control(self, name: str) -> Control | None:
        for control in self.controls:
            if control.name == name:
                return control
        return None


class PlaywrightCabinet:
    """Live cabinet. Fills a textbox by its configured role and name. Does not click."""

    def __init__(
        self,
        page: object,
        markers: Mapping[str, str],
        *,
        submit_names: Callable[[], tuple[str, ...]],
        close=None,
    ) -> None:
        self.page = page
        self.field_attr = markers["field_attr"]
        self.page_attr = markers["page_attr"]
        self.platform_attr = markers["platform_attr"]
        self.external_attr = markers["external_id_attr"]
        self._submit_names = submit_names
        self._close = close or (lambda: None)

    def body_text(self) -> str:
        return self.page.inner_text("body")  # type: ignore[attr-defined]

    def marker(self, attr: str) -> str | None:
        return self.page.locator("body").first.get_attribute(attr)  # type: ignore[attr-defined]

    def field_text(self, field: str) -> str | None:
        if not field.replace("_", "").isalnum():
            raise ValueError(field)
        locator = self.page.locator(f"[{self.field_attr}='{field}']")  # type: ignore[attr-defined]
        if locator.count() == 0:
            return None
        return locator.first.inner_text().strip()

    def control_value(self, role: str, name: str) -> str | None:
        self._reject(role, name)
        locator = self.page.get_by_role(role, name=name, exact=True)  # type: ignore[attr-defined]
        if locator.count() == 0:
            return None
        input_type = locator.get_attribute("type") or ""
        self._reject(role, name, input_type)
        return locator.input_value()

    def write_control(self, role: str, name: str, value: str) -> None:
        self._reject(role, name)
        locator = self.page.get_by_role(role, name=name, exact=True)  # type: ignore[attr-defined]
        if locator.count() == 0:
            raise PermissionError("the cabinet control is missing")
        input_type = locator.get_attribute("type") or ""
        self._reject(role, name, input_type)
        locator.fill(value)

    def clicked(self) -> tuple[str, ...]:
        return ()

    def simulate_submit(self, button: str) -> SubmitReaction:
        raise PermissionError("live listing submission is closed")

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)  # type: ignore[attr-defined]

    def redacted_html(self) -> str:
        # page.content() can contain a password or a one-time code, so it is not stored.
        bits = [
            "<!DOCTYPE html>",
            "<html><body>",
            f"<!-- page={escape(self.marker(self.page_attr) or '')} -->",
            f"<!-- platform={escape(self.marker(self.platform_attr) or '')} -->",
            f"<!-- external={escape(self.marker(self.external_attr) or '')} -->",
            "</body></html>",
        ]
        return "\n".join(bits) + "\n"

    def close(self) -> None:
        self._close()

    def _reject(self, role: str, name: str, input_type: str = "") -> None:
        if role != "textbox" or name in self._submit_names() or is_secret(name, input_type):
            raise PermissionError("submit controls and authentication fields are not used")


class _Parser(HTMLParser):
    def __init__(self, markers: Mapping[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.markers = markers
        self.page: str | None = None
        self.platform: str | None = None
        self.external_id: str | None = None
        self.submit_outcome: str | None = None
        self.confirmation: str | None = None
        self.request_sent: str | None = None
        self.verification: str | None = None
        self.fields: dict[str, str] = {}
        self.controls: list[Control] = []
        self.body_parts: list[str] = []
        self._field: str | None = None
        self._field_depth = 0
        self._buf: list[str] = []
        self._textarea: Control | None = None
        self._textarea_depth = 0

    @property
    def body(self) -> str:
        return "".join(self.body_parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "body":
            self.page = values.get(self.markers["page_attr"]) or None
            self.platform = values.get(self.markers["platform_attr"]) or None
            self.external_id = values.get(self.markers["external_id_attr"]) or None
            self.submit_outcome = values.get("data-submit-outcome") or None
            self.confirmation = values.get("data-confirmation") or None
            self.request_sent = values.get("data-request-sent") or None
            self.verification = values.get("data-verification") or None
        field = values.get(self.markers["field_attr"]) or None
        if tag in {"input", "textarea"}:
            name = values.get("aria-label") or ""
            input_type = values.get("type") or tag
            control = Control(
                name=name,
                field=field,
                value=values.get("value") or "",
                input_type=input_type,
                secret=is_secret(name, input_type),
            )
            self.controls.append(control)
            if tag == "textarea":
                self._textarea = control
                self._textarea_depth = 1
                self._buf = []
            return
        if field and self._field is None:
            self._field = field
            self._field_depth = 1
            self._buf = []
        elif self._field is not None:
            self._field_depth += 1
        if self._textarea is not None:
            self._textarea_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._textarea is not None:
            self._textarea_depth -= 1
            if self._textarea_depth == 0:
                text = "".join(self._buf).strip()
                if text:
                    self._textarea.value = text
                self._textarea = None
                self._buf = []
        if self._field is not None:
            self._field_depth -= 1
            if self._field_depth == 0:
                self.fields[self._field] = "".join(self._buf).strip()
                self._field = None
                self._buf = []

    def handle_data(self, data: str) -> None:
        self.body_parts.append(data)
        if self._textarea is not None or self._field is not None:
            self._buf.append(data)
