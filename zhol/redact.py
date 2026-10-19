from __future__ import annotations

import re

MAX_ERROR_LEN = 2000

_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"), "[redacted]"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(password|passwd|token|api[_-]?key|secret)(\s*[=:]\s*)\S+"), r"\1\2[redacted]"),
)


SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "ciphertext",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "session",
        "login_hint",
        "credential",
        "credentials",
        "storage_state",
        "bearer",
    }
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def strip_secrets(value: object, *, depth: int = 0) -> object:
    """Drop credential-shaped keys and redact secret-looking text. Bytes are removed."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            name = str(key).lower().replace("-", "_")
            if name in SECRET_KEYS or name.endswith("_token") or name.endswith("_secret") or name.endswith("_password"):
                continue
            if isinstance(item, bytes | bytearray):
                continue
            cleaned[str(key)] = strip_secrets(item, depth=depth + 1)
        return cleaned
    if isinstance(value, list | tuple):
        return [strip_secrets(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return redact(value)[:2000]
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, bytes | bytearray):
        return None
    return redact(str(value))[:500]


def safe_error(error: BaseException | str) -> str:
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return redact(text)[:MAX_ERROR_LEN]
