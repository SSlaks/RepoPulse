"""Repository availability and durable daily request ledger."""

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("availability_status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("consecutive_not_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_not_found_at", sa.DateTime(timezone=True)),
        sa.Column("last_not_found_date", sa.Date()),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("next_probe_at", sa.DateTime(timezone=True)),
        sa.Column("probe_backoff_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(40)),
    ):
        op.add_column("repositories", column)
    op.create_index("ix_repositories_next_probe_at", "repositories", ["next_probe_at"])
    op.create_table(
        "snapshot_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_run_id", sa.Integer(), sa.ForeignKey("job_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("repository_id", sa.Integer(), sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(40)),
        sa.Column("error_message", sa.String(2000)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("availability_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("job_run_id", "repository_id", name="uq_snapshot_request_repo"),
    )


def downgrade() -> None:
    op.drop_table("snapshot_requests")
    op.drop_index("ix_repositories_next_probe_at", table_name="repositories")
    for name in ("last_error_code", "probe_backoff_step", "next_probe_at", "last_success_at",
                 "last_checked_at", "last_not_found_date", "first_not_found_at",
                 "consecutive_not_found", "availability_status"):
        op.drop_column("repositories", name)
