import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.config import get_settings
from app.internal import limiter as limiter_module
from psycopg import sql
from redis import Redis as SyncRedis
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from worker.app import tasks

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_redis_lease_coordination_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    redis_url = _required_test_url("TEST_REDIS_URL")
    _assert_disposable_redis(redis_url)
    monkeypatch.setenv("REDIS_URL", redis_url.render_as_string(hide_password=False))
    get_settings.cache_clear()
    namespace = _test_namespace()
    original_keys = limiter_module._keys
    monkeypatch.setattr(
        limiter_module, "_keys",
        lambda policy, subject: tuple(f"{namespace}:{key}" for key in original_keys(policy, subject)),
    )
    first = limiter_module.RedisLimiter()
    second = limiter_module.RedisLimiter()
    lease = None
    try:
        lease = await first.acquire("ai_generate", "test-client")
        with pytest.raises(limiter_module.LeaseDenied):
            await second.acquire("ai_generate", "test-client")
        assert await second.renew(lease)
        assert await first.release(lease)
        lease = await second.acquire("ai_generate", "test-client")
        keys = limiter_module._keys(lease.policy, lease.subject_key)
        await first._client.zadd(keys[1], {lease.owner: 1})
        assert not await second.renew(lease)
        replacement = await first.acquire("ai_generate", "test-client")
        assert replacement.owner != lease.owner
        assert await first.release(replacement)
    finally:
        keys = [key async for key in first._client.scan_iter(match=f"{namespace}:*")]
        if keys:
            await first._client.delete(*keys)
        await first.close()
        await second.close()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_redis_unavailable_rejects_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    get_settings.cache_clear()
    coordinator = limiter_module.RedisLimiter()
    try:
        with pytest.raises(limiter_module.LimiterUnavailable):
            await coordinator.acquire("ai_generate", "test-client")
    finally:
        await coordinator.close()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_redis_failure_returns_http_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "integration-service-token")
    get_settings.cache_clear()
    coordinator = limiter_module.RedisLimiter()
    monkeypatch.setattr("app.api.routes.repositories.limiter", coordinator)
    monkeypatch.setattr("app.services.catalog.limiter", coordinator)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/internal/limits/acquire",
                headers={"X-Internal-Service-Token": "integration-service-token"},
                json={"policy": "ai_generate", "kind": "internal"},
            )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "5"
            assert response.headers["cache-control"] == "no-store"
            response = await client.get("/api/v1/repos/test/repo/readme")
            assert response.status_code == 503
            assert response.headers["retry-after"] == "5"
    finally:
        await coordinator.close()
        get_settings.cache_clear()

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SAFE_POSTGRES_HOSTS = {"127.0.0.1", "localhost"}
SAFE_REDIS_HOSTS = {"127.0.0.1", "localhost"}


def _test_namespace() -> str:
    supplied = os.environ.get("TEST_RUN_NAMESPACE", "local")
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", supplied)[:24]
    return f"{normalized}_{uuid4().hex[:10]}"


def _required_test_url(variable: str) -> URL:
    raw_url = os.environ.get(variable)
    if not raw_url:
        pytest.fail(f"{variable} is required when RUN_INTEGRATION_TESTS=1")
    return make_url(raw_url)


def _psycopg_dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _assert_disposable_postgres(url: URL) -> None:
    assert url.host in SAFE_POSTGRES_HOSTS, "PostgreSQL integration target must be loopback"
    assert url.username and "test" in url.username.lower(), "PostgreSQL user must be test-only"
    assert url.database == "postgres", "admin URL must target the disposable postgres database"


def _assert_disposable_redis(url: URL) -> None:
    assert url.host in SAFE_REDIS_HOSTS, "Redis integration target must be loopback"
    database = int((url.database or "/0").lstrip("/"))
    assert database > 0, "Redis integration tests must not use database zero"


@pytest.fixture
def postgres_database_url() -> Iterator[str]:
    admin_url = _required_test_url("TEST_POSTGRES_ADMIN_URL")
    _assert_disposable_postgres(admin_url)
    database_name = f"repopulse_test_{_test_namespace()}"[:63]
    admin_dsn = _psycopg_dsn(admin_url)

    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

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
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
            )


def _alembic_config(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("SYNC_DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return config


def test_empty_postgres_database_migrates_to_head(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _alembic_config(postgres_database_url, monkeypatch)

    command.upgrade(config, "head")

    engine = create_engine(postgres_database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"alembic_version", "repositories", "repo_snapshots", "ranking_runs"} <= tables
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == ScriptDirectory.from_config(config).get_current_head()
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_upgrade_from_previous_revision_retains_rows(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _alembic_config(postgres_database_url, monkeypatch)
    scripts = ScriptDirectory.from_config(config)
    head_revision = scripts.get_current_head()
    assert head_revision is not None
    head = scripts.get_revision(head_revision)
    assert isinstance(head.down_revision, str)
    command.upgrade(config, head.down_revision)

    inserted_at = datetime(2026, 9, 16, tzinfo=UTC)
    engine = create_engine(postgres_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job_runs "
                    "(job_key, task_name, status, attempts, error_message, started_at, "
                    "finished_at, progress, cancel_requested) "
                    "VALUES (:key, 'integration_probe', 'completed', 1, NULL, :started_at, "
                    ":started_at, NULL, false)"
                ),
                {"key": "migration-retention", "started_at": inserted_at},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(postgres_database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT task_name, status, attempts FROM job_runs WHERE job_key = :key"),
                {"key": "migration-retention"},
            ).one()
        assert row == ("integration_probe", "completed", 1)
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_postgres_transactions_and_row_locks_are_enforced(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _alembic_config(postgres_database_url, monkeypatch)
    command.upgrade(config, "head")
    dsn = _psycopg_dsn(make_url(postgres_database_url))

    try:
        with psycopg.connect(dsn) as setup:
            setup.execute("CREATE TABLE transaction_probe (id integer PRIMARY KEY, value integer)")
            setup.execute("INSERT INTO transaction_probe VALUES (1, 10)")

        with psycopg.connect(dsn) as rolled_back:
            rolled_back.execute("UPDATE transaction_probe SET value = 99 WHERE id = 1")
            rolled_back.rollback()
        with psycopg.connect(dsn) as verifier:
            assert verifier.execute(
                "SELECT value FROM transaction_probe WHERE id = 1"
            ).fetchone() == (10,)

        with (
            psycopg.connect(dsn, options="-c statement_timeout=5000") as owner,
            psycopg.connect(dsn, options="-c statement_timeout=5000") as contender,
        ):
            owner.execute("SELECT id FROM transaction_probe WHERE id = 1 FOR UPDATE")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                contender.execute(
                    "SELECT id FROM transaction_probe WHERE id = 1 FOR UPDATE NOWAIT"
                )
            contender.rollback()
            owner.rollback()
            assert contender.execute(
                "SELECT id FROM transaction_probe WHERE id = 1 FOR UPDATE NOWAIT"
            ).fetchone() == (1,)
    finally:
        get_settings.cache_clear()


def test_redis_lock_and_limiter_namespace_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_url = _required_test_url("TEST_REDIS_URL")
    _assert_disposable_redis(redis_url)
    namespace = _test_namespace()
    limiter_key = f"{namespace}:limiter:probe"
    task_lock_key = f"lock:{namespace}:worker"
    client: SyncRedis = SyncRedis.from_url(redis_url.render_as_string(hide_password=False))
    monkeypatch.setenv("REDIS_URL", redis_url.render_as_string(hide_password=False))
    get_settings.cache_clear()

    try:
        with client.pipeline(transaction=True) as pipeline:
            pipeline.incr(limiter_key)
            pipeline.expire(limiter_key, 60)
            result = pipeline.execute()
        assert result == [1, True]
        remaining_ttl = client.ttl(limiter_key)
        assert isinstance(remaining_ttl, int)
        assert 0 < remaining_ttl <= 60

        with tasks._task_lock(f"{namespace}:worker", timeout=30, required=True) as first:
            assert first is True
            with tasks._task_lock(f"{namespace}:worker", timeout=30, required=True) as second:
                assert second is False
        with tasks._task_lock(f"{namespace}:worker", timeout=30, required=True) as reacquired:
            assert reacquired is True
    finally:
        client.delete(limiter_key, task_lock_key)
        client.close()
        get_settings.cache_clear()
