"""Persist normalized repository search fields for SQL-side filtering.

The normalized columns are derived from ``full_name``/``description``/
``language``/``topics`` using the exact Python ``lower()`` semantics frozen
below.  Introducing ``casefold``, ``strip`` or Unicode normalization here would
silently change which repositories match a filter, so keep this logic in sync
with ``app.models._refresh_normalized_search_fields`` by editing both together.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "h8c9d0e1f2a3"
down_revision = "g7b8c9d0e1f2"
branch_labels = None
depends_on = None

BATCH_SIZE = 500


def _normalized_language(language: str | None) -> str:
    return (language or "").lower()


def _normalized_topics(topics: list[str] | None) -> list[str]:
    return [json.dumps(topic.lower(), ensure_ascii=True) for topic in (topics or [])]


def _normalized_search_text(full_name: str, description: str | None) -> str:
    return f"{full_name} {description or ''}".lower()


_repositories = sa.table(
    "repositories",
    sa.column("id", sa.Integer),
    sa.column("full_name", sa.String),
    sa.column("description", sa.Text),
    sa.column("language", sa.String),
    sa.column("topics", sa.JSON),
    sa.column("language_lower", sa.Text),
    sa.column("topics_lower_keys", sa.JSON),
    sa.column("search_text_lower", sa.Text),
)


def upgrade() -> None:
    op.add_column("repositories", sa.Column("language_lower", sa.Text(), nullable=True))
    op.add_column("repositories", sa.Column("topics_lower_keys", sa.JSON(), nullable=True))
    op.add_column("repositories", sa.Column("search_text_lower", sa.Text(), nullable=True))
    _backfill_search_fields()
    with op.batch_alter_table("repositories") as batch:
        batch.alter_column("language_lower", existing_type=sa.Text(), nullable=False)
        batch.alter_column("topics_lower_keys", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("search_text_lower", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("search_text_lower")
        batch.drop_column("topics_lower_keys")
        batch.drop_column("language_lower")


def _backfill_search_fields() -> None:
    connection = op.get_bind()
    last_id = 0
    while True:
        rows = connection.execute(
            sa.select(
                _repositories.c.id,
                _repositories.c.full_name,
                _repositories.c.description,
                _repositories.c.language,
                _repositories.c.topics,
            )
            .where(_repositories.c.id > last_id)
            .order_by(_repositories.c.id)
            .limit(BATCH_SIZE)
        ).all()
        if not rows:
            return
        for row in rows:
            connection.execute(
                _repositories.update()
                .where(_repositories.c.id == row.id)
                .values(
                    language_lower=_normalized_language(row.language),
                    topics_lower_keys=_normalized_topics(row.topics),
                    search_text_lower=_normalized_search_text(row.full_name, row.description),
                )
            )
        last_id = rows[-1].id
