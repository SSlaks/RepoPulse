import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.models import Base, Repository
from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "g7b8c9d0e1f2"
FROZEN_REVISION = "h8c9d0e1f2a3"

INSERT_REPOSITORY = (
    "INSERT INTO repositories "
    "(github_id, full_name, owner, name, description, html_url, language, topics, "
    "license_name, stars_count, forks_count, open_issues_count, is_fork, archived, "
    "disabled, first_tracked_at, last_seen_at, history_available_from) "
    "VALUES (:github_id, :full_name, :owner, :name, :description, :html_url, :language, "
    ":topics, NULL, 0, 0, 0, false, false, false, :timestamp, :timestamp, :timestamp)"
)


def _migration_config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("SYNC_DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


def _insert_row(engine, **values) -> None:
    metadata = MetaData()
    repositories = Table("repositories", metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(repositories.insert().values(**values))


def test_migration_backfills_normalized_search_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "search-backfill.sqlite3"
    config = _migration_config(database_path, monkeypatch)
    timestamp = datetime(2026, 9, 16, tzinfo=UTC)

    try:
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            unicode_topics = ["AI", "greek-Σ", "ß", "E\u0301"]
            _insert_row(
                engine,
                github_id=1,
                full_name="Owner/Repo",
                owner="Owner",
                name="Repo",
                description="100%_\\ fastapi/fastapi 现代",
                html_url="https://github.com/Owner/Repo",
                language="İstanbul",
                topics=unicode_topics,
                stars_count=0,
                forks_count=0,
                open_issues_count=0,
                is_fork=False,
                archived=False,
                disabled=False,
                first_tracked_at=timestamp,
                last_seen_at=timestamp,
                history_available_from=timestamp,
            )
            _insert_row(
                engine,
                github_id=2,
                full_name="No/Lang",
                owner="No",
                name="Lang",
                description=None,
                html_url="https://github.com/No/Lang",
                language=None,
                topics=[],
                stars_count=0,
                forks_count=0,
                open_issues_count=0,
                is_fork=False,
                archived=False,
                disabled=False,
                first_tracked_at=timestamp,
                last_seen_at=timestamp,
                history_available_from=timestamp,
            )
        finally:
            engine.dispose()

        command.upgrade(config, "head")

        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            columns = {column["name"]: column for column in inspect(engine).get_columns("repositories")}
            assert columns["language_lower"]["nullable"] is False
            assert columns["topics_lower_keys"]["nullable"] is False
            assert columns["search_text_lower"]["nullable"] is False
            with engine.connect() as connection:
                first = connection.execute(
                    text(
                        "SELECT language_lower, topics_lower_keys, search_text_lower "
                        "FROM repositories WHERE github_id = 1"
                    )
                ).one()
                second = connection.execute(
                    text(
                        "SELECT language_lower, topics_lower_keys, search_text_lower "
                        "FROM repositories WHERE github_id = 2"
                    )
                ).one()
        finally:
            engine.dispose()

        expected_topics = [json.dumps(topic.lower(), ensure_ascii=True) for topic in unicode_topics]
        assert first.language_lower == "İstanbul".lower()
        assert json.loads(first.topics_lower_keys) == expected_topics
        assert first.search_text_lower == "Owner/Repo 100%_\\ fastapi/fastapi 现代".lower()
        assert second.language_lower == ""
        assert json.loads(second.topics_lower_keys) == []
        assert second.search_text_lower == "no/lang "
    finally:
        get_settings.cache_clear()


def test_migration_backfills_in_primary_key_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "search-batches.sqlite3"
    config = _migration_config(database_path, monkeypatch)
    timestamp = datetime(2026, 9, 16, tzinfo=UTC)

    try:
        command.upgrade(config, PREVIOUS_REVISION)
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            metadata = MetaData()
            repositories = Table("repositories", metadata, autoload_with=engine)
            with engine.begin() as connection:
                connection.execute(
                    repositories.insert(),
                    [
                        {
                            "github_id": index,
                            "full_name": f"owner/repo-{index}",
                            "owner": "owner",
                            "name": f"repo-{index}",
                            "description": None,
                            "html_url": f"https://github.com/owner/repo-{index}",
                            "language": "Go",
                            "topics": [f"topic-{index}"],
                            "stars_count": 0,
                            "forks_count": 0,
                            "open_issues_count": 0,
                            "is_fork": False,
                            "archived": False,
                            "disabled": False,
                            "first_tracked_at": timestamp,
                            "last_seen_at": timestamp,
                            "history_available_from": timestamp,
                        }
                        for index in range(1, 1201)
                    ],
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")

        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            with engine.connect() as connection:
                missing = connection.scalar(
                    text(
                        "SELECT count(*) FROM repositories WHERE language_lower IS NULL "
                        "OR topics_lower_keys IS NULL OR search_text_lower IS NULL"
                    )
                )
                total = connection.scalar(text("SELECT count(*) FROM repositories"))
                sample = connection.scalar(
                    text("SELECT search_text_lower FROM repositories WHERE github_id = 1199")
                )
        finally:
            engine.dispose()

        assert total == 1200
        assert missing == 0
        assert sample == "owner/repo-1199 "
    finally:
        get_settings.cache_clear()


def test_migration_downgrade_removes_search_fields_and_keeps_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "search-downgrade.sqlite3"
    config = _migration_config(database_path, monkeypatch)

    try:
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            metadata = MetaData()
            repositories = Table("repositories", metadata, autoload_with=engine)
            with engine.begin() as connection:
                connection.execute(
                    repositories.insert().values(
                        github_id=77,
                        full_name="owner/kept",
                        owner="owner",
                        name="kept",
                        description="keep me",
                        html_url="https://github.com/owner/kept",
                        language="Go",
                        language_lower="go",
                        topics=["ai"],
                        topics_lower_keys=['"ai"'],
                        search_text_lower="owner/kept keep me",
                        stars_count=0,
                        forks_count=0,
                        open_issues_count=0,
                        is_fork=False,
                        archived=False,
                        disabled=False,
                        first_tracked_at=datetime(2026, 9, 16, tzinfo=UTC),
                        last_seen_at=datetime(2026, 9, 16, tzinfo=UTC),
                        history_available_from=datetime(2026, 9, 16, tzinfo=UTC),
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(config, PREVIOUS_REVISION)

        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        try:
            columns = {column["name"] for column in inspect(engine).get_columns("repositories")}
            assert columns & {"language_lower", "topics_lower_keys", "search_text_lower"} == set()
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT full_name, language, description FROM repositories "
                        "WHERE github_id = 77"
                    )
                ).one()
        finally:
            engine.dispose()

        assert row == ("owner/kept", "Go", "keep me")
    finally:
        get_settings.cache_clear()


def test_orm_events_normalize_on_insert_and_update(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'normalize.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    timestamp = datetime(2026, 9, 16, tzinfo=UTC)
    try:
        with Session(engine) as session:
            repository = Repository(
                github_id=5,
                full_name="Owner/Repo",
                owner="Owner",
                name="Repo",
                description="First 100%_\\",
                html_url="https://github.com/Owner/Repo",
                language="İstanbul",
                topics=["AI", "greek-Σ"],
                first_tracked_at=timestamp,
                last_seen_at=timestamp,
                history_available_from=timestamp,
            )
            session.add(repository)
            session.commit()
            assert repository.language_lower == "İstanbul".lower()
            assert repository.topics_lower_keys == [
                json.dumps(topic.lower(), ensure_ascii=True) for topic in ["AI", "greek-Σ"]
            ]
            assert repository.search_text_lower == "owner/repo first 100%_\\"

            repository.description = None
            repository.topics = []
            repository.language = None
            session.commit()
            assert repository.language_lower == ""
            assert repository.topics_lower_keys == []
            assert repository.search_text_lower == "owner/repo "

        with Session(engine) as session:
            defaulted = Repository(
                github_id=6,
                full_name="Omitt/Topics",
                owner="Omitt",
                name="Topics",
                html_url="https://github.com/Omitt/Topics",
                first_tracked_at=timestamp,
                last_seen_at=timestamp,
                history_available_from=timestamp,
            )
            session.add(defaulted)
            session.commit()
            assert defaulted.topics_lower_keys == []
            assert defaulted.search_text_lower == "omitt/topics "
    finally:
        engine.dispose()
