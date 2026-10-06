from collections import Counter

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import response_cache
from app.models import RankingItem, Repository
from app.ranking.filters import RankingFilters
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
from app.services.ranking_query import RankingPage, RankingQuery
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
        filters = RankingFilters(
            language=language, topic=topic, min_stars=min_stars, query=query
        )
        runner = RankingQuery(
            repository=self._catalog,
            cache=response_cache,
            build_response=self._build_ranking_response,
        )
        return await runner.execute(
            period=int(period), filters=filters, page=page, limit=limit
        )

    def _build_ranking_response(self, page: RankingPage) -> RankingResponse:
        snapshot = page.snapshot
        return RankingResponse(
            data=[
                self._to_ranking_item(item, repository) for item, repository in page.rows
            ],
            meta=RankingMeta(
                period_days=PeriodDays(snapshot.period_days),
                as_of=snapshot.as_of,
                baseline_at=snapshot.baseline_at,
                generated_at=snapshot.generated_at,
                collection=CollectionSummary.model_validate(snapshot.collection_summary)
                if snapshot.collection_summary
                else None,
                coverage=page.coverage,
                total=page.total,
                page=page.page,
                limit=page.limit,
                data_mode=snapshot.data_mode,
            ),
        )

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
