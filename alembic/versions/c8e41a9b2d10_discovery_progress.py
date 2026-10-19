"""discovery progress and manual source mode

Revision ID: c8e41a9b2d10
Revises: b7e4c1a2d3f0
Create Date: 2026-10-11 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c8e41a9b2d10"
down_revision: Union[str, Sequence[str], None] = "b7e4c1a2d3f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in ('fixture','manual','live')",
    )
    op.drop_constraint("ck_collection_batches_status", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_status",
        "collection_batches",
        "status in ('running','paused','stopped','completed','interrupted')",
    )
    op.add_column(
        "collection_batches",
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collection_batches", "progress")
    op.drop_constraint("ck_collection_batches_status", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_status",
        "collection_batches",
        "status in ('running','completed','interrupted')",
    )
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in ('fixture','live')",
    )
