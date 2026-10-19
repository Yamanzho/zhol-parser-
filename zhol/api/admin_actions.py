"""Authenticated admin actions. Live cabinets, Bek, and paid model calls stay closed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from zhol.api.admin import get_admin_session, require_admin
from zhol.agents.scout.catalog import BatchError
from zhol.agents.scout.browser_2gis import (
    SOURCE_MODE as BROWSER_2GIS,
    BrowserCollectionRefused,
    build_browser_plans,
)
from zhol.agents.scout.browser_google import SOURCE_MODE as BROWSER_GOOGLE
from zhol.agents.scout.browser_google import build_browser_plans as build_google_plans
from zhol.agents.scout.browser_yandex import SOURCE_MODE as BROWSER_YANDEX
from zhol.agents.scout.browser_yandex import build_browser_plans as build_yandex_plans
from zhol.agents.scout.live_2gis import SOURCE_MODE as LIVE_2GIS, CatalogError, build_live_plans
from zhol.agents.scout.setup import CATALOG_NAME, SetupError, build_plans
from zhol.api.admin_jobs import (
    AdminJobError,
    kick_job,
    resume_batch,
    signal_batch,
    start_scout_job,
    start_verification_browser,
)
from zhol.api.filesafe import (
    MAX_HTML_BYTES,
    MAX_XLSX_BYTES,
    AdminPathError,
    admin_root,
    export_destination,
    html_bytes,
    inside,
    open_export,
    order_dir,
    read_upload,
    report_download,
    resolve_source,
    save_upload,
    source_roots,
    xlsx_bytes,
    xlsx_name,
)
from zhol.api.offline_text import OfflineFactProvider
from zhol.content.images import OfflineIllustrationProvider
from zhol.db.base import get_session_factory
from zhol.db.models import ChangePackage, CollectionBatch, ContentItem, Report
from zhol.llm.config import check_startup
from zhol.llm.gateway import Gateway
from zhol.redact import strip_secrets

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])
write_router = APIRouter(prefix="/api/v1/admin", tags=["admin-actions"], dependencies=[Depends(require_admin)])


class ScoutStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_mode: Literal["fixture", "manual"]
    city: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    platforms: list[Literal["2gis", "yandex", "google"]] = Field(min_length=1, max_length=3)
    limit: int | None = Field(default=None, ge=1, le=500)
    catalog: str = Field(min_length=1, max_length=180)


class CollectStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=80)
    cities: list[str] = Field(default_factory=list, max_length=80)
    categories: list[str] = Field(default_factory=list, max_length=20)
    queries: list[str] = Field(default_factory=list, max_length=20)
    platforms: list[Literal["2gis", "yandex", "google"]] = Field(min_length=1, max_length=3)
    limit: int = Field(default=20, ge=1, le=500)
    source_mode: Literal["fixture", "live_2gis", "browser_2gis", "browser_yandex", "browser_google"] = "fixture"
    source_modes: list[Literal["browser_2gis", "browser_google", "browser_yandex"]] = Field(default_factory=list, max_length=3)
    all_cities: bool = False
    all_categories: bool = False
    confirm: bool = False
    skip_unsupported: bool = False
    combination_limit: int | None = Field(default=None, ge=1, le=500)


class ContentApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=1000)


def require_action_header(x_zhol_request: str | None = Header(default=None)) -> None:
    """Bearer tokens are not cookies. This header blocks a cross-site form post."""
    if x_zhol_request != "admin":
        raise HTTPException(status_code=403, detail="missing admin request header")


def get_write_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _error(exc: Exception, status: int) -> HTTPException:
    text = str(exc).replace(str(admin_root()), "admin")
    if len(text) > 300:
        text = text[:300]
    return HTTPException(status_code=status, detail=text or "request failed")


def _offline_gateway() -> Gateway:
    provider = OfflineFactProvider()
    return Gateway(
        check_startup(),
        providers={"anthropic": provider, "openai": provider, "google": provider},
    )


def _fixtures_for(catalog: object, source_mode: str) -> str | None:
    from pathlib import Path

    folder = catalog / "pages" if source_mode == "manual" else catalog.parent / "pages"
    if not isinstance(folder, Path) or not folder.is_dir():
        return None
    resolved = folder.resolve()
    if any(inside(resolved, root) for root in source_roots()):
        return str(resolved)
    return None


@write_router.post("/scout/batches", dependencies=[Depends(require_action_header)])
def post_scout_batch(body: ScoutStart, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    try:
        return _enqueue_scout(session, body)
    except AdminJobError as exc:
        raise _error(exc, 400) from exc


@write_router.post("/scout/collect", dependencies=[Depends(require_action_header)])
def post_scout_collect(body: CollectStart, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    if any(len(item) > 80 for item in body.categories) or any(len(item) > 120 for item in body.queries):
        raise HTTPException(status_code=400, detail="Слишком длинное название категории или запроса.")
    if _wants_plan(body):
        return _collect_plan(session, body)
    if body.source_modes:
        return _collect_separate(session, body)
    try:
        if body.source_mode == "browser_yandex":
            plans = build_yandex_plans(
                city=body.city,
                categories=[item.strip() for item in body.categories],
                queries=[item.strip() for item in body.queries],
                platforms=list(body.platforms),
                limit=body.limit,
            )
        elif body.source_mode == "browser_google":
            plans = build_google_plans(
                city=body.city,
                categories=[item.strip() for item in body.categories],
                queries=[item.strip() for item in body.queries],
                platforms=list(body.platforms),
                limit=body.limit,
            )
        elif body.source_mode == "browser_2gis":
            plans = build_browser_plans(
                city=body.city,
                categories=[item.strip() for item in body.categories],
                queries=[item.strip() for item in body.queries],
                platforms=list(body.platforms),
                limit=body.limit,
            )
        elif body.source_mode == "live_2gis":
            plans = build_live_plans(
                city=body.city,
                categories=[item.strip() for item in body.categories],
                queries=[item.strip() for item in body.queries],
                platforms=list(body.platforms),
                limit=body.limit,
            )
        else:
            catalog = resolve_source(CATALOG_NAME)
            plans = build_plans(
                city=body.city,
                categories=[item.strip() for item in body.categories],
                queries=[item.strip() for item in body.queries],
                platforms=list(body.platforms),
                limit=body.limit,
                catalog_file=catalog,
            )
    except BrowserCollectionRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AdminPathError as exc:
        raise HTTPException(status_code=400, detail="Сохраненный каталог не найден. Запуск сбора из формы недоступен.") from exc
    except (SetupError, CatalogError, BatchError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    jobs = []
    try:
        for plan in plans:
            if plan["source_mode"] == LIVE_2GIS:
                jobs.append(_enqueue_live(session, plan))
            elif plan["source_mode"] == BROWSER_2GIS:
                jobs.append(_enqueue_browser(session, plan))
            elif plan["source_mode"] in {BROWSER_YANDEX, BROWSER_GOOGLE}:
                jobs.append(_enqueue_maps(session, plan))
            else:
                jobs.append(_enqueue_scout(session, ScoutStart.model_validate(plan)))
    except AdminJobError as exc:
        raise _error(exc, 400) from exc
    source = plans[0]["source_mode"]
    if source == LIVE_2GIS:
        data_source = "catalog_api"
    elif source in {BROWSER_2GIS, BROWSER_YANDEX, BROWSER_GOOGLE}:
        data_source = "selenium_public_cards"
    else:
        data_source = "saved_catalog"
    return {
        "jobs": jobs,
        "city": plans[0]["city"],
        "categories": [plan["category"] for plan in plans],
        "platforms": plans[0]["platforms"],
        "limit": body.limit,
        "query_count": len(plans),
        "source_mode": source,
        "data_source": data_source,
        "live_mass_discovery": False,
    }


@write_router.post("/scout/saved-pages", dependencies=[Depends(require_action_header)])
def post_saved_pages(
    city: str = Form(),
    category: str = Form(),
    platforms: str = Form(),
    limit: int = Form(20),
    file_2gis: UploadFile | None = File(None),
    file_yandex: UploadFile | None = File(None),
    file_google: UploadFile | None = File(None),
    screenshot: UploadFile | None = File(None),
    session: Session = Depends(get_write_session),
) -> dict[str, Any]:
    """Import saved HTML. The browser does not open 2GIS, Yandex, or Google."""
    import shutil

    from zhol.agents.scout.saved_import import PLATFORMS, SavedImportError, store_saved_pages
    from zhol.agents.scout.setup import LIMIT_MAX, LIMIT_MIN

    if limit < LIMIT_MIN or limit > LIMIT_MAX:
        raise HTTPException(status_code=400, detail=f"Лимит должен быть от {LIMIT_MIN} до {LIMIT_MAX}.")
    selected = [item.strip().lower() for item in platforms.split(",") if item.strip()]
    if any(name not in PLATFORMS for name in selected) or not selected:
        raise HTTPException(status_code=400, detail="Выберите площадку: 2ГИС, Яндекс Карты или Google Maps.")
    uploads = {"2gis": file_2gis, "yandex": file_yandex, "google": file_google}
    files: dict[str, tuple[str, bytes]] = {}
    for name in PLATFORMS:
        upload = uploads[name]
        has_file = upload is not None and bool(upload.filename)
        if name in selected and not has_file:
            raise HTTPException(status_code=400, detail="Для каждой выбранной площадки нужен HTML-файл.")
        if has_file and name not in selected:
            raise HTTPException(status_code=400, detail="Файл передан для площадки, которая не выбрана.")
        if not has_file or upload is None:
            continue
        try:
            files[name] = (upload.filename or "", read_upload(upload, MAX_HTML_BYTES))
        except AdminPathError as exc:
            raise _error(exc, 400) from exc
    image: tuple[str, bytes] | None = None
    if screenshot is not None and screenshot.filename:
        try:
            image = (screenshot.filename, read_upload(screenshot, 2 * 1024 * 1024))
        except AdminPathError as exc:
            raise _error(exc, 400) from exc
    catalog_name = ""
    try:
        catalog_name = store_saved_pages(files, city=city, category=category, screenshot=image)
        started = ScoutStart(
            source_mode="manual",
            city=" ".join(city.split()),
            category=" ".join(category.split()),
            platforms=list(files),
            limit=limit,
            catalog=catalog_name,
        )
        job = _enqueue_scout(session, started)
    except SavedImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (AdminJobError, AdminPathError, HTTPException):
        if catalog_name:
            shutil.rmtree(admin_root() / "sources" / catalog_name, ignore_errors=True)
        raise
    return {
        "id": job["id"],
        "status": job["status"],
        "duplicate": job["duplicate"],
        "batch_id": job["batch_id"],
        "source_mode": "manual",
        "import_label": "Импорт сохранённых страниц",
        "live_collection": False,
        "data_source": "saved_pages",
        "platforms": list(files),
        "city": started.city,
        "category": started.category,
        "limit": limit,
        "error": job.get("error"),
    }


_SEPARATE_PLATFORM = {"browser_2gis": "2gis", "browser_google": "google", "browser_yandex": "yandex"}


def _wants_plan(body: CollectStart) -> bool:
    return bool(body.cities) or body.all_cities or body.all_categories or body.combination_limit is not None or body.skip_unsupported


def _collect_plan(session: Session, body: CollectStart) -> dict[str, Any]:
    """Queue one job per source. The source cap is not multiplied by cities or queries."""
    from zhol.agents.scout.collection_plan import PlanRejected, build_collection_plan
    from zhol.agents.scout.setup import collection_setup

    catalog = None
    try:
        catalog = resolve_source(CATALOG_NAME)
    except AdminPathError:
        catalog = None
    setup = collection_setup(catalog if catalog is not None and catalog.is_file() else None)
    sources = {str(item["id"]): item for item in setup.get("sources") or []}
    ready = {name: item.get("collection_state") == "available" for name, item in sources.items()}
    notes = {name: str(item.get("live_note") or item.get("note") or "") for name, item in sources.items()}
    try:
        built = build_collection_plan(
            cities=list(body.cities) or ([body.city] if not body.all_cities else []),
            all_cities=body.all_cities,
            categories=[item.strip() for item in body.categories],
            queries=[item.strip() for item in body.queries],
            all_categories=body.all_categories,
            platforms=list(body.platforms),
            limit=body.limit,
            combination_limit=body.combination_limit,
            confirm=body.confirm,
            skip_unsupported=body.skip_unsupported,
            ready=ready,
            blocked_note=notes,
        )
    except PlanRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    jobs: list[dict[str, Any]] = []
    for plan in built["plans"]:
        mode = str(plan["source_mode"])
        first = plan["combinations"][0]
        payload_plan = {
            "platform": plan["platform"],
            "source_mode": mode,
            "source_cap": plan["source_cap"],
            "combination_limit": plan["combination_limit"],
            "combinations": plan["combinations"],
            "cursor": 0,
        }
        material = {
            "source_mode": mode,
            "city": first["city"],
            "category": first["query"],
            "platforms": [plan["platform"]],
            "limit": body.limit,
            "catalog": mode,
            "plan": payload_plan,
        }
        try:
            if mode == "browser_google" or mode == "browser_yandex":
                jobs.append(_enqueue_maps(session, {**material, "source_mode": mode}))
            else:
                jobs.append(_enqueue_browser(session, material))
        except (BrowserCollectionRefused, AdminJobError, BatchError) as exc:
            built["errors"].append({"platform": plan["platform"], "detail": str(exc)})
    if not jobs:
        detail = built["errors"][0]["detail"] if built["errors"] else "Сбор не создан."
        raise HTTPException(status_code=400, detail=detail)
    preview = built["preview"]
    return {
        "jobs": jobs,
        "errors": built["errors"],
        "skipped": built["skipped"],
        "city": built["plans"][0]["combinations"][0]["city"],
        "cities": [item["city"] for item in built["plans"][0]["combinations"]],
        "categories": built["categories"],
        "queries": built["queries"],
        "platforms": [plan["platform"] for plan in built["plans"]],
        "limit": body.limit,
        "limit_scope": "per_source",
        "query_count": preview["combinations"],
        "combination_count": preview["combinations"],
        "estimated_upper_bound": preview["estimated_upper_bound"],
        "source_mode": "plan" if len(built["plans"]) > 1 else built["plans"][0]["source_mode"],
        "data_source": "selenium_public_cards",
        "live_mass_discovery": False,
        "separate_batches": len(built["plans"]) > 1,
        "preview": preview,
        "outcome": "completed_with_warnings" if built["errors"] or built["skipped"] else "completed",
    }


def _collect_separate(session: Session, body: CollectStart) -> dict[str, Any]:
    """One request, one batch per source. A closed source does not cancel the other."""
    if body.source_mode == "browser_yandex":
        raise HTTPException(status_code=400, detail="Яндекс выбирается отдельно и не входит в совместный сбор.")
    modes: list[str] = []
    for mode in body.source_modes:
        if mode not in modes:
            modes.append(mode)
    expected = [_SEPARATE_PLATFORM[mode] for mode in modes]
    if list(body.platforms) != expected:
        raise HTTPException(status_code=400, detail="Площадки должны совпадать с выбранными источниками и идти в том же порядке.")
    jobs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    categories = [item.strip() for item in body.categories]
    queries = [item.strip() for item in body.queries]
    for mode in modes:
        platform = _SEPARATE_PLATFORM[mode]
        try:
            if mode == "browser_google":
                plans = build_google_plans(
                    city=body.city,
                    categories=categories,
                    queries=queries,
                    platforms=[platform],
                    limit=body.limit,
                )
            else:
                plans = build_browser_plans(
                    city=body.city,
                    categories=categories,
                    queries=queries,
                    platforms=[platform],
                    limit=body.limit,
                )
            for plan in plans:
                if mode == "browser_google":
                    jobs.append(_enqueue_maps(session, plan))
                else:
                    jobs.append(_enqueue_browser(session, plan))
        except BrowserCollectionRefused as exc:
            errors.append({"source_mode": mode, "detail": str(exc)})
        except (SetupError, CatalogError, BatchError, AdminJobError) as exc:
            errors.append({"source_mode": mode, "detail": str(exc)})
    if not jobs:
        detail = errors[0]["detail"] if errors else "Сбор не создан."
        raise HTTPException(status_code=400, detail=detail)
    return {
        "jobs": jobs,
        "errors": errors,
        "city": body.city.strip(),
        "categories": categories,
        "platforms": expected,
        "limit": body.limit,
        "query_count": len(jobs),
        "source_mode": modes[0] if len(modes) == 1 else "separate",
        "source_modes": modes,
        "data_source": "selenium_public_cards",
        "live_mass_discovery": False,
        "separate_batches": len(modes) > 1,
        "outcome": "completed_with_warnings" if errors else "completed",
    }


def _enqueue_scout(session: Session, body: ScoutStart) -> dict[str, Any]:
    try:
        catalog = resolve_source(body.catalog)
    except AdminPathError as exc:
        raise _error(exc, 400) from exc
    if body.source_mode == "fixture" and not catalog.is_file():
        raise HTTPException(status_code=400, detail="fixture catalog must be a file in the allowed directories")
    if body.source_mode == "manual" and not catalog.is_dir():
        raise HTTPException(status_code=400, detail="manual discovery needs a saved page directory")
    platforms = list(dict.fromkeys(body.platforms))
    material = {
        "source_mode": body.source_mode,
        "city": body.city.strip(),
        "category": body.category.strip(),
        "platforms": platforms,
        "limit": body.limit,
        "catalog": str(catalog),
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    payload = {
        **material,
        "fixtures_dir": _fixtures_for(catalog, body.source_mode),
        "out": "",
        "dedup_key": f"admin.scout_batch:{digest}",
    }
    return start_scout_job(session, payload)


def _enqueue_maps(session: Session, plan: dict[str, Any]) -> dict[str, Any]:
    mode = str(plan["source_mode"])
    material = {
        "source_mode": mode,
        "city": str(plan["city"]).strip(),
        "category": str(plan["category"]).strip(),
        "platforms": list(plan["platforms"]),
        "limit": plan["limit"],
        "catalog": mode,
    }
    if isinstance(plan.get("plan"), dict):
        material["plan"] = plan["plan"]
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    payload = {
        **material,
        "fixtures_dir": None,
        "out": "",
        "dedup_key": f"admin.scout_batch:{digest}",
    }
    return start_scout_job(session, payload)


def _enqueue_browser(session: Session, plan: dict[str, Any]) -> dict[str, Any]:
    material = {
        "source_mode": BROWSER_2GIS,
        "city": str(plan["city"]).strip(),
        "category": str(plan["category"]).strip(),
        "platforms": ["2gis"],
        "limit": plan["limit"],
        "catalog": BROWSER_2GIS,
    }
    if isinstance(plan.get("plan"), dict):
        material["plan"] = plan["plan"]
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    payload = {
        **material,
        "fixtures_dir": None,
        "out": "",
        "dedup_key": f"admin.scout_batch:{digest}",
    }
    return start_scout_job(session, payload)


def _enqueue_live(session: Session, plan: dict[str, Any]) -> dict[str, Any]:
    material = {
        "source_mode": LIVE_2GIS,
        "city": str(plan["city"]).strip(),
        "category": str(plan["category"]).strip(),
        "platforms": ["2gis"],
        "limit": plan["limit"],
        "catalog": LIVE_2GIS,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    payload = {
        **material,
        "fixtures_dir": None,
        "out": "",
        "dedup_key": f"admin.scout_batch:{digest}",
    }
    return start_scout_job(session, payload)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_admin_session)) -> dict[str, Any]:
    body = kick_job(session, job_id)
    if body is None:
        raise HTTPException(status_code=404, detail="job not found")
    return body


@write_router.post("/scout/batches/{batch_id}/pause", dependencies=[Depends(require_action_header)])
def post_pause(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    return _signal(session, batch_id, "pause")


@write_router.post("/scout/batches/{batch_id}/stop", dependencies=[Depends(require_action_header)])
def post_stop(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    return _signal(session, batch_id, "stop")


@write_router.post("/scout/batches/{batch_id}/resume", dependencies=[Depends(require_action_header)])
def post_resume(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    try:
        return resume_batch(session, batch_id)
    except AdminJobError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise _error(exc, status) from exc


@write_router.post("/scout/batches/{batch_id}/verification-browser", dependencies=[Depends(require_action_header)])
def post_verification_browser(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    try:
        return start_verification_browser(session, batch_id)
    except AdminJobError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise _error(exc, status) from exc


@write_router.post("/scout/batches/{batch_id}/verification-passed", dependencies=[Depends(require_action_header)])
def post_verification_passed(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    return _verification_signal(session, batch_id, "verification_passed")


@write_router.post("/scout/batches/{batch_id}/verification-failed", dependencies=[Depends(require_action_header)])
def post_verification_failed(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    return _verification_signal(session, batch_id, "verification_failed")


def _verification_signal(session: Session, batch_id: int, signal: str) -> dict[str, Any]:
    try:
        batch = signal_batch(session, batch_id, signal)
    except AdminJobError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise _error(exc, status) from exc
    progress = batch.progress if isinstance(batch.progress, dict) else {}
    record = progress.get("manual_verification")
    message = record.get("message") if isinstance(record, dict) else ""
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "signal": signal,
        "message": message if isinstance(message, str) else "",
        "live_mass_discovery": False,
    }


def _signal(session: Session, batch_id: int, signal: str) -> dict[str, Any]:
    try:
        batch = signal_batch(session, batch_id, signal)
    except AdminJobError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise _error(exc, status) from exc
    return {"batch_id": batch.id, "status": batch.status, "signal": signal, "live_mass_discovery": False}


@write_router.post("/scout/export", dependencies=[Depends(require_action_header)])
def post_mailing_export(session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.export.mailing import load_rows, write_xlsx

    path = (admin_root() / "exports" / "batch-0.xlsx").resolve()
    try:
        export_destination(str(path))
    except AdminPathError as exc:
        raise _error(exc, 400) from exc
    count = write_xlsx(load_rows(session), path)
    return {"rows": count, "download": "/api/v1/admin/scout/export.xlsx", "live_mass_discovery": False}


@write_router.post("/scout/platform-export", dependencies=[Depends(require_action_header)])
def post_platform_export(session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.agents.scout.platform_rows import write_platform_workbook

    path = (admin_root() / "exports" / "platforms.xlsx").resolve()
    try:
        export_destination(str(path))
    except AdminPathError as exc:
        raise _error(exc, 400) from exc
    counts = write_platform_workbook(session, path)
    return {
        "sheets": counts,
        "rows": sum(counts.values()),
        "download": "/api/v1/admin/scout/platform-export.xlsx",
        "live_mass_discovery": False,
    }


@router.get("/scout/platform-export.xlsx")
def get_platform_export(session: Session = Depends(get_admin_session)) -> FileResponse:
    """Build the workbook on download. A previously saved file is not the export."""
    from zhol.agents.scout.platform_rows import write_platform_workbook

    path = (admin_root() / "exports" / "platforms.xlsx").resolve()
    try:
        export_destination(str(path))
        write_platform_workbook(session, path)
        target = open_export(str(path))
    except AdminPathError as exc:
        raise HTTPException(status_code=404, detail="export file was not found") from exc
    return FileResponse(
        target,
        filename="2gis-google.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.get("/scout/export.xlsx")
def get_mailing_export() -> FileResponse:
    path = admin_root() / "exports" / "batch-0.xlsx"
    try:
        target = open_export(str(path))
    except AdminPathError as exc:
        raise HTTPException(status_code=404, detail="export file was not found") from exc
    return FileResponse(
        target,
        filename="mailing.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "X-Zhol-Export-Kind": "mailing"},
    )


@write_router.post("/scout/batches/{batch_id}/export", dependencies=[Depends(require_action_header)])
def post_batch_export(batch_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.export.mailing import load_batch_rows, write_xlsx

    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    try:
        path = export_destination(batch.out_path)
    except AdminPathError as exc:
        raise HTTPException(status_code=404, detail="export file was not found") from exc
    count = write_xlsx(load_batch_rows(session, batch_id), path)
    return {
        "batch_id": batch_id,
        "rows": count,
        "download": f"/api/v1/admin/scout/batches/{batch_id}/export.xlsx",
        "live_mass_discovery": False,
    }


@router.get("/scout/batches/{batch_id}/export.xlsx")
def get_batch_export(batch_id: int, session: Session = Depends(get_admin_session)) -> FileResponse:
    batch = session.get(CollectionBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    try:
        target = open_export(batch.out_path)
    except AdminPathError as exc:
        raise HTTPException(status_code=404, detail="export file was not found") from exc
    return FileResponse(
        target,
        filename=f"saved-batch-{batch_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "X-Zhol-Export-Kind": "saved-batch"},
    )


@write_router.post("/audits/validate", dependencies=[Depends(require_action_header)])
def post_validate_audit(file: UploadFile = File(...)) -> dict[str, Any]:
    path = _store_xlsx(file)
    try:
        return {**_workbook_summary(path), "imported": False, "bek_contacted": False}
    finally:
        path.unlink(missing_ok=True)


@write_router.post("/audits/import", dependencies=[Depends(require_action_header)])
def post_import_audit(file: UploadFile = File(...), session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.audit.bek import BekAuditError, import_bek_audit

    path = _store_xlsx(file)
    _workbook_summary(path)
    try:
        stats = import_bek_audit(session, path)
    except BekAuditError as exc:
        raise _error(exc, 400) from exc
    return {
        "sheet": stats.sheet,
        "companies": stats.companies,
        "snapshots": stats.snapshots,
        "audits": stats.audits,
        "plans": stats.plans,
        "unmatched": stats.unmatched[:50],
        "ambiguous": stats.ambiguous[:50],
        "skipped_newer": stats.skipped_newer[:50],
        "tasks_by_type": stats.tasks_by_type,
        "imported": True,
        "bek_contacted": False,
    }


def _store_xlsx(file: UploadFile):
    try:
        xlsx_name(file.filename)
        data = read_upload(file, MAX_XLSX_BYTES)
        xlsx_bytes(data)
        return save_upload(data, ".xlsx")
    except AdminPathError as exc:
        raise _error(exc, 400) from exc


def _workbook_summary(path) -> dict[str, Any]:
    from zhol.audit.workbook import WorkbookError, read_workbook

    try:
        book = read_workbook(path)
    except WorkbookError as exc:
        raise _error(exc, 400) from exc
    return {
        "sheet": book.sheet,
        "sheets": list(book.sheets),
        "rows": len(book.rows),
        "preview": _workbook_preview(book),
    }


def _workbook_preview(book) -> dict[str, Any]:
    """Count companies in the file. This does not match them to the database or write rows."""
    from collections import Counter

    companies: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    ids: list[str] = []
    for row in book.rows:
        name = (row.text("название") or "").strip()
        id_2gis = (row.text("id_2gis") or "").strip()
        if row.skip_reason:
            issues.append({"name": name or "Без названия", "reason": _issue_text(row.skip_reason)})
            continue
        if not name and not id_2gis:
            issues.append({"name": "Без названия", "reason": "В строке нет названия и номера 2ГИС"})
            continue
        if id_2gis:
            ids.append(id_2gis)
        companies.append(
            {
                "name": name or "Без названия",
                "city": (row.text("город") or "").strip() or "Город не указан",
                "score": (row.text("балл_итого") or "").strip() or "Балл не указан",
            }
        )
    counts = Counter(ids)
    seen: dict[str, int] = {}
    for row in book.rows:
        id_2gis = (row.text("id_2gis") or "").strip()
        if not id_2gis or id_2gis not in counts or counts[id_2gis] < 2 or row.skip_reason:
            continue
        seen[id_2gis] = seen.get(id_2gis, 0) + 1
        if seen[id_2gis] > 1:
            name = (row.text("название") or "").strip() or id_2gis
            issues.append({"name": name, "reason": "Повтор номера 2ГИС в файле"})
    duplicates = sum(count - 1 for count in counts.values() if count > 1)
    return {
        "companies": len(companies),
        "duplicates": duplicates,
        "issues": len(issues),
        "issue_rows": issues[:20],
        "rows": companies[:30],
    }


def _issue_text(reason: str) -> str:
    labels = {
        "missing_source_date": "Нет даты просмотра",
        "duplicate id_2gis": "Повтор номера 2ГИС в файле",
    }
    if reason in labels:
        return labels[reason]
    if any("\u0400" <= char <= "\u04ff" for char in reason):
        return reason
    return "Строка требует проверки"


@write_router.post("/orders", dependencies=[Depends(require_action_header)])
def post_order(spec: dict[str, Any], session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.orders.intake import DuplicateOrderError, OrderIntakeError, check_readiness, create_test_order

    if len(json.dumps(spec)) > 200_000:
        raise HTTPException(status_code=413, detail="order spec is too large")
    try:
        order = create_test_order(session, spec)
    except DuplicateOrderError as exc:
        raise _error(exc, 409) from exc
    except OrderIntakeError as exc:
        raise _error(exc, 400) from exc
    report = check_readiness(session, order.id)
    if report is None:
        raise HTTPException(status_code=500, detail="order was not stored")
    return {
        "order_id": order.id,
        "stage": order.stage,
        "readiness": report.as_dict(),
        "bek_contacted": False,
        "live_publication": False,
    }


@write_router.post("/orders/{order_id}/content/prepare", dependencies=[Depends(require_action_header)])
def post_prepare_content(order_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.content.prepare import ContentError, prepare_content

    try:
        folder = order_dir("content", order_id)
        report = prepare_content(
            session,
            order_id,
            gateway=_offline_gateway(),
            output_dir=folder,
            images=OfflineIllustrationProvider(),
        )
    except AdminPathError as exc:
        raise _error(exc, 400) from exc
    except ContentError as exc:
        raise _error(exc, 400) from exc
    return {
        "order_id": report.order_id,
        "items": [strip_secrets(row) for row in report.rows],
        "skipped_reviews": report.skipped_reviews,
        "provider": "offline",
        "publication": "not_published",
    }


@write_router.post("/content/{content_id}/approve", dependencies=[Depends(require_action_header)])
def post_approve_content(
    content_id: int,
    body: ContentApproval,
    session: Session = Depends(get_write_session),
) -> dict[str, Any]:
    from zhol.content.prepare import ContentError, approve_content

    row = session.get(ContentItem, content_id)
    if row is None or not isinstance(row.brief, dict):
        raise HTTPException(status_code=404, detail="content item not found")
    current = int(row.brief.get("version") or 1)
    if current != body.version:
        raise HTTPException(status_code=409, detail="content version does not match")
    try:
        approved = approve_content(session, content_id)
    except ContentError as exc:
        raise _error(exc, 400) from exc
    except PermissionError as exc:
        raise _error(exc, 400) from exc
    brief = approved.brief if isinstance(approved.brief, dict) else {}
    return {
        "id": approved.id,
        "version": int(brief.get("version") or 1),
        "status": approved.status,
        "publication": "not_published",
    }


@write_router.post("/orders/{order_id}/listings/dry-run", dependencies=[Depends(require_action_header)])
def post_dry_run(order_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.fulfillment.engine import FulfillmentError, FulfillmentRefused, run_listing

    try:
        folder = order_dir("listings", order_id)
        report = run_listing(session, order_id, evidence_dir=folder)
    except AdminPathError as exc:
        raise _error(exc, 400) from exc
    except FulfillmentRefused as exc:
        raise _error(exc, 400) from exc
    except FulfillmentError as exc:
        raise _error(exc, 400) from exc
    body = report.as_dict()
    for outcome in body.get("outcomes") or []:
        if isinstance(outcome, dict):
            outcome["evidence_stored"] = bool(outcome.get("evidence"))
            outcome["evidence"] = None
    body["published"] = False
    body["live_publication"] = False
    body["mode"] = "dry_run"
    return strip_secrets(body)  # type: ignore[return-value]


@write_router.post("/orders/{order_id}/listings/prepare", dependencies=[Depends(require_action_header)])
def post_prepare_listing(
    order_id: int,
    package_id: int = Form(...),
    version: int = Form(...),
    platform: str = Form(...),
    operation: str = Form(...),
    mode: str = Form(default="offline"),
    page_url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    session: Session = Depends(get_write_session),
) -> dict[str, Any]:
    _listing_guards(mode, page_url, platform, operation)
    package = _exact_package(session, order_id, package_id, version)
    page = _saved_page(file)
    if page.startswith(("http://", "https://", "file://")):
        raise HTTPException(status_code=400, detail="live listing submission is closed")
    from zhol.fulfillment.engine import FulfillmentError, FulfillmentRefused

    prepare = _prepare_listing(platform, operation)
    try:
        folder = order_dir("listings", order_id) / f"{platform}-{operation}"
        folder.mkdir(parents=True, exist_ok=True)
        report = prepare(session, order_id, page=page, evidence_dir=folder, wait_seconds=0, approve=False)
    except (AdminPathError, FulfillmentError, FulfillmentRefused, PermissionError, FileNotFoundError, OSError) as exc:
        raise _error(exc, 400) from exc
    body = report.as_dict()
    body["package_id"] = package.id
    body["version"] = package.version
    body["published"] = False
    body["submitted"] = False
    body["live_publication"] = False
    return strip_secrets(body)  # type: ignore[return-value]


@write_router.post("/orders/{order_id}/listings/approve", dependencies=[Depends(require_action_header)])
def post_approve_listing(
    order_id: int,
    package_id: int = Form(...),
    version: int = Form(...),
    platform: str = Form(...),
    operation: str = Form(...),
    mode: str = Form(default="offline"),
    page_url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    session: Session = Depends(get_write_session),
) -> dict[str, Any]:
    from zhol.fulfillment.submission import approve_listing

    _listing_guards(mode, page_url, platform, operation)
    _exact_package(session, order_id, package_id, version)
    page = _listing_page(order_id, platform, operation, file)
    return _listing_call(session, order_id, approve_listing, platform, operation, page)


@write_router.post("/orders/{order_id}/listings/simulate", dependencies=[Depends(require_action_header)])
def post_simulate_listing(
    order_id: int,
    package_id: int = Form(...),
    version: int = Form(...),
    platform: str = Form(...),
    operation: str = Form(...),
    mode: str = Form(default="offline"),
    page_url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    session: Session = Depends(get_write_session),
) -> dict[str, Any]:
    from zhol.fulfillment.submission import submit_listing

    _listing_guards(mode, page_url, platform, operation)
    _exact_package(session, order_id, package_id, version)
    page = _listing_page(order_id, platform, operation, file)

    def _submit(db: Session, order: int, *, platform: str, operation: str, page: str, evidence_dir):
        return submit_listing(db, order, platform=platform, operation=operation, page=page, evidence_dir=evidence_dir, mode="offline")

    body = _listing_call(session, order_id, _submit, platform, operation, page)
    body["simulated"] = True
    body["published"] = False
    body["live_publication"] = False
    return body


def _prepare_listing(platform: str, operation: str):
    if platform == "2gis" and operation == "update":
        from zhol.browser.twogis.prepare import prepare_2gis_listing

        return prepare_2gis_listing
    if platform == "yandex" and operation == "update":
        from zhol.browser.yandex.prepare import prepare_yandex_listing

        return prepare_yandex_listing
    if platform == "google" and operation == "update":
        from zhol.browser.google.prepare import prepare_google_listing

        return prepare_google_listing
    if platform == "2gis" and operation == "create":
        from zhol.browser.twogis.create import prepare_2gis_create

        return prepare_2gis_create
    if platform == "yandex" and operation == "create":
        from zhol.browser.yandex.create import prepare_yandex_create

        return prepare_yandex_create
    if platform == "google" and operation == "create":
        from zhol.browser.google.create import prepare_google_create

        return prepare_google_create
    raise HTTPException(status_code=400, detail="platform or operation is not supported")


def _listing_guards(mode: str, page_url: str, platform: str, operation: str) -> None:
    if mode != "offline" or page_url.strip():
        raise HTTPException(status_code=400, detail="live listing submission is closed")
    if platform not in {"2gis", "yandex", "google"} or operation not in {"create", "update"}:
        raise HTTPException(status_code=400, detail="platform or operation is not supported")


def _exact_package(session: Session, order_id: int, package_id: int, version: int) -> ChangePackage:
    package = session.get(ChangePackage, package_id)
    if package is None or package.order_id != order_id:
        raise HTTPException(status_code=404, detail="change package not found")
    if package.version != version:
        raise HTTPException(status_code=409, detail="change package version does not match")
    return package


def _listing_page(order_id: int, platform: str, operation: str, file: UploadFile | None) -> str:
    folder = (order_dir("listings", order_id) / f"{platform}-{operation}").resolve()
    drafted = (folder / "page.html").resolve()
    if drafted.is_file() and drafted.parent == folder:
        return str(drafted)
    return _saved_page(file)


def _saved_page(file: UploadFile | None) -> str:
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="a saved offline page is required")
    try:
        data = read_upload(file, MAX_HTML_BYTES)
        html_bytes(data)
        return str(save_upload(data, ".html"))
    except AdminPathError as exc:
        raise _error(exc, 400) from exc


def _listing_call(session: Session, order_id: int, run: Any, platform: str, operation: str, page: str) -> dict[str, Any]:
    from zhol.fulfillment.engine import FulfillmentError, FulfillmentRefused

    try:
        folder = order_dir("listings", order_id) / f"{platform}-{operation}"
        folder.mkdir(parents=True, exist_ok=True)
        report = run(session, order_id, platform=platform, operation=operation, page=page, evidence_dir=folder)
    except (FulfillmentError, FulfillmentRefused, PermissionError, FileNotFoundError, OSError) as exc:
        raise _error(exc, 400) from exc
    body = report.as_dict()
    body["live_publication"] = False
    body["mode"] = "offline"
    return strip_secrets(body)  # type: ignore[return-value]


@write_router.post("/orders/{order_id}/reports/completion", dependencies=[Depends(require_action_header)])
def post_completion(order_id: int, session: Session = Depends(get_write_session)) -> dict[str, Any]:
    from zhol.reports.completion import CompletionError, generate_completion_report

    try:
        folder = order_dir("reports", order_id)
        result = generate_completion_report(session, order_id, folder)
    except AdminPathError as exc:
        raise _error(exc, 400) from exc
    except CompletionError as exc:
        raise _error(exc, 400) from exc
    document = result["report"]
    stored = _latest_report(session, order_id)
    if stored is None:
        raise HTTPException(status_code=500, detail="report was not stored")
    return _report_body(stored, document)


@router.get("/reports/{report_id}/completion.json")
def get_completion_json(report_id: int, session: Session = Depends(get_admin_session)) -> FileResponse:
    return _report_file(session, report_id, "json")


@router.get("/reports/{report_id}/completion.html")
def get_completion_html(report_id: int, session: Session = Depends(get_admin_session)) -> FileResponse:
    return _report_file(session, report_id, "html")


def _latest_report(session: Session, order_id: int) -> Report | None:
    from sqlalchemy import select

    return session.scalar(select(Report).where(Report.order_id == order_id).order_by(Report.id.desc()).limit(1))


def _report_body(row: Report, document: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.id,
        "order_id": row.order_id,
        "verification_mode": document.get("verification_mode"),
        "outcome": document.get("outcome"),
        "completed": document.get("completed"),
        "live_verified": False,
        "live_publication": False,
        "downloads": {
            "json": f"/api/v1/admin/reports/{row.id}/completion.json",
            "html": f"/api/v1/admin/reports/{row.id}/completion.html",
        },
    }


def _report_file(session: Session, report_id: int, kind: str) -> FileResponse:
    row = session.get(Report, report_id)
    if row is None or row.order_id is None:
        raise HTTPException(status_code=404, detail="report not found")
    try:
        target = report_download(row.order_id, kind)
    except AdminPathError as exc:
        raise HTTPException(status_code=404, detail="report file was not found") from exc
    media = "application/json" if kind == "json" else "text/html"
    return FileResponse(target, filename=target.name, media_type=media)

