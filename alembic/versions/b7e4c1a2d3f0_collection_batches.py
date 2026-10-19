"""collection batches

Revision ID: b7e4c1a2d3f0
Revises: 9c9c10b4e4ff
Create Date: 2026-10-11 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b7e4c1a2d3f0"
down_revision: Union[str, Sequence[str], None] = "9c9c10b4e4ff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collection_batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("platforms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("item_limit", sa.Integer(), nullable=True),
        sa.Column("source_mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("out_path", sa.Text(), nullable=False),
        sa.Column("catalog_path", sa.Text(), nullable=False),
        sa.Column("fixtures_dir", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source_mode in ('fixture','live')", name="ck_collection_batches_source_mode"),
        sa.CheckConstraint("status in ('running','completed','interrupted')", name="ck_collection_batches_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "collection_batch_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("id_2gis", sa.Text(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint(
            "status in ('pending','in_progress','completed','failed','blocked')",
            name="ck_collection_batch_items_status",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["collection_batches.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "id_2gis", name="uq_collection_batch_items_batch_company"),
    )


def downgrade() -> None:
    op.drop_table("collection_batch_items")
    op.drop_table("collection_batches")
