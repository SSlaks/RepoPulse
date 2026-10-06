import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.database import async_engine, async_session_factory
from app.models import Base, RankingItem, RankingRun, Repository
from app.ranking.filters import RankingFilters
from app.repositories.catalog import CatalogRepository
from app.schemas import PeriodDays, RankingResponse
from app.services.catalog import CatalogService
from psycopg import sql as psycopg_sql
from sqlalchemy import create_engine, event, inspect, make_url, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_infrastructure_integration import (
    _assert_disposable_postgres,
    _psycopg_dsn,
    _required_test_url,
    _test_namespace,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@dataclass(frozen=True)
class _Spec:
    full_name: str
    language: str | None
    topics: tuple[str, ...]
    description: str | None
    end_stars: int
    stars_count: int


SPECIAL_SPECS = (
    _Spec("owner/alpha", "Python", ("AI", "ai"), "现代 100%_ spaced", 300, 9999),
    _Spec("owner/beta", "python", ("greek-Σ",), None, 200, 200),
    _Spec("owner/gamma", "İstanbul", ("dup", "DUP"), "decomposed e\u0301 only", 150, 150),
    _Spec("owner/delta", None, (), "plain", 100, 100),
    _Spec("owner/epsilon", "Go", ("ß",), "fastapi/fastapi 现代", 50, 50),
    _Spec("owner/zeta", "GO", ("ß",), None, 25, 25),
    _Spec("owner/eta", "C++", ("%special",), "back\\slash", 10, 10),
)
BULK_SPECS = tuple(
    _Spec(
        f"owner/bulk-{index}",
        "Rust",
        (f"bulk-{index}",),
        f"bulk repository {index}",
        100 - index,
        100 - index,
    )
    for index in range(8, 31)
)
ALL_SPECS = SPECIAL_SPECS + BULK_SPECS
PERIOD = PeriodDays.SEVEN


async def _seed_dataset(session_factory: async_sessionmaker[AsyncSession]) -> int:
    timestamp = datetime(2026, 9, 16, tzinfo=UTC)
    async with session_factory() as session:
        repositories: list[Repository] = []
        for spec in ALL_SPECS:
            owner, name = spec.full_name.split("/")
            repositories.append(
                Repository(
                    github_id=len(repositories) + 1,
                    full_name=spec.full_name,
                    owner=owner,
                    name=name,
                    description=spec.description,
                    html_url=f"https://github.com/{spec.full_name}",
                    language=spec.language,
                    topics=list(spec.topics),
                    stars_count=spec.stars_count,
                    first_tracked_at=timestamp,
                    last_seen_at=timestamp,
                    history_available_from=timestamp,
                )
            )
        session.add_all(repositories)
        await session.flush()
        run = RankingRun(
            period_days=int(PERIOD),
            as_of=timestamp,
            baseline_at=timestamp,
            config_version="test-v1",
            status="ready",
            published_at=timestamp,
        )
        session.add(run)
        await session.flush()
        session.add_all(
            [
                RankingItem(
                    ranking_run_id=run.id,
                    repository_id=repository.id,
                    rank=rank,
                    previous_rank=rank,
                    start_stars=max(0, spec.end_stars - 5),
                    end_stars=spec.end_stars,
                    net_delta=5,
                    growth_rate=0.1,
                    baseline_available=True,
                )
                for rank, (repository, spec) in enumerate(
                    zip(repositories, ALL_SPECS, strict=True), start=1
                )
            ]
        )
        await session.commit()
        return run.id


async def _service_rankings(
    session: AsyncSession,
    *,
    language: str | None = None,
    topic: str | None = None,
    min_stars: int = 0,
    query: str | None = None,
    page: int = 1,
    limit: int = 15,
) -> RankingResponse:
    return await CatalogService(session).rankings(
        period=PERIOD,
        language=language,
        topic=topic,
        min_stars=min_stars,
        query=query,
        page=page,
        limit=limit,
    )


async def _rankings(
    *,
    language: str | None = None,
    topic: str | None = None,
    min_stars: int = 0,
    query: str | None = None,
    page: int = 1,
    limit: int = 15,
) -> RankingResponse:
    async with async_session_factory() as session:
        return await _service_rankings(
            session,
            language=language,
            topic=topic,
            min_stars=min_stars,
            query=query,
            page=page,
            limit=limit,
        )


@pytest_asyncio.fixture
async def sqlite_ranking_dataset() -> AsyncIterator[None]:
    await async_engine.dispose()
    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await _seed_dataset(async_session_factory)
    await async_engine.dispose()
    yield


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_no_filter_pages_preserve_original_rank_and_total() -> None:
    first = await _rankings()
    second = await _rankings(page=2)
    overflow = await _rankings(page=3)

    assert first.meta.total == len(ALL_SPECS) == 30
    assert [item.rank for item in first.data] == list(range(1, 16))
    assert [item.full_name for item in first.data] == [spec.full_name for spec in ALL_SPECS[:15]]
    assert second.meta.total == 30
    assert [item.rank for item in second.data] == list(range(16, 31))
    assert overflow.meta.total == 30
    assert overflow.data == []


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_empty_result_reports_zero_total() -> None:
    response = await _rankings(query="zzz-no-such-repository")
    assert response.meta.total == 0
    assert response.data == []


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_language_matching_is_exact_and_case_insensitive() -> None:
    assert (await _rankings(language="python")).meta.total == 2
    assert (await _rankings(language="PYTHON")).meta.total == 2
    assert (await _rankings(language="go")).meta.total == 2
    assert (await _rankings(language="İstanbul")).meta.total == 1
    assert (await _rankings(language="i\u0307stanbul")).meta.total == 1
    assert (await _rankings(language="istanbul")).meta.total == 0


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_topic_matching_counts_duplicates_once() -> None:
    assert (await _rankings(topic="AI")).meta.total == 1
    assert (await _rankings(topic="ai")).meta.total == 1
    assert (await _rankings(topic="DUP")).meta.total == 1
    assert (await _rankings(topic="ß")).meta.total == 2
    assert (await _rankings(topic="greek-Σ")).meta.total == 1
    assert (await _rankings(topic="greek-σ")).meta.total == 1
    assert (await _rankings(topic="greek-ς")).meta.total == 0
    alpha = await _rankings(topic="AI")
    assert [item.full_name for item in alpha.data] == ["owner/alpha"]


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_search_keeps_literal_wildcards_and_spans_separators() -> None:
    assert (await _rankings(query="%")).meta.total == 1
    assert (await _rankings(query="_")).meta.total == 1
    assert (await _rankings(query="\\")).meta.total == 1
    assert (await _rankings(query="owner/epsilon fastapi/fastapi")).meta.total == 1
    assert (await _rankings(query="现代")).meta.total == 2
    assert (await _rankings(query="owner/beta")).meta.total == 1


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_unicode_normalization_is_not_applied() -> None:
    assert (await _rankings(query="e\u0301")).meta.total == 1
    assert (await _rankings(query="é")).meta.total == 0


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_nul_arguments_return_empty_without_error() -> None:
    assert (await _rankings(language="a\x00b")).meta.total == 0
    assert (await _rankings(query="a\x00b")).meta.total == 0


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_minimum_stars_uses_published_end_stars() -> None:
    response = await _rankings(min_stars=250)
    assert response.meta.total == 1
    assert response.data[0].full_name == "owner/alpha"
    assert response.data[0].total_stars == 300
    assert (await _rankings(min_stars=500)).meta.total == 0


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_combined_filters_narrow_the_result() -> None:
    response = await _rankings(
        language="Rust", topic="bulk-8", min_stars=90, query="bulk repository 8"
    )
    assert response.meta.total == 1
    assert response.data[0].full_name == "owner/bulk-8"


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_only_current_page_entities_are_loaded() -> None:
    loaded: list[str] = []

    def _record(_target: object, _context: object) -> None:
        loaded.append("loaded")

    event.listen(RankingItem, "load", _record)
    event.listen(Repository, "load", _record)
    try:
        response = await _rankings(page=2, limit=5)
    finally:
        event.remove(RankingItem, "load", _record)
        event.remove(Repository, "load", _record)

    assert len(response.data) == 5
    assert response.meta.total == 30
    assert loaded.count("loaded") == 10


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_repository_ranking_page_returns_shared_total_for_overflow() -> None:
    async with async_session_factory() as session:
        repository = CatalogRepository(session)
        async with repository.ranking_read():
            total, rows = await repository.ranking_page(1, RankingFilters(), page=4, limit=15)
    assert total == 30
    assert rows == []


async def _update_repository(full_name: str, **values: object) -> None:
    async with async_session_factory() as session:
        repository = await session.scalar(
            select(Repository).where(Repository.full_name == full_name)
        )
        assert repository is not None
        for key, value in values.items():
            setattr(repository, key, value)
        await session.commit()


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_negative_persisted_stars_are_excluded_at_default_minimum() -> None:
    async with async_session_factory() as session:
        item = await session.scalar(select(RankingItem).where(RankingItem.rank == 30))
        assert item is not None
        item.end_stars = -5
        await session.commit()

    response = await _rankings(min_stars=0)
    assert response.meta.total == 29
    assert all(item.total_stars >= 0 for item in response.data)


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_extreme_page_and_min_stars_do_not_overflow_binds() -> None:
    huge = 10**30
    overflow = await _rankings(page=huge)
    assert overflow.meta.total == 30
    assert overflow.data == []

    minimum = await _rankings(min_stars=huge)
    assert minimum.meta.total == 0
    assert minimum.data == []


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_sqlite_nul_values_can_match() -> None:
    await _update_repository("owner/bulk-8", language="a\x00b", description="nul\x00desc")

    language = await _rankings(language="a\x00b")
    assert language.meta.total == 1
    assert language.data[0].full_name == "owner/bulk-8"

    search = await _rankings(query="nul\x00desc")
    assert search.meta.total == 1
    assert search.data[0].full_name == "owner/bulk-8"


@pytest.mark.usefixtures("sqlite_ranking_dataset")
async def test_overflow_page_loads_no_entities() -> None:
    loaded: list[str] = []

    def _record(_target: object, _context: object) -> None:
        loaded.append("loaded")

    event.listen(RankingItem, "load", _record)
    event.listen(Repository, "load", _record)
    try:
        response = await _rankings(page=10**30)
    finally:
        event.remove(RankingItem, "load", _record)
        event.remove(Repository, "load", _record)

    assert response.meta.total == 30
    assert response.data == []
    assert loaded == []


@pytest.fixture
def postgres_ranking_url() -> Iterator[str]:
    admin_url = _required_test_url("TEST_POSTGRES_ADMIN_URL")
    _assert_disposable_postgres(admin_url)
    database_name = f"repopulse_ranking_{_test_namespace()}"[:63]
    admin_dsn = _psycopg_dsn(admin_url)
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            psycopg_sql.SQL("CREATE DATABASE {}").format(psycopg_sql.Identifier(database_name))
        )
    database_url = admin_url.set(database=database_name)
    try:
        yield database_url.render_as_string(hide_password=False)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                psycopg_sql.SQL("DROP DATABASE {}").format(psycopg_sql.Identifier(database_name))
            )


@pytest.mark.integration
async def test_postgres_filter_semantics(
    postgres_ranking_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_DATABASE_URL", postgres_ranking_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    command.upgrade(config, "head")
    async_url = (
        make_url(postgres_ranking_url)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(async_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _seed_dataset(factory)
        async with factory() as session:
            assert (await _service_rankings(session, language="İstanbul")).meta.total == 1
            assert (await _service_rankings(session, language="istanbul")).meta.total == 0
            assert (await _service_rankings(session, topic="AI")).meta.total == 1
            assert (await _service_rankings(session, topic="greek-Σ")).meta.total == 1
            assert (await _service_rankings(session, topic="greek-ς")).meta.total == 0
            assert (await _service_rankings(session, query="%")).meta.total == 1
            assert (await _service_rankings(session, query="\\")).meta.total == 1
            assert (
                await _service_rankings(session, query="owner/epsilon fastapi/fastapi")
            ).meta.total == 1
            assert (await _service_rankings(session, query="é")).meta.total == 0
            assert (await _service_rankings(session, language="a\x00b")).meta.total == 0
            assert (await _service_rankings(session, query="a\x00b")).meta.total == 0
            published = await _service_rankings(session, min_stars=250)
            assert published.meta.total == 1
            assert published.data[0].total_stars == 300
            overflow = await _service_rankings(session, page=3)
            assert overflow.meta.total == 30
            assert overflow.data == []
    finally:
        await engine.dispose()
        get_settings.cache_clear()


@pytest.mark.integration
async def test_postgres_ranking_page_reads_one_snapshot(
    postgres_ranking_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_DATABASE_URL", postgres_ranking_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    command.upgrade(config, "head")
    async_url = (
        make_url(postgres_ranking_url)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )
    dsn = (
        make_url(postgres_ranking_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(async_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ranking_statements: list[str] = []

    def _barrier(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "filtered_ranking" not in statement:
            return
        ranking_statements.append(statement)
        if len(ranking_statements) == 1:
            with psycopg.connect(dsn, autocommit=True) as concurrent:
                concurrent.execute(
                    "UPDATE repositories SET language = 'Rust', language_lower = 'rust' "
                    "WHERE full_name = 'owner/alpha'"
                )

    try:
        await _seed_dataset(factory)
        event.listen(engine.sync_engine, "after_cursor_execute", _barrier)
        try:
            async with factory() as session:
                response = await _service_rankings(session, language="Python")
        finally:
            event.remove(engine.sync_engine, "after_cursor_execute", _barrier)

        assert len(ranking_statements) == 1
        assert response.meta.total == 2
        alpha = [item for item in response.data if item.full_name == "owner/alpha"]
        assert len(alpha) == 1
        assert alpha[0].language == "Python"
    finally:
        await engine.dispose()
        get_settings.cache_clear()


@pytest.mark.integration
def test_postgres_search_field_migration_backfills_and_reverts(
    postgres_ranking_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_DATABASE_URL", postgres_ranking_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    try:
        command.upgrade(config, "g7b8c9d0e1f2")
        engine = create_engine(postgres_ranking_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO repositories (github_id, full_name, owner, name, description, "
                        "html_url, language, topics, stars_count, forks_count, open_issues_count, "
                        "is_fork, archived, disabled, first_tracked_at, last_seen_at, "
                        "history_available_from) VALUES (1, 'Owner/Repo', 'Owner', 'Repo', "
                        "'Desc 100%_\\ 现代', 'http://x', 'İstanbul', CAST(:topics AS JSON), "
                        "0, 0, 0, false, false, false, now(), now(), now())"
                    ),
                    {"topics": '["AI", "greek-Σ"]'},
                )
        finally:
            engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(postgres_ranking_url)
        try:
            columns = {column["name"]: column for column in inspect(engine).get_columns("repositories")}
            assert columns["language_lower"]["nullable"] is False
            assert columns["topics_lower_keys"]["nullable"] is False
            assert columns["search_text_lower"]["nullable"] is False
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT language_lower, topics_lower_keys, search_text_lower "
                        "FROM repositories WHERE github_id = 1"
                    )
                ).one()
        finally:
            engine.dispose()

        assert row.language_lower == "İstanbul".lower()
        assert row.topics_lower_keys == [
            json.dumps("ai", ensure_ascii=True),
            json.dumps("greek-σ", ensure_ascii=True),
        ]
        assert row.search_text_lower == "Owner/Repo Desc 100%_\\ 现代".lower()

        command.downgrade(config, "g7b8c9d0e1f2")
        engine = create_engine(postgres_ranking_url)
        try:
            columns = {column["name"] for column in inspect(engine).get_columns("repositories")}
            assert columns & {"language_lower", "topics_lower_keys", "search_text_lower"} == set()
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM repositories")) == 1
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
