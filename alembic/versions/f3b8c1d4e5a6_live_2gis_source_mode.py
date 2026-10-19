"""allow live_2gis collection batches

Revision ID: f3b8c1d4e5a6
Revises: e77b4d8c1a02
Create Date: 2026-10-14 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "f3b8c1d4e5a6"
down_revision: Union[str, Sequence[str], None] = "e77b4d8c1a02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in ('fixture','manual','live','live_2gis')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in ('fixture','manual','live')",
    )
