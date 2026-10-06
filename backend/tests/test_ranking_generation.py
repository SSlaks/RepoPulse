from datetime import UTC, datetime, timedelta

import pytest
from app.models import (
    Base,
    JobRun,
    RankingItem,
    RankingRun,
    Repository,
    RepoSnapshot,
    SnapshotRequest,
)
from app.ranking.calculator import RepositorySeries, SnapshotPoint, calculate_ranking
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from worker.app import rankings, tasks

NOW = datetime(2026, 9, 10, 2, tzinfo=UTC)
REQUIRED_OFFSETS = (0, 1, 7, 14, 30)


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'generation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def seed_repository(session, index, snapshots, **flags):
    repository = Repository(
        github_id=index,
        full_name=f"owner/repo{index}",
        owner="owner",
        name=f"repo{index}",
        html_url=f"https://github.com/owner/repo{index}",
        first_tracked_at=NOW,
        last_seen_at=NOW,
        history_available_from=NOW,
        **flags,
    )
    session.add(repository)
    session.flush()
    for offset, stars, source in snapshots:
        session.add(RepoSnapshot(
            repository_id=repository.id,
            snapshot_date=(NOW - timedelta(days=offset)).date(),
            captured_at=NOW - timedelta(days=offset),
            stars_count=stars,
            forks_count=0,
            source=source,
        ))
    return repository


def legacy_series(session, as_of, repository_ids=None):
    """Reconstruct the previous full-history load for equivalence checks only."""
    query = select(Repository)
    if repository_ids is not None:
        query = query.where(Repository.id.in_(repository_ids))
    else:
        query = query.where(
            Repository.is_fork.is_(False),
            Repository.archived.is_(False),
            Repository.disabled.is_(False),
            Repository.availability_status != "quarantined",
        )
    series = []
    for repository in session.scalars(query):
        snapshots = session.scalars(
            select(RepoSnapshot)
            .where(
                RepoSnapshot.repository_id == repository.id,
                RepoSnapshot.snapshot_date <= as_of.date(),
                RepoSnapshot.source == "github_api",
            )
            .order_by(RepoSnapshot.captured_at)
        ).all()
        if not any(snapshot.snapshot_date == as_of.date() for snapshot in snapshots):
            continue
        series.append(RepositorySeries(
            repository_id=repository.id,
            full_name=repository.full_name,
            snapshots=tuple(
                SnapshotPoint(
                    datetime.combine(snapshot.snapshot_date, as_of.timetz()),
                    snapshot.stars_count,
                )
                for snapshot in snapshots
            ),
            current_stars=None,
        ))
    return series


def seed_publication(factory, now, count=3):
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = "completed"
        job.progress = {"membership_frozen": True}
        for index in range(1, count + 1):
            session.add(SnapshotRequest(job_run_id=job.id, repository_id=index, status="saved"))
            session.add(RepoSnapshot(
                repository_id=index,
                snapshot_date=now.date(),
                captured_at=now,
                stars_count=200 + index,
                forks_count=1,
                source="github_api",
            ))
        session.commit()


def snapshot_reads(engine):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "repo_snapshots" in statement and "stars_count" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


def test_required_dates_are_publication_day_and_four_offsets():
    assert rankings.required_snapshot_dates(NOW) == tuple(
        (NOW - timedelta(days=offset)).date() for offset in REQUIRED_OFFSETS
    )


def test_five_date_loading_matches_full_history_for_all_periods(database):
    history = [
        (0, 300, "github_api"), (1, 290, "github_api"), (2, 285, "github_api"),
        (3, 280, "github_api"), (5, 275, "github_api"), (6, 270, "github_api"),
        (7, 260, "github_api"), (9, 250, "github_api"), (13, 240, "github_api"),
        (14, 235, "github_api"), (20, 220, "github_api"), (29, 210, "github_api"),
        (30, 200, "github_api"), (45, 150, "github_api"), (60, 100, "github_api"),
    ]
    with database() as session:
        seed_repository(session, 1, history)
        session.commit()
        loaded = rankings.load_repository_series(session, NOW, [1])
        legacy = legacy_series(session, NOW, [1])
        assert len(loaded) == len(legacy) == 1
        assert len(loaded[0].snapshots) == len(REQUIRED_OFFSETS)
        assert len(legacy[0].snapshots) == len(history)
        for period in (1, 7, 14, 30):
            assert calculate_ranking(loaded, period, NOW) == calculate_ranking(
                legacy, period, NOW)
        for period in (1, 7, 14, 30):
            tasks._persist_ranking(session, period, NOW, loaded)
        session.commit()
        for period in (1, 7, 14, 30):
            run = session.scalar(select(RankingRun).where(RankingRun.period_days == period))
            item = session.scalar(select(RankingItem).where(RankingItem.ranking_run_id == run.id))
            expected = calculate_ranking(legacy, period, NOW)[0]
            assert item.start_stars == expected.start_stars
            assert item.end_stars == expected.end_stars
            assert item.net_delta == expected.net_delta
            assert item.growth_rate == expected.growth_rate
            assert item.baseline_available == expected.baseline_available


def test_only_required_dates_are_loaded_from_long_history(database):
    history = [(offset, 1000 - offset, "github_api") for offset in range(75)]
    with database() as session:
        seed_repository(session, 1, history)
        session.commit()
        loaded = rankings.load_repository_series(session, NOW, [1])
    days = {point.captured_at.date() for point in loaded[0].snapshots}
    assert days == {(NOW - timedelta(days=offset)).date() for offset in REQUIRED_OFFSETS}
    assert len(loaded[0].snapshots) == len(REQUIRED_OFFSETS)


def test_series_without_publication_day_snapshot_is_excluded(database):
    with database() as session:
        seed_repository(session, 1, [(1, 100, "github_api"), (7, 90, "github_api")])
        seed_repository(session, 2, [(0, 200, "github_api")])
        session.commit()
        loaded = rankings.load_repository_series(session, NOW, [1, 2])
    assert [item.repository_id for item in loaded] == [2]


def test_missing_baseline_reports_no_growth(database):
    with database() as session:
        seed_repository(session, 1, [(0, 150, "github_api")])
        session.commit()
        results = calculate_ranking(rankings.load_repository_series(session, NOW, [1]), 7, NOW)
    assert results[0].baseline_available is False
    assert results[0].net_delta == 0
    assert results[0].start_stars == results[0].end_stars == 150


def test_zero_baseline_keeps_delta_without_growth_rate(database):
    with database() as session:
        seed_repository(session, 1, [(0, 40, "github_api"), (7, 0, "github_api")])
        session.commit()
        results = calculate_ranking(rankings.load_repository_series(session, NOW, [1]), 7, NOW)
    assert results[0].baseline_available is True
    assert results[0].start_stars == 0
    assert results[0].net_delta == 40
    assert results[0].growth_rate is None


def test_demo_source_is_excluded_from_baselines_and_endpoints(database):
    with database() as session:
        seed_repository(session, 1, [(0, 130, "github_api"), (7, 100, "demo")])
        seed_repository(session, 2, [(0, 130, "demo")])
        session.commit()
        loaded = rankings.load_repository_series(session, NOW, [1, 2])
        results = calculate_ranking(loaded, 7, NOW)
    assert [item.repository_id for item in loaded] == [1]
    assert results[0].baseline_available is False
    assert results[0].net_delta == 0


def test_default_cohort_excludes_ineligible_and_explicit_ids_do_not(database):
    with database() as session:
        seed_repository(session, 1, [(0, 100, "github_api")])
        seed_repository(session, 2, [(0, 100, "github_api")], is_fork=True)
        seed_repository(session, 3, [(0, 100, "github_api")], archived=True)
        seed_repository(session, 4, [(0, 100, "github_api")], disabled=True)
        seed_repository(session, 5, [(0, 100, "github_api")], availability_status="quarantined")
        session.commit()
        default_ids = [item.repository_id for item in rankings.load_repository_series(session, NOW)]
        explicit_ids = [item.repository_id for item in rankings.load_repository_series(
            session, NOW, [1, 2, 3, 4, 5])]
    assert default_ids == [1]
    assert explicit_ids == [1, 2, 3, 4, 5]


def test_repository_ids_are_loaded_in_batches_of_500(database):
    with database() as session:
        session.add_all([
            Repository(
                github_id=index, full_name=f"owner/repo{index}", owner="owner",
                name=f"repo{index}", html_url=f"https://github.com/owner/repo{index}",
                first_tracked_at=NOW, last_seen_at=NOW, history_available_from=NOW,
            )
            for index in range(1, 502)
        ])
        session.flush()
        session.add_all([
            RepoSnapshot(
                repository_id=index, snapshot_date=NOW.date(), captured_at=NOW,
                stars_count=index, forks_count=0, source="github_api",
            )
            for index in range(1, 502)
        ])
        session.commit()
    engine = database.kw["bind"]
    reads, stop = snapshot_reads(engine)
    try:
        with database() as session:
            loaded = rankings.load_repository_series(session, NOW, list(range(1, 502)))
    finally:
        stop()
    assert rankings.BATCH_SIZE == 500
    assert len(loaded) == 501
    assert len(reads) == 2


def test_publication_loads_once_and_reuses_series_for_four_periods(environment, monkeypatch):
    factory, now = environment
    seed_publication(factory, now)
    engine = factory.kw["bind"]
    reads, stop = snapshot_reads(engine)
    calls = []
    original = rankings.calculate_ranking

    def spy(series, period, as_of):
        calls.append(series)
        return original(series, period, as_of)

    monkeypatch.setattr(rankings, "calculate_ranking", spy)
    try:
        assert tasks.publish_daily_rankings(now.isoformat())["count"] == 12
    finally:
        stop()
    assert len(reads) == 1
    assert len(calls) == 4
    assert all(call is calls[0] for call in calls)


def test_publication_rolls_back_every_period_on_failure(environment, monkeypatch):
    factory, now = environment
    seed_publication(factory, now)
    original = tasks._persist_ranking

    def fail(session, period, as_of, series):
        if period == 7:
            raise RuntimeError("simulated failure")
        return original(session, period, as_of, series)

    monkeypatch.setattr(tasks, "_persist_ranking", fail)
    with pytest.raises(RuntimeError):
        tasks.publish_daily_rankings(now.isoformat())
    with factory() as session:
        assert not session.scalars(select(RankingRun)).all()
