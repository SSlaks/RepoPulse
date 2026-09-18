import asyncio
import atexit
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="repopulse-pytest-"))
_TEST_DATABASE = _TEST_ROOT / "default.sqlite3"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Pytest launched from ``backend`` does not add the repository root to
# ``sys.path``.  The worker package lives beside backend and is imported by
# fixtures and collection tests, so make that package resolvable consistently
# in local runs and CI.
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))


def _remove_test_root() -> None:
    resolved = _TEST_ROOT.resolve()
    if resolved.parent != Path(tempfile.gettempdir()).resolve() or not resolved.name.startswith(
        "repopulse-pytest-"
    ):
        raise RuntimeError("refusing to remove an unexpected pytest directory")
    shutil.rmtree(resolved, ignore_errors=True)


os.environ.update(
    {
        "DATABASE_URL": f"sqlite+aiosqlite:///{_TEST_DATABASE.as_posix()}",
        "SYNC_DATABASE_URL": f"sqlite:///{_TEST_DATABASE.as_posix()}",
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "ENVIRONMENT": "test",
        "SEED_DEMO_DATA": "false",
        "GITHUB_TOKEN": "",
        "SENTRY_DSN": "",
        "AVATAR_CACHE_DIR": str(_TEST_ROOT / "avatars"),
    }
)
atexit.register(_remove_test_root)

import pytest
from app.config import Settings, get_settings

# Settings normally supports a repository-local .env file. Tests use only the
# explicit environment above, so a developer's secrets and production targets
# can never influence imports performed during collection.
Settings.model_config["env_file"] = None
get_settings.cache_clear()

from app.database import async_engine, async_session_factory
from app.models import Base, JobRun
from app.seed import seed_demo_data
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_collection import repository_data

from worker.app import tasks


class InMemoryResponseCache:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    async def set(
        self, key: str, value: dict[str, Any], ttl_seconds: int = 300
    ) -> None:
        del ttl_seconds
        self.values[key] = value

    async def ping(self) -> bool:
        return True


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: disposable PostgreSQL/Redis tests enabled with RUN_INTEGRATION_TESTS=1",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_INTEGRATION_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set RUN_INTEGRATION_TESTS=1 for disposable service tests")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolate_external_services(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("integration"):
        yield
        return

    def reject_network(*_: object, **__: object) -> NoReturn:
        raise AssertionError("default pytest suite attempted a real network connection")

    cache = InMemoryResponseCache()
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(connection: socket.socket, address: object) -> None:
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(connection, address)
        reject_network()

    def guarded_connect_ex(connection: socket.socket, address: object) -> int:
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect_ex(connection, address)
        reject_network()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    import httpx

    original_http_send = httpx.Client.send
    original_async_http_send = httpx.AsyncClient.send

    def guarded_http_send(client: httpx.Client, request: httpx.Request, **kwargs: Any):
        if isinstance(client._transport, httpx.MockTransport):
            return original_http_send(client, request, **kwargs)
        return reject_network()

    async def guarded_async_http_send(
        client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ):
        if isinstance(client._transport, httpx.MockTransport):
            return await original_async_http_send(client, request, **kwargs)
        return reject_network()

    from starlette.testclient import TestClient

    monkeypatch.setattr(httpx.Client, "send", guarded_http_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_http_send)
    monkeypatch.setattr(TestClient, "send", original_http_send)
    monkeypatch.setattr("redis.Redis.execute_command", reject_network)
    monkeypatch.setattr("redis.asyncio.Redis.execute_command", reject_network)
    monkeypatch.setattr("app.services.catalog.response_cache", cache)
    monkeypatch.setattr("app.services.readme.response_cache", cache)
    monkeypatch.setattr("app.api.routes.system.response_cache", cache)
    monkeypatch.setattr("app.cache.response_cache", cache)
    monkeypatch.setattr("app.task_queue.task_sender.send_task", reject_network)
    monkeypatch.setattr(tasks.capture_daily_snapshots, "delay", reject_network)
    monkeypatch.setattr(tasks.publish_daily_rankings, "delay", reject_network)
    yield


@pytest.fixture
def seeded_api_database() -> None:
    async def reset_and_seed() -> None:
        await async_engine.dispose()
        async with async_engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with async_session_factory() as session:
            await seed_demo_data(session)
        await async_engine.dispose()

    asyncio.run(reset_and_seed())


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(hour=2, minute=0, second=0, microsecond=0)
    with factory() as session:
        for index in range(1, 4):
            tasks._upsert_repository(session, repository_data(index))
        session.add(JobRun(job_key=f"snapshot:all:{now.date()}",
                           task_name="capture_daily_snapshots", status="running", started_at=now))
        session.commit()
    settings = Settings(repository_auto_quarantine=True, snapshot_requests_per_second=10)
    monkeypatch.setattr(tasks, "get_sync_session", factory)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr("worker.app.snapshots.get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "_task_lock", lambda *a, **kw: nullcontext(True))
    yield factory, now
    engine.dispose()
