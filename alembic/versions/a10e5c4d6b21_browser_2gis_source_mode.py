"""allow browser_2gis collection batches

Revision ID: a10e5c4d6b21
Revises: f3b8c1d4e5a6
Create Date: 2026-10-15 01:40:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "a10e5c4d6b21"
down_revision: Union[str, Sequence[str], None] = "f3b8c1d4e5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in ('fixture','manual','live','live_2gis','browser_2gis')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in ('fixture','manual','live','live_2gis')",
    )
