import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import anyio
from sqlalchemy import func, select, text, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import RankingItem, RankingRun, Repository, RepoSnapshot
from app.ranking.filters import MAX_SQL_INT, RankingFilters

logger = logging.getLogger(__name__)

_RELEASE_TIMEOUT_SECONDS = 5.0
_INVALIDATE_TIMEOUT_SECONDS = 1.0


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
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(query)

    async def begin_read(self) -> None:
        """Open the shared read transaction that covers the run and its page.

        SQLite's legacy driver mode does not start a transaction for SELECTs, so
        an explicit ``BEGIN`` pins run selection and page reads to one snapshot.
        PostgreSQL starts its transaction on the first statement, which keeps the
        ``FOR SHARE`` lock acquired by :meth:`latest_ranking_run` until release.
        The method is a no-op when the caller already opened a transaction.
        """
        if self._session.in_transaction():
            return
        if self._session.get_bind().dialect.name == "sqlite":
            await self._session.execute(text("BEGIN"))

    async def release_read(self) -> None:
        """Roll back the read-only ranking transaction and clear session state.

        The ranking read never writes, so rollback is the correct release: it
        discards the snapshot and any accidental pending state instead of
        committing it. Shielded from cancellation and bounded; a failed or
        timed-out rollback invalidates the session under its own shielded budget
        and then re-raises, so a dirty connection is never reported as success.
        """
        if not self._session.in_transaction():
            return
        try:
            with anyio.CancelScope(shield=True), anyio.fail_after(_RELEASE_TIMEOUT_SECONDS):
                await self._session.rollback()
        except (SQLAlchemyError, TimeoutError):
            await self._invalidate_session()
            raise

    async def _invalidate_session(self) -> None:
        try:
            with anyio.CancelScope(shield=True), anyio.move_on_after(_INVALIDATE_TIMEOUT_SECONDS):
                await self._session.invalidate()
        except BaseException:
            logger.warning("Ranking read session invalidation failed", exc_info=True)

    @asynccontextmanager
    async def ranking_read(self) -> AsyncIterator[None]:
        """Pin ranking page reads to one transaction and release it afterwards."""
        await self.begin_read()
        try:
            yield
        finally:
            await self.release_read()

    async def ranking_page(
        self,
        run_id: int,
        filters: RankingFilters,
        *,
        page: int,
        limit: int,
    ) -> tuple[int, list[tuple[RankingItem, Repository]]]:
        """Return the filtered total and current page read from one snapshot.

        The filtered relation feeds a shared COUNT and the ordered page
        identifiers in one statement that also resolves the display entities,
        so the total, rank and repository metadata can never disagree. An
        oversized page offset is clamped to the largest bindable integer, which
        still yields an empty page instead of overflowing the bind.
        """
        dialect = self._session.get_bind().dialect.name
        offset = min((page - 1) * limit, MAX_SQL_INT)
        filtered = (
            select(
                RankingItem.id.label("item_id"),
                RankingItem.rank.label("item_rank"),
            )
            .join(Repository, RankingItem.repository_id == Repository.id)
            .where(RankingItem.ranking_run_id == run_id, *filters.predicates(dialect))
        )
        filtered_relation = filtered.cte("filtered_ranking")
        totals = (
            select(func.count().label("total"))
            .select_from(filtered_relation)
            .subquery("filtered_totals")
        )
        page_rows = (
            select(filtered_relation.c.item_id, filtered_relation.c.item_rank)
            .order_by(filtered_relation.c.item_rank)
            .limit(limit)
            .offset(offset)
            .subquery("page_rows")
        )
        combined = (
            select(totals.c.total, RankingItem, Repository)
            .select_from(totals)
            .outerjoin(page_rows, true())
            .outerjoin(RankingItem, page_rows.c.item_id == RankingItem.id)
            .outerjoin(Repository, RankingItem.repository_id == Repository.id)
            .order_by(page_rows.c.item_rank)
        )
        result = (await self._session.execute(combined)).all()
        total = int(result[0][0]) if result else 0
        rows = [
            (item, repository)
            for _total, item, repository in result
            if item is not None and repository is not None
        ]
        return total, rows

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
