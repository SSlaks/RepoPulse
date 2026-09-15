from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import RankingItem, RankingRun, Repository, RepoSnapshot


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_ranking_run(self, period_days: int) -> RankingRun | None:
        query = (
            select(RankingRun)
            .where(RankingRun.period_days == period_days, RankingRun.status == "ready")
            .order_by(RankingRun.as_of.desc())
            .limit(1)
            .with_for_update(read=True)
        )
        return await self._session.scalar(query)

    async def ranking_rows(self, run_id: int) -> list[tuple[RankingItem, Repository]]:
        query = (
            select(RankingItem, Repository)
            .join(Repository, RankingItem.repository_id == Repository.id)
            .where(RankingItem.ranking_run_id == run_id)
            .order_by(RankingItem.rank)
        )
        return list((await self._session.execute(query)).tuples())

    async def coverage_count(self) -> int:
        query = select(func.count(Repository.id)).where(
            Repository.is_fork.is_(False),
            Repository.archived.is_(False),
            Repository.disabled.is_(False),
        )
        return int(await self._session.scalar(query) or 0)

    async def find_repository(self, owner: str, name: str) -> Repository | None:
        query = select(Repository).where(
            func.lower(Repository.owner) == owner.lower(),
            func.lower(Repository.name) == name.lower(),
        )
        return await self._session.scalar(query)

    async def snapshots(self, repository_id: int, range_days: int) -> list[RepoSnapshot]:
        since = datetime.now(UTC) - timedelta(days=range_days)
        query = (
            select(RepoSnapshot)
            .where(
                RepoSnapshot.repository_id == repository_id,
                RepoSnapshot.captured_at >= since,
            )
            .order_by(RepoSnapshot.captured_at)
        )
        return list((await self._session.scalars(query)).all())

    async def all_repositories_with_snapshots(self) -> list[Repository]:
        query = select(Repository).options(selectinload(Repository.snapshots))
        return list((await self._session.scalars(query)).all())

    async def language_counts(self) -> list[tuple[str, int]]:
        query = (
            select(Repository.language, func.count(Repository.id))
            .where(Repository.language.is_not(None))
            .group_by(Repository.language)
            .order_by(func.count(Repository.id).desc(), Repository.language)
        )
        rows = (await self._session.execute(query)).all()
        return [(str(language), int(count)) for language, count in rows]

    async def all_topics(self) -> list[list[str]]:
        return list((await self._session.scalars(select(Repository.topics))).all())
