"""Read-only README serving backed by the durable repository cache."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from math import ceil

from fastapi import HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.cache import response_cache
from app.config import get_settings
from app.internal.limiter import (
    LeaseDenied,
    LimiterUnavailable,
    limit_failure_response,
    limiter,
)
from app.models import Repository, RepositoryReadme
from app.readme_cache import readme_cache_key
from app.schemas import ReadmeResponse
from app.task_queue import enqueue_readme_refresh

logger = logging.getLogger(__name__)


class RepositoryReadmeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, owner: str, name: str, *, client_identity: str | None) -> ReadmeResponse:
        repository = await self._repository(owner, name)
        if repository is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
        readme = await self._readme(repository.id)
        if readme is None:
            return await self._queue_missing(repository.id, None)
        if readme.is_private or readme.visibility.lower() == "private":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
        if readme.visibility.lower() != "public" or readme.last_public_verified_at is None:
            return await self._queue_missing(repository.id, readme)

        cache_key = readme_cache_key(repository.id)
        cached = await self._cache_get(cache_key)
        cached_success = cached.get("_last_success_at") if cached else None
        current_success = _timestamp(readme.last_success_at)
        if cached and cached.get("content") and cached_success == current_success:
            response = ReadmeResponse.model_validate(cached)
            await self._record_cached_request(client_identity)
            if _is_due(readme.next_refresh_at):
                enqueue_readme_refresh(repository.id)
            return response
        await self._session.refresh(readme, ["content"])
        if readme.content:
            response = ReadmeResponse(
                repository=repository.full_name,
                path=readme.path or "README.md",
                content=readme.content,
                html_url=readme.html_url or repository.html_url,
            )
            await self._cache_set(
                cache_key,
                {
                    **response.model_dump(mode="json"),
                    "_last_success_at": current_success,
                },
                ttl_seconds=get_settings().readme_cache_ttl_seconds,
            )
            await self._record_cached_request(client_identity)
            if _is_due(readme.next_refresh_at):
                enqueue_readme_refresh(repository.id)
            return response
        return await self._queue_missing(repository.id, readme)

    async def _repository(self, owner: str, name: str) -> Repository | None:
        return await self._session.scalar(
            select(Repository).where(
                func.lower(Repository.owner) == owner.lower(),
                func.lower(Repository.name) == name.lower(),
            )
        )

    async def _readme(self, repository_id: int) -> RepositoryReadme | None:
        return await self._session.scalar(
            select(RepositoryReadme)
            .options(defer(RepositoryReadme.content), defer(RepositoryReadme.root_entries))
            .where(RepositoryReadme.repository_id == repository_id)
        )

    async def _queue_missing(
        self, repository_id: int, readme: RepositoryReadme | None
    ) -> ReadmeResponse:
        if readme and readme.next_refresh_at and not _is_due(readme.next_refresh_at):
            retry_after = max(1, ceil((_as_utc(readme.next_refresh_at) - datetime.now(UTC)).total_seconds()))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="README 暂时无法更新，系统会自动重试",
                headers={"Cache-Control": "no-store", "Retry-After": str(retry_after)},
            )
        if not enqueue_readme_refresh(repository_id):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="README 暂时不可用，请稍后重试",
                headers={"Cache-Control": "no-store", "Retry-After": "120"},
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="README 正在准备，请稍后刷新",
            headers={"Cache-Control": "no-store", "Retry-After": "60"},
        )

    async def _cache_get(self, key: str) -> dict[str, object] | None:
        try:
            return await response_cache.get(key)
        except (RedisError, OSError, ValueError):
            logger.warning("README response cache unavailable", exc_info=True)
            return None

    async def _cache_set(self, key: str, value: dict[str, object], *, ttl_seconds: int) -> None:
        try:
            await response_cache.set(key, value, ttl_seconds=ttl_seconds)
        except (RedisError, OSError, ValueError):
            logger.warning("README response cache write skipped", exc_info=True)

    async def _record_cached_request(self, client_identity: str | None) -> None:
        subject = client_identity or "internal"
        try:
            await limiter.record("readme", subject)
        except LeaseDenied as exc:
            raise limit_failure_response(exc) from exc
        except LimiterUnavailable:
            # The durable body remains readable when Redis is unavailable.
            logger.warning("Failed to record cached README limiter request", exc_info=True)


def _is_due(value: datetime | None) -> bool:
    if value is None:
        return True
    current = value if value.tzinfo else value.replace(tzinfo=UTC)
    return current <= datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _timestamp(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value else None
