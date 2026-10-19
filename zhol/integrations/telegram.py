from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MAX_TEXT = 4096


class TelegramError(RuntimeError):
    pass


def notify(text: str, *, timeout: float = 10.0) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("RESPONSIBLE_TG_CHAT_ID")
    if not token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN or RESPONSIBLE_TG_CHAT_ID is not set")
    body = json.dumps({"chat_id": chat_id, "text": text[:MAX_TEXT]}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    # Errors are re-raised without the original exception: its text can contain the URL with the token.
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise TelegramError(f"telegram HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"telegram HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise TelegramError(f"telegram request failed: {type(exc).__name__}") from None
