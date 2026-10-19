"""listing improvement plans

Revision ID: d9e75b1c4a20
Revises: c8e41a9b2d10
Create Date: 2026-10-11 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d9e75b1c4a20"
down_revision: Union[str, Sequence[str], None] = "c8e41a9b2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listing_improvement_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("audit_id", sa.BigInteger(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["audit_id"], ["audits.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "source_key", name="uq_listing_improvement_plans_company_source"),
    )


def downgrade() -> None:
    op.drop_table("listing_improvement_plans")
