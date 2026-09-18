from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config import get_settings
from app.models import Base, Repository
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRE_README_REVISION = "f6a7b8c9d0e1"


def _migration_config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("SYNC_DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


def test_empty_database_migrates_readme_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "readme-empty.sqlite3"
    config = _migration_config(database_path, monkeypatch)

    try:
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            readme_tables = {name for name in Base.metadata.tables if "readme" in name}
            assert readme_tables
            assert readme_tables <= set(inspect(engine).get_table_names())
            readme_columns = {column["name"] for column in inspect(engine).get_columns("repository_readmes")}
            assert {"content", "is_private", "visibility", "readme_etag", "root_etag"} <= readme_columns
            repository_columns = {column["name"] for column in inspect(engine).get_columns("repositories")}
            assert "is_private" not in repository_columns
            with engine.connect() as connection:
                version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == ScriptDirectory.from_config(config).get_current_head()
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()


def test_readme_migration_preserves_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "readme-upgrade.sqlite3"
    config = _migration_config(database_path, monkeypatch)

    try:
        command.upgrade(config, PRE_README_REVISION)
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            timestamp = datetime(2026, 9, 16, tzinfo=UTC)
            with Session(engine) as session:
                session.add(
                    Repository(
                        github_id=123,
                        full_name="owner/retained",
                        owner="owner",
                        name="retained",
                        html_url="https://github.com/owner/retained",
                        topics=[],
                        first_tracked_at=timestamp,
                        last_seen_at=timestamp,
                        history_available_from=timestamp,
                    )
                )
                session.commit()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO job_runs "
                        "(job_key, task_name, status, attempts, started_at, progress, "
                        "cancel_requested) "
                        "VALUES ('readme-migration-retention', 'probe', 'completed', 1, "
                        "'2026-09-16 00:00:00', '{}', false)"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT task_name, status, attempts FROM job_runs "
                        "WHERE job_key = 'readme-migration-retention'"
                    )
                ).one()
                repository = connection.execute(
                    text("SELECT github_id, full_name FROM repositories WHERE github_id = 123")
                ).one()
            assert row == ("probe", "completed", 1)
            assert repository == (123, "owner/retained")
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
