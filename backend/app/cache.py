from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any

import anyio
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.singleflight import SingleFlight

logger = logging.getLogger(__name__)

_ENTRY_OVERHEAD_BYTES = 128
_REDIS_FILL_TTL_SECONDS = 30
_DEFAULT_TTL_SECONDS = 300
_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _LocalEntry:
    expires_at: float
    payload: bytes
    size: int


class ResponseCache:
    """Bounded in-process JSON cache with Redis fallback.

    Local capacity is enforced by an O(1) LRU ``OrderedDict`` under two limits:
    ``max_entries`` entries and an estimated ``max_bytes`` footprint that counts
    the key, the serialized payload, and a fixed per-entry overhead. Reads and
    evictions delete expired entries; writes never sweep or rebuild the whole
    map. Redis failures are non-fatal, so local reads and writes still apply.

    A default instance (no injected ``client``) is inert until :meth:`start` is
    called; ``get``/``set`` never open Redis on their own. :meth:`close` is
    terminal for that instance, and :meth:`start` recreates the Redis client and
    the single-flight registry so repeated app lifespans work. An injected
    ``client`` counts as already started and is only closed, never recreated.
    """

    def __init__(
        self,
        *,
        max_entries: int | None = None,
        max_bytes: int | None = None,
        client: Redis | None = None,
    ) -> None:
        settings = get_settings()
        self._max_entries = (
            settings.response_cache_max_entries if max_entries is None else max_entries
        )
        self._max_bytes = settings.response_cache_max_bytes if max_bytes is None else max_bytes
        self._memory: OrderedDict[str, _LocalEntry] = OrderedDict()
        self._total_bytes = 0
        self._client = client
        self._started = client is not None
        self._closed = False
        self._singleflight = self._new_singleflight()

    def _new_singleflight(self) -> SingleFlight:
        settings = get_settings()
        return SingleFlight(
            max_inflight=settings.ranking_max_inflight,
            max_waiters_per_key=settings.ranking_max_waiters_per_key,
        )

    def start(self) -> None:
        """Create the Redis client and flights exactly once per lifespan.

        Idempotent while started; after :meth:`close` it recreates both so a
        fresh TestClient lifespan or worker process can reuse the global.
        """
        if self._started:
            return
        settings = get_settings()
        self._client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
        )
        self._singleflight = self._new_singleflight()
        self._closed = False
        self._started = True

    @property
    def singleflight(self) -> SingleFlight:
        return self._singleflight

    @property
    def estimated_bytes(self) -> int:
        return self._total_bytes

    @property
    def entry_count(self) -> int:
        return len(self._memory)

    async def get(self, key: str) -> dict[str, Any] | None:
        client = self._client
        if not self._started or client is None:
            return None
        now = monotonic()
        entry = self._memory.get(key)
        if entry is not None:
            if entry.expires_at > now:
                self._memory.move_to_end(key)
                return json.loads(entry.payload)
            del self._memory[key]
            self._total_bytes -= entry.size
        try:
            raw = await client.get(key)
        except RedisError:
            return None
        if not raw:
            return None
        payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            return None
        self._insert(key, payload, now + _REDIS_FILL_TTL_SECONDS)
        return value

    async def set(
        self, key: str, value: dict[str, Any], ttl_seconds: int = _DEFAULT_TTL_SECONDS
    ) -> None:
        client = self._client
        if not self._started or client is None:
            return
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._insert(key, payload, monotonic() + ttl_seconds)
        try:
            await client.set(key, payload, ex=ttl_seconds)
        except RedisError:
            logger.debug("Redis unavailable; response cache skipped", exc_info=True)

    async def ping(self) -> bool:
        if not self._started or self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        """Release Redis, pending flights, and local entries exactly once."""
        if self._closed and not self._started:
            return
        self._closed = True
        self._started = False
        self._singleflight.close()
        self._memory.clear()
        self._total_bytes = 0
        client = self._client
        self._client = None
        if client is None:
            return
        with anyio.CancelScope(shield=True), anyio.move_on_after(_CLOSE_TIMEOUT_SECONDS):
            try:
                await client.aclose()
            except RedisError:
                logger.debug("Redis close failed; continuing shutdown", exc_info=True)

    def _insert(self, key: str, payload: bytes, expires_at: float) -> None:
        size = _ENTRY_OVERHEAD_BYTES + len(key.encode("utf-8")) + len(payload)
        existing = self._memory.pop(key, None)
        if existing is not None:
            self._total_bytes -= existing.size
        if self._max_entries < 1 or size > self._max_bytes:
            return
        while self._memory and (
            len(self._memory) >= self._max_entries or self._total_bytes + size > self._max_bytes
        ):
            _, oldest = self._memory.popitem(last=False)
            self._total_bytes -= oldest.size
        self._memory[key] = _LocalEntry(expires_at=expires_at, payload=payload, size=size)
        self._total_bytes += size


response_cache = ResponseCache()
