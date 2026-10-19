"""listing submission approvals and attempts

Revision ID: e77b4d8c1a02
Revises: d9e75b1c4a20
Create Date: 2026-10-12 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e77b4d8c1a02"
down_revision: Union[str, Sequence[str], None] = "d9e75b1c4a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listing_approvals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("package_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("platform in ('2gis','yandex','google')", name="ck_listing_approvals_platform"),
        sa.CheckConstraint("operation in ('create','update')", name="ck_listing_approvals_operation"),
        sa.CheckConstraint(
            "status in ('active','invalidated','expired')",
            name="ck_listing_approvals_status",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["package_id"], ["change_packages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "listing_submissions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("package_id", sa.BigInteger(), nullable=False),
        sa.Column("approval_id", sa.BigInteger(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("screenshot_before", sa.Text(), nullable=True),
        sa.Column("screenshot_after", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moderation_status", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint("platform in ('2gis','yandex','google')", name="ck_listing_submissions_platform"),
        sa.CheckConstraint("operation in ('create','update')", name="ck_listing_submissions_operation"),
        sa.CheckConstraint(
            "status in ('prepared','awaiting_approval','approved','submitting','submitted',"
            "'moderation_pending','published','rejected','uncertain','human_required')",
            name="ck_listing_submissions_status",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["package_id"], ["change_packages.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["listing_approvals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "order_id",
            "package_id",
            "platform",
            "operation",
            name="uq_listing_submissions_target",
        ),
    )
    op.create_table(
        "listing_submission_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("submission_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("clicked", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("confirmation", sa.Text(), nullable=True),
        sa.Column("screenshot_before", sa.Text(), nullable=True),
        sa.Column("screenshot_after", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["submission_id"], ["listing_submissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("listing_submission_attempts")
    op.drop_table("listing_submissions")
    op.drop_table("listing_approvals")
