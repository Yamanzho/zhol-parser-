"""allow yandex and google browser batch modes

Revision ID: e5a9b3c20f41
Revises: d4f8a2c19e30
Create Date: 2026-10-18 09:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a9b3c20f41"
down_revision: Union[str, Sequence[str], None] = "d4f8a2c19e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODES = (
    "fixture",
    "manual",
    "live",
    "live_2gis",
    "browser_2gis",
    "browser_yandex",
    "browser_google",
)
_PREVIOUS = ("fixture", "manual", "live", "live_2gis", "browser_2gis")


def upgrade() -> None:
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in (" + ", ".join(f"'{item}'" for item in _MODES) + ")",
    )


def downgrade() -> None:
    bind = op.get_bind()
    waiting = bind.execute(
        sa.text(
            "SELECT count(*) FROM collection_batches "
            "WHERE source_mode in ('browser_yandex', 'browser_google')"
        )
    ).scalar_one()
    if waiting:
        raise RuntimeError(
            "downgrade stopped: "
            f"{waiting} batches use browser_yandex or browser_google. "
            "No company, snapshot, or batch row was changed."
        )
    op.drop_constraint("ck_collection_batches_source_mode", "collection_batches", type_="check")
    op.create_check_constraint(
        "ck_collection_batches_source_mode",
        "collection_batches",
        "source_mode in (" + ", ".join(f"'{item}'" for item in _PREVIOUS) + ")",
    )
