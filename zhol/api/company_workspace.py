"""Company workspace for the admin panel.

Reads orders, plans, drafts, listing packages and reports that already belong
to the company. It does not create orders, publish listings, or change rows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zhol.api.admin_data import list_listings
from zhol.api.company_view import PLATFORM_TITLE
from zhol.audit.bek import read_company_plan
from zhol.content.prepare import list_content
from zhol.db.models import Audit, HumanTask, Order, Report
from zhol.orders.intake import check_readiness
from zhol.redact import strip_secrets

TASK_RU = {
    "create_listing_candidate": "Создать карточку",
    "update_listing": "Обновить карточку",
    "add_description": "Добавить описание",
    "update_contacts": "Обновить контакты",
    "update_hours": "Обновить график",
    "add_services": "Добавить услуги",
    "add_prices": "Добавить цены",
    "request_real_photos": "Нужны реальные фото",
    "prepare_review_replies": "Подготовить ответы на отзывы",
    "request_owner_verification": "Нужно подтверждение владельца",
    "reconcile_platform_data": "Сверить данные площадок",
    "request_client_facts": "Нужны данные от клиента",
}
STATUS_RU = {
    "ready_for_preparation": "Можно подготовить",
    "blocked": "Пока нельзя",
    "manual": "Нужен человек",
    "unsupported": "Площадка это не принимает",
    "done": "Черновик готов",
    "checked": "Утверждено",
    "draft": "Черновик",
    "approved": "Утверждено",
}
STAGE_RU = {
    "created": "Заказ создан",
    "prepay_invoiced": "Счет на предоплату",
    "prepaid": "Предоплата получена",
    "intake": "Ждем материалы",
    "package": "Пакет правок",
    "client_review": "На согласовании у клиента",
    "applying": "Внесение правок",
    "moderation": "На модерации",
    "report": "Отчет",
    "delivered": "Сдано",
    "final_invoiced": "Финальный счет",
    "closed": "Закрыт",
    "cancelled": "Отменен",
    "human": "Нужен человек",
}
KIND_RU = {
    "description": "Описание",
    "review_reply": "Ответ на отзыв",
    "cover": "Обложка",
    "banner": "Баннер",
    "price_image": "Прайс картинкой",
}
FIELD_RU = {
    "description": "описание",
    "phones": "телефон",
    "hours": "график",
    "category": "рубрика",
    "address": "адрес",
    "name": "название",
    "website": "сайт",
    "services": "услуги",
    "photos": "фото",
    "prices": "цены",
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
}


def company_workspace(session: Session, company_id: int, audit: Audit | None) -> dict[str, Any]:
    plan = _plan(session, company_id)
    orders = list(
        session.scalars(select(Order).where(Order.company_id == company_id).order_by(Order.id.desc()).limit(20)).all()
    )
    order_ids = [row.id for row in orders]
    materials = _materials(session, order_ids)
    packages = _packages(session, order_ids)
    reports = _reports(session, order_ids)
    tasks = list(
        session.scalars(
            select(HumanTask)
            .where(HumanTask.object_type == "company", HumanTask.object_id == company_id, HumanTask.status == "open")
            .order_by(HumanTask.id.desc())
            .limit(20)
        ).all()
    )
    improvements = _improvements(audit, plan)
    report = _report_view(session, orders, packages, materials, reports)
    approvals = _approvals(materials, packages, tasks)
    return {
        "improvements": improvements,
        "prepare": {
            "orders": [_order(row) for row in orders],
            "tasks": [item for item in _tasks(plan) if item["lane"] == "automatic_preparation"],
            "note": "Подготовка использует уже сохраненный заказ. Новая публикация не запускается.",
        },
        "materials": materials,
        "changes": packages,
        "approvals": approvals,
        "report": report,
    }


def _plan(session: Session, company_id: int) -> dict[str, Any] | None:
    try:
        return read_company_plan(session, company_id)
    except Exception:
        return None


def _improvements(audit: Audit | None, plan: dict[str, Any] | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if audit is not None:
        for text in _texts(audit.observations)[:12]:
            rows.append({"title": text, "platform": "", "detail": "Наблюдение аудита", "lane": "audit"})
        for text in _gap_lines(audit.gaps)[:12]:
            rows.append({"title": text, "platform": "", "detail": "Пробел аудита", "lane": "gap"})
    for task in _tasks(plan):
        if task["lane"] == "manual":
            rows.append(task)
    return rows[:40]


def _tasks(plan: dict[str, Any] | None) -> list[dict[str, str]]:
    body = {}
    if isinstance(plan, dict):
        stored = plan.get("plan")
        body = stored if isinstance(stored, dict) else {}
    rows: list[dict[str, str]] = []
    for lane in ("automatic_preparation", "manual_work"):
        for task in body.get(lane) or []:
            if not isinstance(task, dict):
                continue
            platform = PLATFORM_TITLE.get(str(task.get("platform") or ""), "")
            kind = TASK_RU.get(str(task.get("type") or ""), "Улучшение карточки")
            field = FIELD_RU.get(str(task.get("field") or ""), "")
            title = kind if not field else f"{kind}: {field}"
            rows.append(
                {
                    "title": title,
                    "platform": platform,
                    "detail": STATUS_RU.get(str(task.get("status") or ""), "Смотрите статус в базе"),
                    "lane": "manual" if lane == "manual_work" else "automatic_preparation",
                }
            )
    return rows


def _materials(session: Session, order_ids: list[int]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for order_id in order_ids:
        _total, items = list_content(session, order_id=order_id, status=None, limit=50, offset=0)
        for item in items:
            text = item.get("text") if isinstance(item.get("text"), str) else ""
            status = str(item.get("approval_status") or item.get("status") or "")
            found.append(
                {
                    "id": item.get("id"),
                    "order_id": order_id,
                    "title": KIND_RU.get(str(item.get("kind") or ""), "Материал"),
                    "platform": PLATFORM_TITLE.get(str(item.get("platform") or ""), ""),
                    "text": strip_secrets(text) if text else "",
                    "status_label": STATUS_RU.get(status, "Сохранено"),
                    "can_approve": status == "done",
                    "version": item.get("version") or 1,
                    "illustrative": item.get("illustrative") is True,
                }
            )
    return found[:50]


def _packages(session: Session, order_ids: list[int]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for order_id in order_ids:
        page = list_listings(session, order_id=order_id, limit=20, offset=0)
        for package in page.get("items") or []:
            if not isinstance(package, dict):
                continue
            found.append(
                {
                    "order_id": order_id,
                    "package_id": package.get("package_id"),
                    "version": package.get("version"),
                    "phase_label": _phase_ru(str(package.get("phase") or "")),
                    "targets": [
                        {
                            "platform": PLATFORM_TITLE.get(str(item.get("platform") or ""), str(item.get("platform") or "")),
                            "platform_code": item.get("platform"),
                            "operation": item.get("operation") or "update",
                            "preparation": item.get("preparation"),
                            "approval": item.get("approval"),
                            "submission": item.get("submission"),
                            "published": False,
                        }
                        for item in (package.get("targets") or [])
                        if isinstance(item, dict)
                    ],
                    "items": [
                        {
                            "platform": PLATFORM_TITLE.get(str(item.get("platform") or ""), ""),
                            "field": FIELD_RU.get(str(item.get("field") or ""), str(item.get("field") or "")),
                            "before": _short(item.get("before")),
                            "after": _short(item.get("after")),
                            "status_label": _phase_ru(str(item.get("phase") or item.get("status") or "")),
                        }
                        for item in (package.get("items") or [])
                        if isinstance(item, dict)
                    ],
                }
            )
    return found[:20]


def _reports(session: Session, order_ids: list[int]) -> list[Report]:
    if not order_ids:
        return []
    return list(session.scalars(select(Report).where(Report.order_id.in_(order_ids)).order_by(Report.id.desc()).limit(20)).all())


def _report_view(
    session: Session,
    orders: list[Order],
    packages: list[dict[str, Any]],
    materials: list[dict[str, Any]],
    reports: list[Report],
) -> dict[str, Any]:
    completed: list[str] = []
    prepared: list[str] = []
    awaiting: list[str] = []
    moderation: list[str] = []
    missing: list[str] = []
    for order in orders:
        ready = check_readiness(session, order.id)
        if ready is not None:
            missing.extend(_missing_lines(ready.missing_company_information, "Данные компании"))
            missing.extend(_missing_lines(ready.missing_materials, "Материалы"))
            missing.extend(_missing_lines(ready.missing_owner_permissions, "Доступ владельца"))
            missing.extend(_missing_lines(ready.unresolved_ambiguities, "Неясность"))
    for package in packages:
        for item in package.get("items") or []:
            line = _change_line(item)
            label = str(item.get("status_label") or "")
            if label == "Совпало с сохраненными данными":
                completed.append(line)
            elif label == "На модерации":
                moderation.append(line)
            elif label in {"Черновик", "Ожидает"}:
                prepared.append(line)
        for target in package.get("targets") or []:
            if target.get("approval") in {None, "none", "awaiting", "awaiting_approval"} and target.get("preparation") == "prepared":
                awaiting.append(f"{target.get('platform')}: правка подготовлена и ждет утверждения")
            if str(target.get("submission") or "") in {"moderation_pending", "moderation"}:
                moderation.append(f"{target.get('platform')}: модерация еще не завершена")
    for item in materials:
        if item.get("can_approve"):
            awaiting.append(f"{item.get('title')}: черновик ждет утверждения")
        elif item.get("status_label") == "Утверждено":
            completed.append(f"{item.get('title')}: утверждено, не опубликовано")
        else:
            prepared.append(f"{item.get('title')}: {item.get('status_label')}")
    for report in reports:
        data = report.data if isinstance(report.data, dict) else {}
        if data.get("completed") is True:
            completed.append("Сохраненный отчет отмечает работу завершенной")
    downloads = []
    for report in reports:
        if report.order_id is None:
            continue
        files = _report_files(report.order_id)
        if files["json"] or files["html"]:
            downloads.append({"id": report.id, "order_id": report.order_id, "json": files["json"], "html": files["html"]})
    return {
        "completed": _unique(completed),
        "prepared": _unique(prepared),
        "awaiting_approval": _unique(awaiting),
        "moderation_pending": _unique(moderation),
        "missing": _unique(missing),
        "downloads": downloads,
        "orders": [_order(row) for row in orders],
    }


def _approvals(
    materials: list[dict[str, Any]],
    packages: list[dict[str, Any]],
    tasks: list[HumanTask],
) -> dict[str, Any]:
    pending = [
        {
            "kind": "material",
            "id": item["id"],
            "order_id": item["order_id"],
            "version": item["version"],
            "title": f"{item['title']}, версия {item['version']}",
        }
        for item in materials
        if item.get("can_approve")
    ]
    listing = []
    for package in packages:
        for target in package.get("targets") or []:
            if target.get("approval") in {None, "none", "awaiting", "awaiting_approval"}:
                listing.append(
                    {
                        "order_id": package.get("order_id"),
                        "version": package.get("version"),
                        "platform": target.get("platform"),
                        "platform_code": target.get("platform_code"),
                        "operation": target.get("operation") or "update",
                    }
                )
    return {
        "materials": pending,
        "listings": listing[:20],
        "human_tasks": [
            {
                "id": row.id,
                "title": _task_title(row.reason),
                "status_label": "Открыта",
            }
            for row in tasks
        ],
    }


def _order(row: Order) -> dict[str, Any]:
    return {"id": row.id, "stage_label": STAGE_RU.get(row.stage, "Этап сохранен"), "amount_kzt": row.amount_kzt}


def _texts(observed: object) -> list[str]:
    if isinstance(observed, dict):
        raw = observed.get("texts")
        if isinstance(raw, list):
            return [item.strip() for item in raw if isinstance(item, str) and item.strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
    if isinstance(observed, list):
        return [item.strip() for item in observed if isinstance(item, str) and item.strip()]
    return []


def _gap_lines(gaps: object) -> list[str]:
    if isinstance(gaps, list):
        lines = []
        for item in gaps:
            if isinstance(item, str) and item.strip():
                lines.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("field") or item.get("gap")
                if isinstance(text, str) and text.strip():
                    lines.append(text.strip())
        return lines
    return []


def _missing_lines(values: object, label: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [f"{label}: {item}" for item in values if item]


def _change_line(item: dict[str, Any]) -> str:
    platform = item.get("platform") or "Площадка"
    field = item.get("field") or "поле"
    before = item.get("before") or "нет данных"
    after = item.get("after") or "нет данных"
    return f"{platform}, {field}: {before} -> {after}"


def _short(value: object) -> str:
    cleaned = strip_secrets(value)
    if cleaned is None:
        return "нет данных"
    if isinstance(cleaned, str):
        text = cleaned.strip()
    elif isinstance(cleaned, dict):
        text = str(cleaned.get("value") or cleaned.get("text") or "")
    else:
        text = str(cleaned)
    text = " ".join(text.split())
    return text[:180] or "нет данных"


def _phase_ru(phase: str) -> str:
    labels = {
        "draft": "Черновик",
        "submitted": "Отмечено в сохраненных данных",
        "moderation": "На модерации",
        "verified": "Совпало с сохраненными данными",
        "rejected": "Отклонено",
        "uncertain": "Неясно",
        "human_required": "Нужен человек",
        "prepared": "Подготовлено",
        "awaiting_approval": "Ожидает",
    }
    return labels.get(phase, "Сохранено")


def _task_title(reason: str) -> str:
    labels = {
        "bek_audit_ambiguous": "В книге аудита неоднозначная строка",
        "bek_audit_unmatched": "Строка аудита не сопоставилась с компанией",
    }
    return labels.get(reason, "Нужен человек")


def _unique(lines: list[str]) -> list[str]:
    seen: list[str] = []
    for line in lines:
        if line and line not in seen:
            seen.append(line)
    return seen[:30]


def _report_files(order_id: int) -> dict[str, bool]:
    env = os.environ.get("ZHOL_ADMIN_FILES", "").strip()
    root = Path(env).resolve() if env else Path(__file__).resolve().parents[2] / "data" / "admin"
    folder = root / "reports" / str(int(order_id))
    return {
        "json": (folder / "completion.json").is_file(),
        "html": (folder / "completion.html").is_file(),
    }
