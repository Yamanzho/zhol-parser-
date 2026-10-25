"""Entry point for the standalone macOS demo. Standard library and the demo package only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _prepare_environment() -> None:
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("VIRTUAL_ENV", None)
    for name in (
        "ZHOL_2GIS_BROWSER_GRANT",
        "ZHOL_YANDEX_BROWSER_GRANT",
        "ZHOL_GOOGLE_BROWSER_GRANT",
        "ZHOL_SCOUT_LIVE",
    ):
        os.environ.pop(name, None)
    if not getattr(sys, "frozen", False):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))


def _alert(message: str) -> None:
    stream = sys.stderr
    if stream is not None:
        try:
            stream.write(message + "\n")
            stream.flush()
        except Exception:
            pass
    if sys.platform != "darwin":
        return
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display alert "Жол" message "{safe}"'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    _prepare_environment()
    from zhol.demo.server import DemoLaunchError, serve

    try:
        message = serve("127.0.0.1", 8765, open_browser=True)
    except DemoLaunchError as exc:
        _alert(exc.message)
        return 1
    except Exception:
        _alert("Демонстрация не запустилась. Локальный сервер не открылся.")
        return 1
    if message:
        _alert(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
