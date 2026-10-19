"""batch status for a manual browser verification

Revision ID: d4f8a2c19e30
Revises: c3e7a1b84d02
Create Date: 2026-10-17 22:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a2c19e30"
down_revision: Union[str, Sequence[str], None] = "c3e7a1b84d02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUSES = (
    "running",
    "paused",
    "stopped",
    "completed",
    "interrupted",
    "awaiting_manual_verification",
)
_PREVIOUS = ("running", "paused", "stopped", "completed", "interrupted")


def upgrade() -> None:
    op.drop_constraint("ck_collection_batches_status", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_status",
        "collection_batches",
        "status in (" + ", ".join(f"'{item}'" for item in _STATUSES) + ")",
    )


def downgrade() -> None:
    bind = op.get_bind()
    waiting = bind.execute(
        sa.text(
            "SELECT count(*) FROM collection_batches WHERE status = 'awaiting_manual_verification'"
        )
    ).scalar_one()
    if waiting:
        raise RuntimeError(
            "downgrade stopped: "
            f"{waiting} batches are awaiting manual verification. "
            "No company, snapshot, or batch row was changed."
        )
    op.drop_constraint("ck_collection_batches_status", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_status",
        "collection_batches",
        "status in (" + ", ".join(f"'{item}'" for item in _PREVIOUS) + ")",
    )
