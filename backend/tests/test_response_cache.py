from __future__ import annotations

import pytest
from app.cache import ResponseCache
from app.config import Settings
from pydantic import ValidationError
from redis.exceptions import RedisError


class FakeRedis:
    """Minimal async Redis stand-in; the default suite forbids real sockets."""

    def __init__(self, *, persist_writes: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.unavailable = False
        self.closed = False
        self.persist_writes = persist_writes

    async def get(self, key: str) -> str | None:
        self._check()
        return self.values.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        del ex
        self._check()
        if self.persist_writes:
            self.values[key] = value.decode("utf-8")

    async def ping(self) -> bool:
        self._check()
        return True

    async def aclose(self) -> None:
        self.closed = True

    def _check(self) -> None:
        if self.unavailable:
            raise RedisError("redis unavailable")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr("app.cache.monotonic", fake)
    return fake


@pytest.fixture(autouse=True)
def isolate_cache_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "RESPONSE_CACHE_MAX_ENTRIES",
        "RESPONSE_CACHE_MAX_BYTES",
        "RANKING_MAX_INFLIGHT",
        "RANKING_MAX_WAITERS_PER_KEY",
        "RANKING_LOAD_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)


def build_cache(
    client: FakeRedis, *, max_entries: int = 512, max_bytes: int = 16_777_216
) -> ResponseCache:
    return ResponseCache(max_entries=max_entries, max_bytes=max_bytes, client=client)


async def test_dict_mutation_cannot_grow_or_change_the_cached_entry(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=8, max_bytes=1_000_000)
    await cache.set("k", {"items": [1, 2]}, ttl_seconds=300)
    count_before = cache.entry_count
    bytes_before = cache.estimated_bytes

    fetched = await cache.get("k")
    assert fetched is not None
    fetched["items"].append("x" * 10_000_000)
    fetched["extra"] = "y" * 1000

    assert cache.entry_count == count_before
    assert cache.estimated_bytes == bytes_before
    assert await cache.get("k") == {"items": [1, 2]}


async def test_entry_limit_evicts_least_recently_used(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=2, max_bytes=1_000_000)
    await cache.set("a", {"v": 1})
    await cache.set("b", {"v": 2})
    assert await cache.get("a") == {"v": 1}

    await cache.set("c", {"v": 3})

    assert cache.entry_count == 2
    assert await cache.get("b") is None
    assert await cache.get("a") == {"v": 1}
    assert await cache.get("c") == {"v": 3}


async def test_byte_limit_evicts_least_recently_used(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=100, max_bytes=500)
    await cache.set("k1", {"v": "x" * 200})
    await cache.set("k2", {"v": "x" * 200})

    assert cache.entry_count == 1
    assert cache.estimated_bytes <= 500
    assert await cache.get("k1") is None
    assert await cache.get("k2") == {"v": "x" * 200}


async def test_expired_entry_is_deleted_on_read(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=8, max_bytes=1_000_000)
    await cache.set("k", {"v": 1}, ttl_seconds=10)

    clock.now += 11

    assert await cache.get("k") is None
    assert cache.entry_count == 0
    assert cache.estimated_bytes == 0


async def test_expired_entry_is_dropped_when_evicting(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=1, max_bytes=1_000_000)
    await cache.set("old", {"v": 1}, ttl_seconds=5)

    clock.now += 6
    await cache.set("new", {"v": 2}, ttl_seconds=5)

    assert cache.entry_count == 1
    assert await cache.get("new") == {"v": 2}
    assert await cache.get("old") is None


async def test_access_does_not_extend_ttl(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=8, max_bytes=1_000_000)
    await cache.set("k", {"v": 1}, ttl_seconds=300)

    clock.now += 200
    assert await cache.get("k") == {"v": 1}
    clock.now += 200

    assert await cache.get("k") is None


async def test_set_default_ttl_is_three_hundred_seconds(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=8, max_bytes=1_000_000)
    await cache.set("k", {"v": 1})

    clock.now += 299
    assert await cache.get("k") == {"v": 1}
    clock.now += 2

    assert await cache.get("k") is None


async def test_redis_fill_uses_a_thirty_second_local_ttl(clock: FakeClock) -> None:
    client = FakeRedis()
    client.values["k"] = '{"v": 1}'
    cache = build_cache(client, max_entries=8, max_bytes=1_000_000)

    assert await cache.get("k") == {"v": 1}
    assert cache.entry_count == 1

    client.unavailable = True
    clock.now += 29
    assert await cache.get("k") == {"v": 1}

    clock.now += 2
    assert await cache.get("k") is None
    assert cache.entry_count == 0


async def test_oversized_entry_is_skipped_locally_but_written_to_redis(clock: FakeClock) -> None:
    client = FakeRedis(persist_writes=True)
    cache = build_cache(client, max_entries=8, max_bytes=64)

    await cache.set("k", {"v": "x" * 100})

    assert cache.entry_count == 0
    assert cache.estimated_bytes == 0
    assert "k" in client.values
    assert await cache.get("k") == {"v": "x" * 100}
    assert cache.entry_count == 0


async def test_replacement_tracks_only_the_new_estimated_bytes(clock: FakeClock) -> None:
    cache = build_cache(FakeRedis(), max_entries=8, max_bytes=1_000_000)
    await cache.set("k", {"v": "x" * 10})
    small = cache.estimated_bytes

    await cache.set("k", {"v": "x" * 60})

    assert cache.estimated_bytes - small == 50
    assert cache.entry_count == 1


async def test_redis_outage_keeps_local_cache_nonfatal(clock: FakeClock) -> None:
    client = FakeRedis()
    client.unavailable = True
    cache = build_cache(client, max_entries=8, max_bytes=1_000_000)

    await cache.set("k", {"v": 1})

    assert cache.entry_count == 1
    assert await cache.get("k") == {"v": 1}
    assert await cache.ping() is False


async def test_close_clears_state_and_refuses_new_work(clock: FakeClock) -> None:
    client = FakeRedis()
    cache = build_cache(client, max_entries=8, max_bytes=1_000_000)
    await cache.set("k", {"v": 1})

    await cache.close()

    assert client.closed is True
    assert cache.entry_count == 0
    assert cache.estimated_bytes == 0
    assert await cache.get("k") is None
    assert await cache.ping() is False

    await cache.set("k2", {"v": 2})
    assert cache.entry_count == 0
    assert "k2" not in client.values

    await cache.close()


def test_cache_settings_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.response_cache_max_entries == 512
    assert settings.response_cache_max_bytes == 16_777_216
    assert settings.ranking_max_inflight == 32
    assert settings.ranking_max_waiters_per_key == 64
    assert settings.ranking_load_timeout_seconds == 10


@pytest.mark.parametrize(
    "overrides",
    [
        {"response_cache_max_entries": 0},
        {"response_cache_max_bytes": -1},
        {"ranking_max_inflight": 0},
        {"ranking_max_waiters_per_key": -1},
        {"ranking_load_timeout_seconds": 0},
    ],
)
def test_cache_settings_reject_invalid_boundaries(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
