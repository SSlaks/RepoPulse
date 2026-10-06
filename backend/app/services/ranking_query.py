"""Cache-aware, single-flight coordination for one ranking page read.

The coordinator owns the read-transaction lifetime, the versioned cache key,
and the producer/follower protocol. It touches ORM rows only while the read
transaction is open and hands the caller a complete, detached response.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

import anyio
from fastapi import HTTPException, status

from app.cache import ResponseCache
from app.config import get_settings
from app.models import RankingItem, RankingRun, Repository
from app.ranking.filters import RankingFilters
from app.repositories.catalog import CatalogRepository
from app.schemas import RankingResponse
from app.singleflight import (
    FollowerLease,
    ProducerLease,
    SingleFlightBusy,
    SingleFlightLoadCancelled,
    SingleFlightLoadFailed,
)

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "rankings:v6"
_RETRY_AFTER_SECONDS = "5"

ResponseBuilder = Callable[["RankingPage"], RankingResponse]


@dataclass(frozen=True, slots=True)
class RankingRunSnapshot:
    """Detached subset of a ranking run, safe to use after the read release."""

    run_id: int
    published_at: str | None
    fingerprint: str
    config_version: str
    period_days: int
    as_of: datetime
    baseline_at: datetime | None
    generated_at: datetime
    collection_summary: dict[str, Any] | None
    data_mode: Literal["demo", "live"]


@dataclass(frozen=True, slots=True)
class RankingPage:
    """One loaded page plus its metadata, assembled inside the read scope."""

    snapshot: RankingRunSnapshot
    total: int
    rows: list[tuple[RankingItem, Repository]]
    coverage: int
    page: int
    limit: int


def snapshot_run(run: RankingRun) -> RankingRunSnapshot:
    published = run.published_at
    return RankingRunSnapshot(
        run_id=run.id,
        published_at=_utc_isoformat(published) if published is not None else None,
        fingerprint=(run.collection_summary or {}).get("fingerprint", "legacy"),
        config_version=run.config_version,
        period_days=run.period_days,
        as_of=run.as_of,
        baseline_at=run.baseline_at,
        generated_at=published or run.as_of,
        collection_summary=run.collection_summary,
        data_mode="demo" if run.config_version == "demo-v1" else "live",
    )


def _utc_isoformat(value: datetime) -> str:
    """Serialize an instant to UTC so equal instants share one cache key."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def ranking_cache_key(
    snapshot: RankingRunSnapshot, filters: RankingFilters, page: int, limit: int
) -> str:
    """Stable, fixed-length key over the full version and canonical parameters."""
    body = json.dumps(
        {
            "run": snapshot.run_id,
            "published": snapshot.published_at,
            "fingerprint": snapshot.fingerprint,
            "config": snapshot.config_version,
            "period": snapshot.period_days,
            "filters": filters.parameters(),
            "page": page,
            "limit": limit,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{CACHE_NAMESPACE}:{digest}"


class RankingQuery:
    """Merge concurrent ranking misses for the same key behind one budget."""

    def __init__(
        self,
        *,
        repository: CatalogRepository,
        cache: ResponseCache,
        build_response: ResponseBuilder,
    ) -> None:
        self._repository = repository
        self._cache = cache
        self._build_response = build_response

    async def execute(
        self,
        *,
        period: int,
        filters: RankingFilters,
        page: int,
        limit: int,
    ) -> RankingResponse:
        deadline = monotonic() + get_settings().ranking_load_timeout_seconds
        return await self._resolve(period, filters, page, limit, deadline, retry=True)

    async def _resolve(
        self,
        period: int,
        filters: RankingFilters,
        page: int,
        limit: int,
        deadline: float,
        *,
        retry: bool,
    ) -> RankingResponse:
        lease: ProducerLease | FollowerLease | None = None
        snapshot: RankingRunSnapshot | None = None
        key = ""
        try:
            try:
                with anyio.fail_after(_remaining(deadline)):
                    await self._repository.begin_read()
                    run = await self._repository.latest_ranking_run(period)
                    if run is None:
                        raise _not_generated(period)
                    snapshot = snapshot_run(run)
                    key = ranking_cache_key(snapshot, filters, page, limit)
                    cached = await self._cache.get(key)
                    if cached is not None:
                        return RankingResponse.model_validate(cached)
                    try:
                        lease = self._cache.singleflight.claim(key)
                    except SingleFlightBusy:
                        raise _unavailable() from None
            except TimeoutError:
                raise _unavailable() from None
            assert lease is not None and snapshot is not None
            if isinstance(lease, ProducerLease):
                return await self._produce(lease, snapshot, key, filters, page, limit, deadline)
            try:
                return await self._follow(lease, deadline)
            except SingleFlightLoadCancelled:
                pass
        finally:
            await self._repository.release_read()
        if not retry:
            raise _unavailable()
        # The producer was cancelled after it released its read; read the current
        # run again under a fresh transaction and key instead of reusing old ORM.
        return await self._resolve(period, filters, page, limit, deadline, retry=False)

    async def _follow(self, lease: FollowerLease, deadline: float) -> RankingResponse:
        flight = self._cache.singleflight
        try:
            await self._repository.release_read()
            payload = await flight.wait(lease, timeout=_remaining(deadline))
        except (SingleFlightLoadFailed, TimeoutError):
            raise _unavailable() from None
        finally:
            flight.release_follower(lease)
        return RankingResponse.model_validate(payload)

    async def _produce(
        self,
        lease: ProducerLease,
        snapshot: RankingRunSnapshot,
        key: str,
        filters: RankingFilters,
        page: int,
        limit: int,
        deadline: float,
    ) -> RankingResponse:
        flight = self._cache.singleflight
        response: RankingResponse | None = None
        try:
            with anyio.fail_after(_remaining(deadline), shield=True):
                # A different producer may have published between our miss and this
                # lease; re-check under the claim so the stale miss cannot reload.
                cached = await self._cache.get(key)
                if cached is not None:
                    response = RankingResponse.model_validate(cached)
                    payload = cached
                    # Serve the entry as-is; skipping cache.set keeps its TTL.
                    await self._repository.release_read()
                else:
                    total, rows = await self._repository.ranking_page(
                        snapshot.run_id, filters, page=page, limit=limit
                    )
                    coverage = await self._repository.coverage_count()
                    response = self._build_response(
                        RankingPage(
                            snapshot=snapshot,
                            total=total,
                            rows=rows,
                            coverage=coverage,
                            page=page,
                            limit=limit,
                        )
                    )
                    payload = response.model_dump(mode="json")
                    # Data and DTO are complete; end the read before the Redis write
                    # so followers never wait on a row lock.
                    await self._repository.release_read()
                    await self._cache.set(key, payload)
                flight.complete(lease, payload)
        except TimeoutError:
            try:
                await self._repository.release_read()
            finally:
                flight.fail(lease, TimeoutError())
            raise _unavailable() from None
        except anyio.get_cancelled_exc_class() as error:
            try:
                await self._repository.release_read()
            finally:
                flight.fail(lease, error)
            raise
        except Exception as error:
            try:
                await self._repository.release_read()
            finally:
                flight.fail(lease, error)
            logger.exception("Unexpected ranking producer failure")
            raise
        # The shield deferred any outer cancel until the result was published and
        # the cache written; restore that cancellation before reporting success.
        await anyio.lowlevel.checkpoint_if_cancelled()
        assert response is not None
        return response


def _remaining(deadline: float) -> float:
    return max(deadline - monotonic(), 0.0)


def _not_generated(period: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"{period} 天榜单尚未生成",
    )


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="榜单暂时不可用，请稍后重试",
        headers={"Cache-Control": "no-store", "Retry-After": _RETRY_AFTER_SECONDS},
    )
