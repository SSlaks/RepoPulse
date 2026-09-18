"""Persist public README bodies and refresh validators."""

import sqlalchemy as sa
from alembic import op

revision = "g7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repository_readmes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "repository_id",
            sa.Integer(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text()),
        sa.Column("path", sa.String(length=500)),
        sa.Column("html_url", sa.String(length=500)),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("metadata_etag", sa.String(length=500)),
        sa.Column("last_public_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("readme_etag", sa.String(length=500)),
        sa.Column("readme_endpoint", sa.String(length=600)),
        sa.Column("root_etag", sa.String(length=500)),
        sa.Column("root_entries", sa.JSON()),
        sa.Column("root_checked_at", sa.DateTime(timezone=True)),
        sa.Column("root_selected_path", sa.String(length=500)),
        sa.UniqueConstraint("repository_id", name="uq_repository_readme_repository"),
    )
    op.create_index(
        "ix_repository_readmes_next_refresh_at",
        "repository_readmes",
        ["next_refresh_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_repository_readmes_next_refresh_at", table_name="repository_readmes")
    op.drop_table("repository_readmes")
