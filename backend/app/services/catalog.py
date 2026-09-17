import asyncio
import logging
from collections import Counter
from math import ceil

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import response_cache
from app.clients.github import (
    GitHubClient,
    GitHubClientError,
    GitHubRateLimitError,
    GitHubReadmeData,
)
from app.config import get_settings
from app.internal.limiter import (
    LeaseDenied,
    LimiterUnavailable,
    limit_failure_response,
    limiter,
    limiter_failure_response,
)
from app.models import RankingItem, Repository
from app.repositories.catalog import CatalogRepository
from app.schemas import (
    ChartRange,
    CollectionSummary,
    FilterOption,
    FilterResponse,
    PeriodDays,
    RankingItemResponse,
    RankingMeta,
    RankingResponse,
    ReadmeResponse,
    RepositoryResponse,
    SnapshotResponse,
    SnapshotSeriesResponse,
)

RANGE_DAYS = {"30d": 30, "90d": 90, "365d": 365}
logger = logging.getLogger(__name__)


class CatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self._catalog = CatalogRepository(session)

    async def rankings(
        self,
        *,
        period: PeriodDays,
        language: str | None,
        topic: str | None,
        min_stars: int,
        query: str | None,
        page: int,
        limit: int,
    ) -> RankingResponse:
        environment = get_settings().environment
        run = await self._catalog.latest_ranking_run(period)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{period} 天榜单尚未生成",
            )

        version = (run.collection_summary or {}).get("fingerprint", "legacy")
        cache_key = (
            f"rankings:v3:{run.id}:{run.published_at}:{version}:{environment}:{period}:"
            f"{language or '-'}:{topic or '-'}:{min_stars}:{query or '-'}:{page}:{limit}"
        )
        cached = await response_cache.get(cache_key)
        if cached:
            return RankingResponse.model_validate(cached)

        rows = await self._catalog.ranking_rows(run.id)
        filtered = [
            self._to_ranking_item(item, repository)
            for item, repository in rows
            if self._matches(repository, language, topic, min_stars, query)
        ]
        offset = (page - 1) * limit
        response = RankingResponse(
            data=filtered[offset : offset + limit],
            meta=RankingMeta(
                period_days=period,
                as_of=run.as_of,
                baseline_at=run.baseline_at,
                generated_at=run.published_at or run.as_of,
                collection=CollectionSummary.model_validate(run.collection_summary)
                if run.collection_summary else None,
                coverage=await self._catalog.coverage_count(),
                total=len(filtered),
                page=page,
                limit=limit,
                data_mode="demo"
                if get_settings().seed_demo_data or environment == "development"
                else "live",
            ),
        )
        await response_cache.set(cache_key, response.model_dump(mode="json"))
        return response

    async def repository(self, owner: str, name: str) -> RepositoryResponse:
        repository = await self._catalog.find_repository(owner, name)
        if repository is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
        return RepositoryResponse.model_validate(repository)

    async def readme(
        self,
        owner: str,
        name: str,
        *,
        client_identity: str | None = None,
    ) -> ReadmeResponse:
        full_name = f"{owner}/{name}"
        cache_key = f"readme:v1:{full_name.lower()}"
        subject = client_identity or "internal"
        cached = await response_cache.get(cache_key)
        if cached:
            try:
                await limiter.record("readme", subject)
            except LeaseDenied as exc:
                raise limit_failure_response(exc) from exc
            except LimiterUnavailable:
                logger.warning("Failed to record cached README limiter request", exc_info=True)
            return ReadmeResponse.model_validate(cached)

        try:
            lease = await limiter.acquire("readme", subject)
        except LeaseDenied as exc:
            raise limit_failure_response(exc) from exc
        except LimiterUnavailable as exc:
            raise limiter_failure_response() from exc

        client: GitHubClient | None = None
        try:
            cached = await response_cache.get(cache_key)
            if cached:
                return ReadmeResponse.model_validate(cached)

            client = GitHubClient()
            try:
                readme = await self._read_github_readme(client, full_name)
            except GitHubRateLimitError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="GitHub README 暂时无法获取",
                    headers={
                        "Cache-Control": "no-store",
                        "Retry-After": str(ceil(exc.retry_after)),
                    },
                ) from exc
            except GitHubClientError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="README 暂不可用",
                ) from exc
        finally:
            if client is not None:
                client.close()
            try:
                await limiter.release(lease)
            except LimiterUnavailable:
                logger.warning("Failed to release README limiter lease", exc_info=True)

        response = ReadmeResponse(
            repository=readme.repository,
            path=readme.path,
            content=readme.content,
            html_url=readme.html_url,
        )
        await response_cache.set(cache_key, response.model_dump(mode="json"), ttl_seconds=3600)
        return response

    @staticmethod
    async def _read_github_readme(client: GitHubClient, full_name: str) -> GitHubReadmeData:
        # asyncio.to_thread cannot be interrupted safely; keep the lease until
        # the worker thread has completed its network call.
        task = asyncio.create_task(asyncio.to_thread(client.readme, full_name))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def snapshot_series(
        self, owner: str, name: str, range_name: ChartRange
    ) -> SnapshotSeriesResponse:
        repository = await self._catalog.find_repository(owner, name)
        if repository is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
        snapshots = await self._catalog.snapshots(repository.id, RANGE_DAYS[range_name])
        return SnapshotSeriesResponse(
            repository=repository.full_name,
            range=range_name,
            data=[
                SnapshotResponse(
                    captured_at=snapshot.captured_at,
                    stars_count=snapshot.stars_count,
                    forks_count=snapshot.forks_count,
                )
                for snapshot in snapshots
            ],
        )

    async def filters(self) -> FilterResponse:
        languages = await self._catalog.language_counts()
        topic_counts = Counter(
            topic for topics in await self._catalog.all_topics() for topic in topics
        )
        return FilterResponse(
            languages=[
                FilterOption(value=value, label=value, count=count) for value, count in languages
            ],
            topics=[
                FilterOption(value=value, label=value, count=count)
                for value, count in topic_counts.most_common(20)
            ],
        )

    @staticmethod
    def _matches(
        repository: Repository,
        language: str | None,
        topic: str | None,
        min_stars: int,
        query: str | None,
    ) -> bool:
        repo_language = repository.language
        if language and (repo_language or "").lower() != language.lower():
            return False
        if topic and topic.lower() not in [item.lower() for item in repository.topics]:
            return False
        if repository.stars_count < min_stars:
            return False
        if query:
            haystack = f"{repository.full_name} {repository.description or ''}"
            if query.lower() not in haystack.lower():
                return False
        return True

    @staticmethod
    def _to_ranking_item(item: RankingItem, repository: Repository) -> RankingItemResponse:
        return RankingItemResponse(
            rank=item.rank,
            previous_rank=item.previous_rank,
            full_name=repository.full_name,
            owner=repository.owner,
            owner_github_id=repository.owner_github_id,
            name=repository.name,
            description=repository.description,
            language=repository.language,
            topics=repository.topics,
            total_stars=item.end_stars,
            star_delta=item.net_delta,
            growth_rate=item.growth_rate,
            baseline_available=item.baseline_available,
            last_updated_at=repository.pushed_at,
            github_url=repository.html_url,
        )
