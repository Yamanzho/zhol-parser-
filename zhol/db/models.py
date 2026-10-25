from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from zhol.db.base import Base

TZ = DateTime(timezone=True)
NOW = sql_text("now()")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (Index("ix_companies_city_lower", sql_text("lower(city)")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Internal identity is `id`. A 2GIS firm id is optional: Yandex-only and Google-only companies leave it empty.
    id_2gis: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'Алматы'"))
    niche: Mapped[str | None] = mapped_column(Text)
    whatsapp: Mapped[str | None] = mapped_column(Text)
    instagram: Mapped[str | None] = mapped_column(Text)
    dnc: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class CompanyPlatformId(Base):
    __tablename__ = "company_platform_ids"
    __table_args__ = (
        CheckConstraint("platform in ('2gis','yandex','google')", name="ck_company_platform_ids_platform"),
        Index(
            "uq_company_platform_ids_platform_external",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=sql_text("external_id IS NOT NULL"),
        ),
    )

    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(Text)
    public_url: Mapped[str | None] = mapped_column(Text)
    cabinet_url: Mapped[str | None] = mapped_column(Text)
    access_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'none'"))


class CompanyProfile(Base):
    __tablename__ = "company_profiles"
    __table_args__ = (UniqueConstraint("company_id", "version", name="uq_company_profiles_company_id_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class CardSnapshot(Base):
    __tablename__ = "card_snapshots"
    __table_args__ = (
        CheckConstraint("platform in ('2gis','yandex','google')", name="ck_card_snapshots_platform"),
        CheckConstraint(
            "match_status in ('found','other_address','not_found')",
            name="ck_card_snapshots_match_status",
        ),
        Index(
            "ix_card_snapshots_latest",
            "company_id",
            "platform",
            sql_text("collected_at DESC"),
            sql_text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    match_status: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    field_sources: Mapped[dict] = mapped_column(JSONB, nullable=False)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    collected_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'collected'"))
    recheck_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    fail_reason: Mapped[str | None] = mapped_column(Text)


class Audit(Base):
    __tablename__ = "audits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    snapshot_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    score_total: Mapped[int] = mapped_column(Integer, nullable=False)
    score_groups: Mapped[dict] = mapped_column(JSONB, nullable=False)
    derived: Mapped[dict] = mapped_column(JSONB, nullable=False)
    discrepancies: Mapped[dict] = mapped_column(JSONB, nullable=False)
    gaps: Mapped[dict] = mapped_column(JSONB, nullable=False)
    strength: Mapped[dict | None] = mapped_column(JSONB)
    observations: Mapped[dict | None] = mapped_column(JSONB)
    onepager_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'draft'"))
    prompt_version: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    syntony_deal_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("kind in ('setup','subscription')", name="ck_orders_kind"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    branches: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    amount_kzt: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    stage_before_human: Mapped[str | None] = mapped_column(Text)
    stage_before_revision: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    changes_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    revision_until: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("part in ('prepay','final','monthly')", name="ck_invoices_part"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    part: Mapped[str] = mapped_column(Text, nullable=False)
    amount_kzt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    paid_via: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    confirm_note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime | None] = mapped_column(TZ)


class Intake(Base):
    __tablename__ = "intake"

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    checklist: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class ClientMessage(Base):
    __tablename__ = "client_messages"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(TZ)


class ClientFile(Base):
    __tablename__ = "client_files"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    login_hint: Mapped[str | None] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    deleted_at: Mapped[datetime | None] = mapped_column(TZ)


class BrowserProfile(Base):
    __tablename__ = "browser_profiles"
    __table_args__ = (UniqueConstraint("platform", "account_label", name="uq_browser_profiles_platform_account"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    account_label: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'needs_login'"))
    last_check_at: Mapped[datetime | None] = mapped_column(TZ)


class ChangePackage(Base):
    __tablename__ = "change_packages"
    __table_args__ = (UniqueConstraint("order_id", "version", name="uq_change_packages_order_id_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'draft'"))
    summary_for_client: Mapped[str | None] = mapped_column(Text)
    approval_message_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class ChangeItem(Base):
    __tablename__ = "change_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("change_packages.id"))
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    source_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_nap: Mapped[bool] = mapped_column(Boolean, nullable=False)
    nap_consent_ref: Mapped[str | None] = mapped_column(Text)
    counts_to_limit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'draft'"))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    moderation_checks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))


class ChangeLog(Base):
    __tablename__ = "change_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    change_item_id: Mapped[int | None] = mapped_column(ForeignKey("change_items.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    screenshot_before: Mapped[str | None] = mapped_column(Text)
    screenshot_after: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_reviews_platform_external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    author_label: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(TZ)
    reply_text: Mapped[str | None] = mapped_column(Text)
    reply_language: Mapped[str | None] = mapped_column(Text)
    reply_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'new'"))
    complaint_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'none'"))
    complaint_text: Mapped[str | None] = mapped_column(Text)


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[dict | None] = mapped_column(JSONB)
    source_file_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    result_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'todo'"))


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    period: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    xlsx_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'draft'"))


class HumanTask(Base):
    __tablename__ = "human_tasks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'open'"))
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    closed_at: Mapped[datetime | None] = mapped_column(TZ)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_ready",
            "queue",
            "run_after",
            "id",
            postgresql_where=sql_text("status IN ('queued', 'failed')"),
        ),
        Index(
            "ix_jobs_running_lease",
            "run_after",
            "id",
            postgresql_where=sql_text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    queue: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'main'"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    run_after: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    dedup_key: Mapped[str | None] = mapped_column(Text, unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    prompt_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    input_hash: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    schema_ok: Mapped[bool | None] = mapped_column(Boolean)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class EventOut(Base):
    __tablename__ = "events_out"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"))
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class CouncilRun(Base):
    __tablename__ = "council_runs"
    __table_args__ = (
        CheckConstraint("level in ('single','council')", name="ck_council_runs_level"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    point: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[int | None] = mapped_column(BigInteger)
    author_family: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    members: Mapped[dict] = mapped_column(JSONB, nullable=False)
    verdict: Mapped[str | None] = mapped_column(Text)
    groups: Mapped[dict | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    started_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)


class CouncilItem(Base):
    __tablename__ = "council_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("council_runs.id"))
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class CollectionBatch(Base):
    __tablename__ = "collection_batches"
    __table_args__ = (
        CheckConstraint(
            "source_mode in ('fixture','manual','live','live_2gis','browser_2gis','browser_yandex','browser_google')",
            name="ck_collection_batches_source_mode",
        ),
        CheckConstraint(
            "status in ('running','paused','stopped','completed','interrupted','awaiting_manual_verification')",
            name="ck_collection_batches_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    platforms: Mapped[list] = mapped_column(JSONB, nullable=False)
    item_limit: Mapped[int | None] = mapped_column(Integer)
    source_mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'running'"))
    out_path: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_path: Mapped[str] = mapped_column(Text, nullable=False)
    fixtures_dir: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)


class CollectionBatchItem(Base):
    __tablename__ = "collection_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "id_2gis", name="uq_collection_batch_items_batch_company"),
        CheckConstraint(
            "status in ('pending','in_progress','completed','failed','blocked')",
            name="ck_collection_batch_items_status",
        ),
        Index("ix_collection_batch_items_batch_id", "batch_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("collection_batches.id"), nullable=False)
    id_2gis: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"))
    updated_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class ReviewQuality(Base):
    __tablename__ = "review_quality"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("council_runs.id"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    agreed: Mapped[bool | None] = mapped_column(Boolean)
    overridden_items: Mapped[dict | None] = mapped_column(JSONB)
    missed: Mapped[dict | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class ListingImprovementPlan(Base):
    __tablename__ = "listing_improvement_plans"
    __table_args__ = (
        UniqueConstraint("company_id", "source_key", name="uq_listing_improvement_plans_company_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    audit_id: Mapped[int | None] = mapped_column(ForeignKey("audits.id"))
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class ListingApproval(Base):
    __tablename__ = "listing_approvals"
    __table_args__ = (
        CheckConstraint("platform in ('2gis','yandex','google')", name="ck_listing_approvals_platform"),
        CheckConstraint("operation in ('create','update')", name="ck_listing_approvals_operation"),
        CheckConstraint("status in ('active','invalidated','expired')", name="ck_listing_approvals_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    package_id: Mapped[int] = mapped_column(ForeignKey("change_packages.id"), nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'active'"))
    approved_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)


class ListingSubmission(Base):
    __tablename__ = "listing_submissions"
    __table_args__ = (
        CheckConstraint("platform in ('2gis','yandex','google')", name="ck_listing_submissions_platform"),
        CheckConstraint("operation in ('create','update')", name="ck_listing_submissions_operation"),
        CheckConstraint(
            "status in ('prepared','awaiting_approval','approved','submitting','submitted',"
            "'moderation_pending','published','rejected','uncertain','human_required')",
            name="ck_listing_submissions_status",
        ),
        UniqueConstraint(
            "order_id",
            "package_id",
            "platform",
            "operation",
            name="uq_listing_submissions_target",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    package_id: Mapped[int] = mapped_column(ForeignKey("change_packages.id"), nullable=False)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("listing_approvals.id"))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmation: Mapped[dict | None] = mapped_column(JSONB)
    screenshot_before: Mapped[str | None] = mapped_column(Text)
    screenshot_after: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(TZ)
    moderation_status: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class ListingSubmissionAttempt(Base):
    __tablename__ = "listing_submission_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("listing_submissions.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    clicked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    confirmation: Mapped[str | None] = mapped_column(Text)
    screenshot_before: Mapped[str | None] = mapped_column(Text)
    screenshot_after: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime | None] = mapped_column(TZ, server_default=NOW)


class CompanyMatchKey(Base):
    """Search keys for identity lookup. They do not decide which company owns a card."""

    __tablename__ = "company_match_keys"
    __table_args__ = (
        CheckConstraint("kind in ('name','phone')", name="ck_company_match_keys_kind"),
        CheckConstraint("platform in ('','2gis','yandex','google')", name="ck_company_match_keys_platform"),
        UniqueConstraint("company_id", "platform", "kind", "value", name="uq_company_match_keys"),
        Index("ix_company_match_keys_lookup", "city", "kind", "platform", "value"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("''"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)


def _install_match_index() -> None:
    from zhol.agents.scout.match_index import install

    install()


_install_match_index()
