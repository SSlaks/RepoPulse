"""Service-level pipeline tests for bounded cache + single-flight ranking reads.

These drive the real :class:`CatalogService` / ``RankingQuery`` with a
contracted in-memory cache and a scripted repository so the producer/follower
protocol, the read-transaction lifetime and the budget can be observed without
a database or Redis. The final test exercises the real PostgreSQL repository.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anyio
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.cache import ResponseCache
from app.config import get_settings
from app.database import async_session_factory
from app.models import Base, RankingItem, RankingRun, Repository
from app.ranking.filters import RankingFilters
from app.repositories.catalog import CatalogRepository
from app.schemas import PeriodDays, RankingResponse
from app.services import catalog as catalog_module
from app.services import ranking_query as ranking_query_module
from app.services.catalog import CatalogService
from app.services.ranking_query import ranking_cache_key, snapshot_run
from app.singleflight import (
    FollowerLease,
    ProducerLease,
    SingleFlight,
)
from fastapi import HTTPException
from psycopg import sql as psycopg_sql
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_infrastructure_integration import (
    _assert_disposable_postgres,
    _psycopg_dsn,
    _required_test_url,
    _test_namespace,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 16, 2, 0, tzinfo=UTC)


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


# --------------------------------------------------------------------- doubles


class MemoryRedis:
    """Async Redis stand-in that records writes for ordering assertions."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.values: dict[str, str] = {}
        self.closed = False
        self._events = events

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        del ex
        if self._events is not None:
            self._events.append("redis:set")
        self.values[key] = value.decode("utf-8") if isinstance(value, bytes) else value

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class GatedRedis:
    """Redis stand-in that stalls the first GET until the test releases it."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_count = 0
        self.get_calls = 0
        self.first_get_entered = asyncio.Event()
        self.release_first_get = asyncio.Event()

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        if self.get_calls == 1:
            self.first_get_entered.set()
            await self.release_first_get.wait()
            return None
        return self.values.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        del ex
        self.set_count += 1
        self.values[key] = value.decode("utf-8") if isinstance(value, bytes) else value

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class RecordingSingleFlight(SingleFlight):
    """SingleFlight that records wait transitions for ordering assertions."""

    def __init__(
        self,
        *,
        events: list[str],
        max_inflight: int = 32,
        max_waiters_per_key: int = 64,
    ) -> None:
        super().__init__(max_inflight=max_inflight, max_waiters_per_key=max_waiters_per_key)
        self._events = events

    async def wait(self, lease: FollowerLease, *, timeout: float) -> dict[str, object]:
        self._events.append("wait:start")
        try:
            return await super().wait(lease, timeout=timeout)
        finally:
            self._events.append("wait:end")

    def complete(self, lease: ProducerLease, value: dict[str, object]) -> None:
        self._events.append("flight:complete")
        super().complete(lease, value)

    def fail(self, lease: ProducerLease, error: BaseException) -> None:
        self._events.append("flight:fail")
        super().fail(lease, error)


def build_cache(
    events: list[str] | None = None,
    *,
    max_inflight: int = 32,
    max_waiters: int = 64,
) -> ResponseCache:
    if events is None:
        events = []
    cache = ResponseCache(max_entries=512, max_bytes=16_777_216, client=MemoryRedis(events))
    cache._singleflight = RecordingSingleFlight(
        events=events,
        max_inflight=max_inflight,
        max_waiters_per_key=max_waiters,
    )
    return cache


def make_run(
    *,
    run_id: int = 1,
    fingerprint: str = "fp",
    config_version: str = "v1",
    as_of: datetime = NOW,
) -> RankingRun:
    return RankingRun(
        id=run_id,
        period_days=7,
        as_of=as_of,
        baseline_at=as_of,
        config_version=config_version,
        status="ready",
        collection_summary={
            "fingerprint": fingerprint,
            "expected": 0,
            "succeeded": 0,
            "missing": 0,
            "completeness_percent": 100.0,
            "is_partial": False,
        },
        published_at=as_of,
    )


class FakeRepository:
    """Scripted CatalogRepository that records lifecycle and load events."""

    def __init__(
        self,
        runs: RankingRun | list[RankingRun],
        *,
        events: list[str] | None = None,
        gate: asyncio.Event | None = None,
        coverage: int = 7,
    ) -> None:
        self._runs = runs if isinstance(runs, list) else [runs]
        self._index = 0
        self._events = events if events is not None else []
        self._gate = gate
        self.coverage = coverage
        self.page_calls = 0
        self.coverage_calls = 0
        self.run_calls = 0
        self.in_read = False

    async def begin_read(self) -> None:
        self.in_read = True
        self._events.append("begin")

    async def release_read(self) -> None:
        self.in_read = False
        self._events.append("release")

    async def latest_ranking_run(self, period_days: int) -> RankingRun:
        self.run_calls += 1
        self._events.append("run")
        run = self._runs[min(self._index, len(self._runs) - 1)]
        self._index += 1
        return run

    async def ranking_page(self, run_id, filters, *, page, limit):
        self.page_calls += 1
        self._events.append("load")
        if self._gate is not None:
            await self._gate.wait()
        return 0, []

    async def coverage_count(self) -> int:
        self.coverage_calls += 1
        return self.coverage


class CancelOnceRepository(FakeRepository):
    """First ranking_page waits then raises a cancellation to mark the flight."""

    def __init__(self, runs, *, events: list[str], error_gate: asyncio.Event) -> None:
        super().__init__(runs, events=events)
        self._error_gate = error_gate

    async def ranking_page(self, run_id, filters, *, page, limit):
        self.page_calls += 1
        self._events.append("load")
        if self.page_calls == 1:
            await self._error_gate.wait()
            raise asyncio.CancelledError
        return 0, []


def patch_service(monkeypatch: pytest.MonkeyPatch, repository, cache: ResponseCache) -> None:
    monkeypatch.setattr(catalog_module, "CatalogRepository", lambda session: repository)
    monkeypatch.setattr(catalog_module, "response_cache", cache)


async def call_rankings(
    *,
    period: int = 7,
    language: str | None = None,
    topic: str | None = None,
    min_stars: int = 0,
    query: str | None = None,
    page: int = 1,
    limit: int = 15,
) -> RankingResponse:
    async with async_session_factory() as session:
        return await CatalogService(session).rankings(
            period=PeriodDays(period),
            language=language,
            topic=topic,
            min_stars=min_stars,
            query=query,
            page=page,
            limit=limit,
        )


async def wait_until(predicate: Callable[[], bool], *, turns: int = 10_000) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("bounded wait never observed the expected state")


def flight_count(cache: ResponseCache) -> int:
    return len(cache.singleflight._flights)


def waiter_total(cache: ResponseCache) -> int:
    return sum(flight.waiters for flight in cache.singleflight._flights.values())


def fill_flights(cache: ResponseCache, count: int) -> list[ProducerLease]:
    leases = []
    for index in range(count):
        lease = cache.singleflight.claim(f"filler-{index}")
        assert isinstance(lease, ProducerLease)
        leases.append(lease)
    return leases


def set_timeout(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(
        ranking_query_module,
        "get_settings",
        lambda: SimpleNamespace(ranking_load_timeout_seconds=seconds),
    )


# ------------------------------------------------------------------ pure unit


def test_parameters_lower_strings_but_preserve_literals() -> None:
    parameters = RankingFilters(
        language="PyThOn", topic=" AI ", min_stars=5, query="%_\x00"
    ).parameters()

    assert parameters == ["python", " ai ", 5, "%_\x00"]


def test_parameters_preserve_none_empty_dash_and_whitespace() -> None:
    assert RankingFilters().parameters() == [None, None, 0, None]
    assert RankingFilters(language="", topic="-", query=" ").parameters() == ["", "-", 0, " "]


def test_parameters_use_lower_not_casefold() -> None:
    parameters = RankingFilters(language="ß", topic="İstanbul").parameters()
    assert parameters[0] == "ß"
    assert parameters[1] == "i̇stanbul"


def test_cache_key_is_fixed_length_and_version_sensitive() -> None:
    snapshot = snapshot_run(make_run(fingerprint="abc"))
    first = ranking_cache_key(snapshot, RankingFilters(), 1, 15)
    assert first.startswith("rankings:v6:")
    assert len(first) == len("rankings:v6:") + 64
    assert ranking_cache_key(snapshot, RankingFilters(), 1, 15) == first
    assert ranking_cache_key(snapshot, RankingFilters(), 2, 15) != first
    assert ranking_cache_key(snapshot, RankingFilters(language="python"), 1, 15) != first


def test_cache_key_normalizes_published_instant_to_utc() -> None:
    aware = make_run(as_of=NOW)
    naive = make_run(as_of=NOW.replace(tzinfo=None))
    shifted = make_run(as_of=NOW.astimezone(timezone(timedelta(hours=-7))))

    assert snapshot_run(aware).published_at == NOW.isoformat()
    assert snapshot_run(naive).published_at == NOW.isoformat()
    assert snapshot_run(shifted).published_at == NOW.isoformat()

    key = ranking_cache_key(snapshot_run(aware), RankingFilters(), 1, 15)
    assert ranking_cache_key(snapshot_run(naive), RankingFilters(), 1, 15) == key
    assert ranking_cache_key(snapshot_run(shifted), RankingFilters(), 1, 15) == key
    # generated_at stays the run's own timestamp; only the cache key is normalized.
    assert snapshot_run(naive).generated_at == NOW.replace(tzinfo=None)
    assert snapshot_run(shifted).generated_at == NOW


# ------------------------------------------------------- bounded coordination


async def test_metadata_phase_latest_run_timeout_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = asyncio.Event()
    events: list[str] = []

    class BlockedRunRepository(FakeRepository):
        async def latest_ranking_run(self, period_days: int) -> RankingRun:
            self.run_calls += 1
            self._events.append("run")
            await blocked.wait()
            return self._runs[0]

    repository = BlockedRunRepository(make_run(), events=events)
    cache = build_cache(events)
    patch_service(monkeypatch, repository, cache)
    set_timeout(monkeypatch, 0.05)

    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(call_rankings(), timeout=0.3)

    assert caught.value.status_code == 503
    assert flight_count(cache) == 0
    assert repository.in_read is False
    assert "release" in events


async def test_metadata_phase_cache_get_timeout_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = asyncio.Event()
    events: list[str] = []
    repository = FakeRepository(make_run(), events=events)
    cache = build_cache(events)

    async def blocked_get(key: str) -> None:
        await blocked.wait()

    cache.get = blocked_get
    patch_service(monkeypatch, repository, cache)
    set_timeout(monkeypatch, 0.05)

    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(call_rankings(), timeout=0.3)

    assert caught.value.status_code == 503
    assert flight_count(cache) == 0
    assert repository.in_read is False
    assert "release" in events


async def test_fifty_identical_requests_share_one_load(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    events: list[str] = []
    repository = FakeRepository(make_run(), events=events, gate=gate)
    cache = build_cache(events)
    patch_service(monkeypatch, repository, cache)

    tasks = [asyncio.create_task(call_rankings()) for _ in range(50)]
    await wait_until(lambda: repository.page_calls == 1 and waiter_total(cache) == 49)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert repository.page_calls == 1
    assert repository.coverage_calls == 1
    assert all(result == results[0] for result in results)
    assert flight_count(cache) == 0


async def test_late_miss_after_completed_flight_rechecks_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository = FakeRepository(make_run(), events=events)
    client = GatedRedis()
    cache = ResponseCache(max_entries=512, max_bytes=16_777_216, client=client)
    patch_service(monkeypatch, repository, cache)
    key = ranking_cache_key(snapshot_run(make_run()), RankingFilters(), 1, 15)

    # B misses, then stalls inside its first GET while still holding its read.
    late = asyncio.create_task(call_rankings())
    await wait_until(lambda: client.first_get_entered.is_set())

    # A is the sole producer: one load, one write, flight cleared on completion.
    early = await call_rankings()
    assert repository.page_calls == 1
    assert client.set_count == 1
    assert flight_count(cache) == 0
    expiry_after_a = cache._memory[key].expires_at

    # B's stale miss resolves after A published; it claims a fresh lease, so the
    # producer must double-check before touching the database again.
    client.release_first_get.set()
    late_result = await late

    assert late_result == early
    assert repository.page_calls == 1
    assert repository.coverage_calls == 1
    assert client.set_count == 1
    assert cache._memory[key].expires_at == expiry_after_a
    assert flight_count(cache) == 0
    assert repository.in_read is False


async def test_different_keys_load_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(make_run())
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)

    first, second = await asyncio.gather(
        call_rankings(language="python"), call_rankings(language="go")
    )

    assert repository.page_calls == 2
    assert first.meta.period_days == second.meta.period_days


async def test_cache_hit_is_served_while_inflight_cap_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(make_run())
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)

    await call_rankings()
    assert repository.page_calls == 1
    fill_flights(cache, 32)

    cached = await call_rankings()

    assert cached.meta.total == 0
    assert repository.page_calls == 1


async def test_new_key_over_inflight_cap_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(make_run())
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)

    fill_flights(cache, 32)

    with pytest.raises(HTTPException) as caught:
        await call_rankings(language="python")

    assert caught.value.status_code == 503
    assert caught.value.headers == {"Cache-Control": "no-store", "Retry-After": "5"}
    assert repository.page_calls == 0


async def test_waiter_cap_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeRepository(make_run())
    cache = build_cache(max_waiters=64)
    patch_service(monkeypatch, repository, cache)

    key = ranking_cache_key(snapshot_run(make_run()), RankingFilters(), 1, 15)
    assert isinstance(cache.singleflight.claim(key), ProducerLease)
    for _ in range(64):
        assert isinstance(cache.singleflight.claim(key), FollowerLease)

    with pytest.raises(HTTPException) as caught:
        await call_rankings()

    assert caught.value.status_code == 503
    assert repository.page_calls == 0


async def test_follower_cancel_spares_the_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    repository = FakeRepository(make_run(), gate=gate)
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)

    producer = asyncio.create_task(call_rankings())
    await wait_until(lambda: repository.page_calls == 1 and flight_count(cache) == 1)
    follower = asyncio.create_task(call_rankings())
    await wait_until(lambda: waiter_total(cache) == 1)

    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower

    assert flight_count(cache) == 1
    assert waiter_total(cache) == 0
    gate.set()
    assert (await producer).meta.total == 0


async def test_producer_timeout_is_503_then_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    repository = FakeRepository(make_run(), gate=gate)
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)
    set_timeout(monkeypatch, 0.05)

    with pytest.raises(HTTPException) as caught:
        await call_rankings()

    assert caught.value.status_code == 503
    assert repository.page_calls == 1
    assert flight_count(cache) == 0

    gate.set()
    set_timeout(monkeypatch, 10)
    retried = await call_rankings()

    assert retried.meta.total == 0
    assert repository.page_calls == 2


async def test_follower_retries_once_with_fresh_run_after_producer_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_gate = asyncio.Event()
    old_run = make_run(run_id=1, fingerprint="old", as_of=NOW)
    new_run = make_run(run_id=2, fingerprint="new", as_of=NOW + timedelta(days=1))
    events: list[str] = []
    repository = CancelOnceRepository([old_run, old_run, new_run], events=events, error_gate=error_gate)
    cache = build_cache(events)
    patch_service(monkeypatch, repository, cache)

    async def guarded() -> RankingResponse | str:
        try:
            return await call_rankings()
        except BaseException as error:  # noqa: BLE001 - classify the producer cancel
            return type(error).__name__

    producer = asyncio.create_task(guarded())
    await wait_until(lambda: repository.page_calls == 1)
    follower = asyncio.create_task(guarded())
    await wait_until(lambda: waiter_total(cache) == 1)
    error_gate.set()
    producer_result, follower_result = await asyncio.gather(producer, follower)

    assert producer_result == "CancelledError"
    assert isinstance(follower_result, RankingResponse)
    assert follower_result.meta.as_of == new_run.as_of
    assert repository.run_calls == 3
    assert repository.page_calls == 2


async def test_release_precedes_wait_and_redis_write(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    repository = FakeRepository(make_run(), events=events)
    cache = build_cache(events)
    patch_service(monkeypatch, repository, cache)

    await call_rankings()

    assert events.index("release") < events.index("redis:set")

    # Follower: a pre-claimed lease makes the request join and wait.
    follower_events: list[str] = []
    follower_repo = FakeRepository(make_run(), events=follower_events)
    follower_cache = build_cache(follower_events)
    patch_service(monkeypatch, follower_repo, follower_cache)
    key = ranking_cache_key(snapshot_run(make_run()), RankingFilters(), 1, 15)
    assert isinstance(follower_cache.singleflight.claim(key), ProducerLease)

    follower = asyncio.create_task(call_rankings())
    await wait_until(lambda: "wait:start" in follower_events)
    assert follower_events.index("release") < follower_events.index("wait:start")
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower


async def test_producer_task_cancel_is_shielded_until_the_result_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    repository = FakeRepository(make_run(), gate=gate)
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)

    producer = asyncio.create_task(call_rankings())
    await wait_until(lambda: repository.page_calls == 1 and flight_count(cache) == 1)
    follower = asyncio.create_task(call_rankings())
    await wait_until(lambda: waiter_total(cache) == 1)

    producer.cancel()
    gate.set()

    follower_result = await follower
    assert isinstance(follower_result, RankingResponse)
    assert follower_result.meta.total == 0
    with pytest.raises(asyncio.CancelledError):
        await producer


async def test_anyio_scope_cancel_restores_caller_cancel_after_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    events: list[str] = []
    repository = FakeRepository(make_run(), events=events, gate=gate)
    cache = build_cache(events)
    patch_service(monkeypatch, repository, cache)

    holder: dict[str, anyio.CancelScope] = {}
    observed: dict[str, str] = {}

    async def producer_call() -> RankingResponse | None:
        with anyio.CancelScope() as scope:
            holder["scope"] = scope
            try:
                result = await call_rankings()
            except BaseException as error:  # noqa: BLE001 - classify the restore
                observed["producer"] = type(error).__name__
                return None
            observed["producer"] = "returned"
            return result

    producer = asyncio.create_task(producer_call())
    await wait_until(lambda: repository.page_calls == 1 and "scope" in holder)
    follower = asyncio.create_task(call_rankings())
    await wait_until(lambda: waiter_total(cache) == 1)
    holder["scope"].cancel()
    gate.set()

    follower_result = await follower
    producer_result = await producer

    assert observed["producer"] == "CancelledError"
    assert producer_result is None
    assert isinstance(follower_result, RankingResponse)
    assert follower_result.meta.total == 0
    assert flight_count(cache) == 0
    assert "redis:set" in events


async def test_producer_cancel_wakes_followers_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_gate = asyncio.Event()
    events: list[str] = []
    run = make_run()
    repository = CancelOnceRepository([run, run], events=events, error_gate=error_gate)
    cache = build_cache(events)
    patch_service(monkeypatch, repository, cache)

    async def guarded() -> RankingResponse | str:
        try:
            return await call_rankings()
        except BaseException as error:  # noqa: BLE001 - classify the producer cancel
            return type(error).__name__

    producer = asyncio.create_task(guarded())
    await wait_until(lambda: repository.page_calls == 1)
    follower = asyncio.create_task(guarded())
    await wait_until(lambda: waiter_total(cache) == 1)
    error_gate.set()
    producer_result, follower_result = await asyncio.gather(producer, follower)

    assert producer_result == "CancelledError"
    assert isinstance(follower_result, RankingResponse)
    fail_index = events.index("flight:fail")
    assert events[fail_index - 1] == "release"


async def test_producer_failure_is_shared_with_followers_as_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()

    class FailingRepository(FakeRepository):
        async def ranking_page(self, run_id, filters, *, page, limit):
            self.page_calls += 1
            self._events.append("load")
            await gate.wait()
            raise RuntimeError("database unavailable")

    repository = FailingRepository(make_run())
    cache = build_cache()
    patch_service(monkeypatch, repository, cache)

    producer = asyncio.create_task(call_rankings())
    await wait_until(lambda: repository.page_calls == 1 and flight_count(cache) == 1)
    follower = asyncio.create_task(call_rankings())
    await wait_until(lambda: waiter_total(cache) == 1)

    gate.set()
    with pytest.raises(RuntimeError):
        await producer
    with pytest.raises(HTTPException) as caught:
        await follower

    assert caught.value.status_code == 503
    assert caught.value.headers == {"Cache-Control": "no-store", "Retry-After": "5"}


# ------------------------------------------------------------------- lifecycle


async def test_start_close_reopen_recreates_redis_and_flights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[MemoryRedis] = []

    def build_client(url: str, **kwargs: object) -> MemoryRedis:
        del url, kwargs
        client = MemoryRedis()
        clients.append(client)
        return client

    monkeypatch.setattr("app.cache.Redis.from_url", build_client)
    cache = ResponseCache(max_entries=8, max_bytes=1_000_000)

    assert await cache.get("k") is None
    await cache.set("k", {"v": 1})
    assert cache.entry_count == 0

    cache.start()
    cache.start()
    assert len(clients) == 1
    await cache.set("k", {"v": 1})
    assert await cache.get("k") == {"v": 1}

    await cache.close()
    assert clients[0].closed is True
    assert await cache.get("k") is None
    assert cache.entry_count == 0

    cache.start()
    assert len(clients) == 2
    assert await cache.ping() is True
    await cache.close()


async def test_sequential_reads_repeat_on_one_session(seeded_api_database: None) -> None:
    async with async_session_factory() as session:
        service = CatalogService(session)
        first = await service.rankings(
            period=PeriodDays.SEVEN,
            language=None,
            topic=None,
            min_stars=0,
            query=None,
            page=1,
            limit=5,
        )
        second = await service.rankings(
            period=PeriodDays.SEVEN,
            language=None,
            topic=None,
            min_stars=0,
            query=None,
            page=1,
            limit=5,
        )

    assert first == second
    assert first.meta.total >= 1


async def test_release_read_rolls_back_pending_changes(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repository = CatalogRepository(session)
            await repository.begin_read()
            session.add(
                Repository(
                    github_id=1,
                    full_name="owner/pending",
                    owner="owner",
                    name="pending",
                    html_url="https://github.com/owner/pending",
                    first_tracked_at=NOW,
                    last_seen_at=NOW,
                    history_available_from=NOW,
                )
            )
            await session.flush()
            assert session.in_transaction()

            await repository.release_read()

            assert not session.in_transaction()
        async with factory() as session:
            assert await session.scalar(select(func.count(Repository.id))) == 0
    finally:
        await engine.dispose()


async def test_release_read_invalidates_and_propagates_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'failure.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repository = CatalogRepository(session)
            await repository.begin_read()
            invalidations: list[str] = []

            async def failing_rollback() -> None:
                raise SQLAlchemyError("rollback failed")

            async def recording_invalidate() -> None:
                invalidations.append("invalidate")

            monkeypatch.setattr(session, "rollback", failing_rollback)
            monkeypatch.setattr(session, "invalidate", recording_invalidate)

            with pytest.raises(SQLAlchemyError):
                await repository.release_read()

            assert invalidations == ["invalidate"]
    finally:
        await engine.dispose()


def test_lifespan_closes_cache_and_limiter_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    events: list[str] = []

    class StubCache:
        def start(self) -> None:
            events.append("cache:start")

        async def close(self) -> None:
            events.append("cache:close")

    class StubLimiter:
        async def close(self) -> None:
            events.append("limiter:close")

    async def stubborn_seed(_session: object) -> None:
        raise RuntimeError("seed failed")

    monkeypatch.setattr(main_module, "response_cache", StubCache())
    monkeypatch.setattr(main_module, "limiter", StubLimiter())
    monkeypatch.setattr(main_module, "seed_demo_data", stubborn_seed)
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(seed_demo_data=True, environment="development"),
    )

    async def run() -> None:
        with pytest.raises(RuntimeError):
            async with main_module.lifespan(main_module.app):
                pass

    asyncio.run(run())

    assert events == ["cache:start", "cache:close", "limiter:close"]


# --------------------------------------------------------------- real postgres


@pytest.mark.integration
async def test_postgres_reader_preserves_old_run_without_blocking_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url = _required_test_url("TEST_POSTGRES_ADMIN_URL")
    _assert_disposable_postgres(admin_url)
    database_name = f"repopulse_sf_{_test_namespace()}"[:63]
    admin_dsn = _psycopg_dsn(admin_url)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(psycopg_sql.SQL("CREATE DATABASE {}").format(psycopg_sql.Identifier(database_name)))
    database_url = admin_url.set(database=database_name)
    sync_url = database_url.render_as_string(hide_password=False)
    monkeypatch.setenv("SYNC_DATABASE_URL", sync_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    command.upgrade(config, "head")
    async_url = (
        make_url(sync_url)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(async_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    new_run_at = NOW + timedelta(days=1)
    barrier = asyncio.Event()
    started = asyncio.Event()
    coverage_calls = {"count": 0}
    original_coverage = CatalogRepository.coverage_count

    async def blocked_coverage(self: CatalogRepository) -> int:
        coverage_calls["count"] += 1
        if coverage_calls["count"] == 1:
            started.set()
            await barrier.wait()
        return await original_coverage(self)

    cache = ResponseCache(client=MemoryRedis())
    monkeypatch.setattr(CatalogRepository, "coverage_count", blocked_coverage)
    monkeypatch.setattr(catalog_module, "response_cache", cache)

    async def request() -> RankingResponse:
        async with factory() as session:
            return await CatalogService(session).rankings(
                period=PeriodDays.SEVEN,
                language=None,
                topic=None,
                min_stars=0,
                query=None,
                page=1,
                limit=15,
            )

    def publish() -> None:
        publisher = create_engine(sync_url)
        try:
            with publisher.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO ranking_runs (period_days, as_of, baseline_at, "
                        "config_version, status, collection_summary, published_at) "
                        "VALUES (7, :as_of, :as_of, 'v2', 'ready', "
                        "CAST(:summary AS JSON), :as_of)"
                    ),
                    {
                        "as_of": new_run_at,
                        "summary": json.dumps(
                            {
                                "fingerprint": "new",
                                "expected": 1,
                                "succeeded": 1,
                                "missing": 0,
                                "completeness_percent": 100.0,
                                "is_partial": False,
                            }
                        ),
                    },
                )
        finally:
            publisher.dispose()

    try:
        async with factory() as session:
            session.add(
                Repository(
                    github_id=1,
                    full_name="owner/repo",
                    owner="owner",
                    name="repo",
                    description="desc",
                    html_url="https://github.com/owner/repo",
                    language="Python",
                    topics=["ai"],
                    stars_count=100,
                    first_tracked_at=NOW,
                    last_seen_at=NOW,
                    history_available_from=NOW,
                )
            )
            await session.flush()
            run = RankingRun(
                period_days=7,
                as_of=NOW,
                baseline_at=NOW,
                config_version="v1",
                status="ready",
                collection_summary={
                    "fingerprint": "old",
                    "expected": 1,
                    "succeeded": 1,
                    "missing": 0,
                    "completeness_percent": 100.0,
                    "is_partial": False,
                },
                published_at=NOW,
            )
            session.add(run)
            await session.flush()
            repository_id = await session.scalar(func.min(Repository.id))
            session.add(
                RankingItem(
                    ranking_run_id=run.id,
                    repository_id=repository_id,
                    rank=1,
                    previous_rank=1,
                    start_stars=90,
                    end_stars=100,
                    net_delta=10,
                    growth_rate=0.1,
                    baseline_available=True,
                )
            )
            await session.commit()

        producer = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), timeout=10)
        follower = asyncio.create_task(request())
        await wait_until(lambda: flight_count(cache) == 1 and waiter_total(cache) == 1)

        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.run_in_executor(None, publish), timeout=10)

        barrier.set()
        producer_result, follower_result = await asyncio.gather(producer, follower)

        assert producer_result.meta.generated_at == NOW
        assert follower_result.meta.collection == producer_result.meta.collection
        assert producer_result.meta.total == 1

        latest = await request()
        assert latest.meta.generated_at == new_run_at
        assert latest.meta.collection is not None
    finally:
        barrier.set()
        await engine.dispose()
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(
                psycopg_sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    psycopg_sql.Identifier(database_name)
                )
            )
