"""Create a private .env from .env.example. An existing file is left unchanged.

Generated values stay on this Mac. A grant already written into the example
stops installation. Grants are never copied from another environment file.
"""

from __future__ import annotations

import secrets
import stat
import sys
from pathlib import Path

GRANT_KEYS = (
    "ZHOL_2GIS_BROWSER_GRANT",
    "ZHOL_YANDEX_BROWSER_GRANT",
    "ZHOL_GOOGLE_BROWSER_GRANT",
)
GENERATED = {
    "POSTGRES_USER": "zhol",
    "POSTGRES_DB": "zhol",
    "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(24),
    "API_PORT": "18080",
    "WEB_PORT": "15173",
    "ZHOL_ADMIN_TOKEN": lambda: secrets.token_hex(32),
    "ZHOL_LOCAL_GATE": lambda: secrets.token_hex(32),
    "FILE_LINK_SECRET": lambda: secrets.token_hex(32),
    "SECRETS_KEY": lambda: secrets.token_urlsafe(32),
    "TZ": "Asia/Almaty",
    "ZHOL_BROWSER_ENGINE": "1",
}


def _value(spec: object) -> str:
    if callable(spec):
        return str(spec())
    return str(spec)


def write_env(target: Path, example: Path) -> str:
    if target.exists():
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return "kept"
    if not example.is_file():
        raise SystemExit("Нет .env.example. Установка остановлена.")
    seen: set[str] = set()
    rewritten: list[str] = []
    for raw in example.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            rewritten.append(raw)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        seen.add(key)
        if key in GRANT_KEYS and value.strip():
            raise SystemExit(f"{key} в .env.example должен остаться пустым. Разрешение не копируется.")
        if key in GENERATED and not value.strip():
            rewritten.append(f"{key}={_value(GENERATED[key])}")
        else:
            rewritten.append(f"{key}={value}")
    for key, spec in GENERATED.items():
        if key not in seen:
            rewritten.append(f"{key}={_value(spec)}")
    for key in GRANT_KEYS:
        if key not in seen:
            rewritten.append(f"{key}=")
    target.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return "created"


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    print(f"env={write_env(root / '.env', root / '.env.example')}")


if __name__ == "__main__":
    main()
