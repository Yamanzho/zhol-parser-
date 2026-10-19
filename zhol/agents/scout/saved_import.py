"""Store uploaded saved pages under the admin source root and return a relative name.

The caller does not choose a server path. Live sites are not opened.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

import yaml

from zhol.api.filesafe import MAX_HTML_BYTES, AdminPathError, admin_root, html_bytes, inside

PLATFORMS = ("2gis", "yandex", "google")
_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_PNG = 2 * 1024 * 1024


class SavedImportError(ValueError):
    pass


def store_saved_pages(
    files: dict[str, tuple[str, bytes]],
    *,
    city: str,
    category: str,
    screenshot: tuple[str, bytes] | None = None,
) -> str:
    """Write one HTML file per selected platform. Return the relative catalog name."""
    chosen = _platforms(files)
    city_name = " ".join(city.split())
    category_name = " ".join(category.split())
    if not city_name or len(city_name) > 80:
        raise SavedImportError("Укажите город.")
    if not category_name or len(category_name) > 80:
        raise SavedImportError("Укажите категорию.")
    starts: dict[str, str] = {}
    payloads: dict[str, tuple[str, bytes]] = {}
    for platform in chosen:
        name, data = files[platform]
        _html(platform, name, data, category_name)
        payloads[platform] = (_html_name(name, platform), data)
        starts[platform] = f"{platform}/{payloads[platform][0]}"
    image = _png(screenshot)
    folder_name = f"saved-{uuid.uuid4().hex}"
    root = (admin_root() / "sources" / folder_name).resolve()
    base = (admin_root() / "sources").resolve()
    if not inside(root, base) or root.parent != base:
        raise SavedImportError("Не удалось сохранить загруженные страницы.")
    try:
        root.mkdir(parents=True, exist_ok=False)
        for platform, (name, data) in payloads.items():
            dest = (root / platform / name).resolve()
            if not inside(dest, root / platform):
                raise SavedImportError("Имя файла страницы не принято.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if image is not None:
            shot = (root / "pages" / "blank.png").resolve()
            if not inside(shot, root):
                raise SavedImportError("Файл скриншота не принят.")
            shot.parent.mkdir(parents=True, exist_ok=True)
            shot.write_bytes(image)
        manifest = {"city": city_name, "category": category_name, "starts": starts}
        (root / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    except SavedImportError:
        shutil.rmtree(root, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise SavedImportError("Не удалось сохранить загруженные страницы.") from exc
    return folder_name


def _platforms(files: dict[str, tuple[str, bytes]]) -> list[str]:
    found = [name for name in PLATFORMS if name in files]
    extra = [name for name in files if name not in PLATFORMS]
    if extra:
        raise SavedImportError("Неизвестный источник сохранённой страницы.")
    if not found:
        raise SavedImportError("Выберите HTML хотя бы для одной площадки: 2ГИС, Яндекс Карты или Google Maps.")
    return found


def _html(platform: str, name: str, data: bytes, category: str) -> None:
    if len(data) > MAX_HTML_BYTES:
        raise SavedImportError("Файл страницы слишком большой.")
    try:
        html_bytes(data)
    except AdminPathError as exc:
        raise SavedImportError("Нужен сохранённый HTML-файл, а не адрес сайта.") from exc
    text = data.decode("utf-8", errors="replace")
    marker = f'data-platform="{platform}"'
    if marker not in text and marker.replace('"', "'") not in text:
        raise SavedImportError(f"Файл для {platform} не похож на сохранённую страницу этой площадки.")
    if 'data-page="search"' not in text and "data-page='search'" not in text:
        raise SavedImportError("Нужна сохранённая страница поиска, а не живой адрес сайта.")
    if category.casefold() not in text.casefold():
        raise SavedImportError("В сохранённой странице нет выбранной категории.")
    if "/" in name or "\\" in name or ".." in name:
        raise SavedImportError("Имя файла страницы не принято.")


def _html_name(name: str, platform: str) -> str:
    base = Path(name or "").name
    stem = Path(base).stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", stem).strip(".-")[:60]
    if not cleaned:
        cleaned = platform
    return f"{cleaned}.html"


def _png(screenshot: tuple[str, bytes] | None) -> bytes | None:
    if screenshot is None:
        return None
    _name, data = screenshot
    if not data:
        raise SavedImportError("Файл скриншота пустой.")
    if len(data) > _MAX_PNG or not data.startswith(_PNG):
        raise SavedImportError("Необязательный скриншот должен быть файлом PNG.")
    return data
