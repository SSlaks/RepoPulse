"""Persist completeness and publication time with each ranking batch."""

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ranking_runs", sa.Column("collection_summary", sa.JSON(), nullable=True))
    op.add_column("ranking_runs", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ranking_runs", "published_at")
    op.drop_column("ranking_runs", "collection_summary")
