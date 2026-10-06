"""Ranking generation: batch snapshot inputs and persist period rankings.

A publication day only needs the five calendar days the four supported periods
can consume -- the publication day itself plus D-1, D-7, D-14 and D-30. Those
``github_api`` snapshots are loaded once in repository-ID batches, and the same
prepared ``RepositorySeries`` collection is reused by every period.
"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta

from app.models import RankingItem, RankingRun, Repository, RepoSnapshot
from app.ranking.calculator import (
    RepositorySeries,
    SnapshotPoint,
    calculate_ranking,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

BATCH_SIZE = 500
_PERIOD_OFFSETS = (0, 1, 7, 14, 30)


def required_snapshot_dates(as_of: datetime) -> tuple[date, ...]:
    """Return the calendar days the supported periods can read for ``as_of``."""
    return tuple((as_of - timedelta(days=offset)).date() for offset in _PERIOD_OFFSETS)


def load_repository_series(
    session: Session,
    as_of: datetime,
    repository_ids: list[int] | None = None,
) -> list[RepositorySeries]:
    """Load the required snapshot points, batched by repository ID.

    Explicit ``repository_ids`` keep publication's frozen cohort. Without them,
    independent callers get the default eligible cohort so the standalone
    generation path keeps its fork/archived/disabled/quarantined exclusions.
    """
    days = required_snapshot_dates(as_of)
    as_of_day = as_of.date()
    series: list[RepositorySeries] = []
    for batch in _repository_batches(session, repository_ids):
        names = dict(batch)
        points: dict[int, list[SnapshotPoint]] = {repository_id: [] for repository_id in names}
        rows = session.execute(
            select(
                RepoSnapshot.repository_id,
                RepoSnapshot.snapshot_date,
                RepoSnapshot.stars_count,
            )
            .where(
                RepoSnapshot.repository_id.in_(list(names)),
                RepoSnapshot.snapshot_date.in_(days),
                RepoSnapshot.source == "github_api",
            )
            .order_by(RepoSnapshot.repository_id, RepoSnapshot.snapshot_date)
        ).tuples().all()
        for repository_id, snapshot_date, stars_count in rows:
            points[repository_id].append(
                SnapshotPoint(datetime.combine(snapshot_date, as_of.timetz()), stars_count)
            )
        for repository_id, repository_points in points.items():
            if not any(point.captured_at.date() == as_of_day for point in repository_points):
                continue
            series.append(
                RepositorySeries(
                    repository_id=repository_id,
                    full_name=names[repository_id],
                    snapshots=tuple(repository_points),
                    current_stars=None,
                )
            )
    return series


def _repository_batches(
    session: Session, repository_ids: list[int] | None
) -> Iterator[list[tuple[int, str]]]:
    if repository_ids is not None:
        ordered = sorted(set(repository_ids))
        for start in range(0, len(ordered), BATCH_SIZE):
            yield _select_repositories(session, ordered[start:start + BATCH_SIZE])
        return
    base = (
        select(Repository.id, Repository.full_name)
        .where(
            Repository.is_fork.is_(False),
            Repository.archived.is_(False),
            Repository.disabled.is_(False),
            Repository.availability_status != "quarantined",
        )
        .order_by(Repository.id)
    )
    last_id = 0
    while True:
        rows = session.execute(
            base.where(Repository.id > last_id).limit(BATCH_SIZE)
        ).tuples().all()
        if not rows:
            return
        yield list(rows)
        last_id = rows[-1][0]


def _select_repositories(session: Session, repository_ids: list[int]) -> list[tuple[int, str]]:
    return list(
        session.execute(
            select(Repository.id, Repository.full_name)
            .where(Repository.id.in_(repository_ids))
            .order_by(Repository.id)
        ).tuples()
    )


def persist_period(
    session: Session,
    period_days: int,
    as_of: datetime,
    series: list[RepositorySeries] | None = None,
) -> int:
    """Persist one ranking period, reusing prepared series when supplied."""
    prepared = series if series is not None else load_repository_series(session, as_of)
    results = calculate_ranking(prepared, period_days, as_of)
    previous_run = session.scalar(
        select(RankingRun)
        .where(RankingRun.period_days == period_days, RankingRun.as_of < as_of)
        .order_by(RankingRun.as_of.desc())
        .limit(1)
    )
    previous_ranks: dict[int, int] = (
        dict(
            session.execute(
                select(RankingItem.repository_id, RankingItem.rank).where(
                    RankingItem.ranking_run_id == previous_run.id
                )
            ).tuples()
        )
        if previous_run
        else {}
    )
    run = RankingRun(
        period_days=period_days,
        as_of=as_of,
        baseline_at=as_of - timedelta(days=period_days),
        config_version="v1",
        status="ready",
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            RankingItem(
                ranking_run_id=run.id,
                repository_id=result.repository_id,
                rank=rank,
                previous_rank=previous_ranks.get(result.repository_id),
                start_stars=result.start_stars,
                end_stars=result.end_stars,
                net_delta=result.net_delta,
                growth_rate=result.growth_rate,
                baseline_available=result.baseline_available,
            )
            for rank, result in enumerate(results, start=1)
        ]
    )
    return len(results)
