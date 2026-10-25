"""Search keys and indexes for company lookup. Company rows are not rewritten.

Revision ID: b6d2f4a81c30
Revises: e5a9b3c20f41
Create Date: 2026-10-25 22:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6d2f4a81c30"
down_revision: Union[str, Sequence[str], None] = "e5a9b3c20f41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_companies_city_lower", "companies", [sa.text("lower(city)")])
    op.create_index(
        "ix_card_snapshots_latest",
        "card_snapshots",
        ["company_id", "platform", sa.text("collected_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_jobs_ready",
        "jobs",
        ["queue", "run_after", "id"],
        postgresql_where=sa.text("status IN ('queued', 'failed')"),
    )
    op.create_index(
        "ix_jobs_running_lease",
        "jobs",
        ["run_after", "id"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_collection_batch_items_batch_id", "collection_batch_items", ["batch_id", "id"])
    op.create_table(
        "company_match_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("company_id", sa.BigInteger(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), server_default="", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.CheckConstraint("kind in ('name','phone')", name="ck_company_match_keys_kind"),
        sa.CheckConstraint("platform in ('','2gis','yandex','google')", name="ck_company_match_keys_platform"),
        sa.UniqueConstraint("company_id", "platform", "kind", "value", name="uq_company_match_keys"),
    )
    op.create_index(
        "ix_company_match_keys_lookup",
        "company_match_keys",
        ["city", "kind", "platform", "value"],
    )
    from zhol.agents.scout.match_index import rebuild_match_keys

    rebuild_match_keys(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_company_match_keys_lookup", table_name="company_match_keys")
    op.drop_table("company_match_keys")
    op.drop_index("ix_collection_batch_items_batch_id", table_name="collection_batch_items")
    op.drop_index("ix_jobs_running_lease", table_name="jobs")
    op.drop_index("ix_jobs_ready", table_name="jobs")
    op.drop_index("ix_card_snapshots_latest", table_name="card_snapshots")
    op.drop_index("ix_companies_city_lower", table_name="companies")
