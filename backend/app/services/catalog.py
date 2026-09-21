from collections import Counter

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import response_cache
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
from app.services.readme import RepositoryReadmeService

RANGE_DAYS = {"30d": 30, "90d": 90, "365d": 365}


class CatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self._catalog = CatalogRepository(session)
        self._readmes = RepositoryReadmeService(session)

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
        run = await self._catalog.latest_ranking_run(period)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{period} 天榜单尚未生成",
            )

        version = (run.collection_summary or {}).get("fingerprint", "legacy")
        cache_key = (
            f"rankings:v4:{run.id}:{run.published_at}:{version}:{run.config_version}:{period}:"
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
                data_mode="demo" if run.config_version == "demo-v1" else "live",
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
        return await self._readmes.get(owner, name, client_identity=client_identity)

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
