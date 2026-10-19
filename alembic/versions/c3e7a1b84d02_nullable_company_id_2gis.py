"""allow a company without a 2GIS id

Revision ID: c3e7a1b84d02
Revises: a10e5c4d6b21
Create Date: 2026-10-16 06:50:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e7a1b84d02"
down_revision: Union[str, Sequence[str], None] = "a10e5c4d6b21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("companies", "id_2gis", existing_type=sa.Text(), nullable=True)
    op.create_index(
        "uq_company_platform_ids_platform_external",
        "company_platform_ids",
        ["platform", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    missing = bind.execute(sa.text("SELECT count(*) FROM companies WHERE id_2gis IS NULL")).scalar_one()
    if missing:
        raise RuntimeError(
            "downgrade stopped: "
            f"{missing} companies have no 2GIS id, so companies.id_2gis cannot be restored to NOT NULL. "
            "No company, snapshot, audit, order, or platform id was changed, and no 2GIS id was invented."
        )
    op.drop_index("uq_company_platform_ids_platform_external", table_name="company_platform_ids")
    op.alter_column("companies", "id_2gis", existing_type=sa.Text(), nullable=False)
