from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from app.config import Settings
from app.models import JobRun, RankingItem, RankingRun, RepoSnapshot, SnapshotRequest
from sqlalchemy import select
from test_collection import repository_data

from worker.app import tasks
from worker.app.publication import candidate, needs_publication
from worker.app.recovery import dispatch_recovery


def seed_cohort(factory, now, total=20, saved=19, status="partial"):
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = status
        job.progress = {"membership_frozen": True, "as_of": now.isoformat(),
                        "deadline": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
        for index in range(1, total + 1):
            tasks._upsert_repository(session, repository_data(index))
            session.add(SnapshotRequest(job_run_id=job.id, repository_id=index,
                                        status="saved" if index <= saved else "failed",
                                        error_code=None if index <= saved else "404"))
            if index <= saved:
                session.add(RepoSnapshot(repository_id=index, snapshot_date=now.date(),
                                         captured_at=now, stars_count=200+index, forks_count=1,
                                         source="github_api"))
        session.commit()


@pytest.mark.parametrize("saved,total,allowed", [(9499, 10000, False), (95, 100, True),
                                                 (100, 100, True), (0, 0, False)])
def test_exact_threshold(saved, total, allowed):
    session = Mock()
    session.scalars.side_effect = [range(total), range(saved)]
    job = JobRun(id=1, status="partial", progress={}, cancel_requested=False)
    result = candidate(session, job, datetime.now(UTC), 95)
    assert (result.reason is None) == allowed


@pytest.mark.parametrize("status,progress,cancel", [
    ("failed", {}, False), ("partial", {"global_failure": "401"}, False),
    ("partial", {}, True), ("running", {}, False),
])
def test_global_failure_and_cancel_block_even_at_full_coverage(status, progress, cancel):
    session = Mock()
    session.scalars.side_effect = [range(20), range(20)]
    job = JobRun(id=1, status=status, progress=progress, cancel_requested=cancel)
    assert candidate(session, job, datetime.now(UTC), 95).reason is not None


def test_partial_then_complete_replaces_all_periods(environment):
    factory, now = environment
    seed_cohort(factory, now)
    assert tasks.publish_daily_rankings(now.isoformat())["count"] == 76
    with factory() as session:
        runs = session.scalars(select(RankingRun)).all()
        assert len(runs) == 4
        assert all(run.collection_summary["completeness_percent"] == 95 for run in runs)
        assert all(run.collection_summary["missing"] == 1 for run in runs)
        first_version = runs[0].collection_summary["fingerprint"]
        assert session.scalar(select(JobRun.status)) == "partial"
        assert not session.scalars(select(RankingItem).where(RankingItem.repository_id == 20)).all()
        session.add(RepoSnapshot(repository_id=20, snapshot_date=now.date(), captured_at=now,
                                 stars_count=220, forks_count=1, source="github_api"))
        # Deliberately leave the request marked failed: real snapshots are authoritative.
        session.commit()
    assert tasks.publish_daily_rankings(now.isoformat())["count"] == 80
    assert tasks.publish_daily_rankings(now.isoformat())["status"] == "duplicate"
    with factory() as session:
        runs = session.scalars(select(RankingRun)).all()
        assert len(runs) == 4
        assert all(run.collection_summary["is_partial"] is False for run in runs)
        assert all(run.collection_summary["fingerprint"] != first_version for run in runs)
        assert all(run.published_at for run in runs)


def test_failed_update_keeps_previous_batch_and_version_budget(environment, monkeypatch):
    factory, now = environment
    seed_cohort(factory, now)
    tasks.publish_daily_rankings(now.isoformat())
    with factory() as session:
        session.add(RepoSnapshot(repository_id=20, snapshot_date=now.date(), captured_at=now,
                                 stars_count=220, forks_count=1, source="github_api"))
        session.commit()
    original = tasks._persist_ranking
    def fail(session, period, as_of, ids):
        if period == 7:
            raise RuntimeError("simulated failure")
        return original(session, period, as_of, ids)
    monkeypatch.setattr(tasks, "_persist_ranking", fail)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            tasks.publish_daily_rankings(now.isoformat(), automatic=True)
    assert tasks.publish_daily_rankings(now.isoformat(), automatic=True)["status"] == "failed"
    with factory() as session:
        runs = session.scalars(select(RankingRun)).all()
        assert len(runs) == 4
        assert all(run.collection_summary["succeeded"] == 19 for run in runs)
        assert len(session.scalars(select(RankingItem)).all()) == 76
        job = session.scalar(select(JobRun))
        assert sorted(job.progress["publication_attempts_by_version"].values()) == [1, 5]


def test_recovery_publishes_after_deadline_without_recapturing(environment, monkeypatch):
    factory, now = environment
    seed_cohort(factory, now)
    publish, capture = Mock(), Mock()
    monkeypatch.setattr(tasks.publish_daily_rankings, "delay", publish)
    monkeypatch.setattr(tasks.capture_daily_snapshots, "delay", capture)
    assert dispatch_recovery() == 1
    publish.assert_called_once_with(now.isoformat(), automatic=True)
    capture.assert_not_called()


def test_small_access_anomaly_does_not_block_qualified_candidate(environment):
    factory, now = environment
    seed_cohort(factory, now)
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.progress = dict(job.progress, access_anomaly=True)
        result = candidate(session, job, now, 95)
        assert needs_publication(job, result)


def test_explicit_restart_clears_global_failure_only(environment):
    factory, now = environment
    seed_cohort(factory, now, status="failed")
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.progress = dict(job.progress, global_failure="401",
                            publication_attempts_by_version={"old": 5})
        key = job.job_key
        session.commit()
    assert tasks.capture_daily_snapshots.run(now.isoformat(), automatic=True)["status"] != "ok"
    assert tasks._start_job(key, "capture_daily_snapshots", clear_failure=True)
    with factory() as session:
        progress = session.scalar(select(JobRun.progress))
        assert "global_failure" not in progress
        assert progress["publication_attempts_by_version"] == {"old": 5}


@pytest.mark.parametrize("value", [0, 101])
def test_threshold_config_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        Settings(ranking_min_completeness_percent=value)


def test_cancel_between_attempt_reservation_and_publication(environment, monkeypatch):
    factory, now = environment
    seed_cohort(factory, now)
    from worker.app import publication
    original = publication._locked_job
    calls = 0
    def lock(session, key):
        nonlocal calls
        calls += 1
        if calls == 2:
            with factory() as other:
                job = other.scalar(select(JobRun))
                job.cancel_requested = True
                other.commit()
        return original(session, key)
    monkeypatch.setattr(publication, "_locked_job", lock)
    assert tasks.publish_daily_rankings(now.isoformat())["status"] == "blocked"
    with factory() as session:
        assert not session.scalars(select(RankingRun)).all()
        assert session.scalar(select(JobRun.cancel_requested))


@pytest.mark.asyncio
async def test_api_metadata_is_versioned_and_independent_of_filters(environment, monkeypatch):
    factory, now = environment
    seed_cohort(factory, now)
    tasks.publish_daily_rankings(now.isoformat())
    from app.services.catalog import CatalogService
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    class Cache:
        def __init__(self):
            self.values = {}
        async def get(self, key):
            return self.values.get(key)
        async def set(self, key, value):
            self.values[key] = value
    cache = Cache()
    monkeypatch.setattr("app.services.catalog.response_cache", cache)
    engine = create_async_engine(str(factory.kw["bind"].url).replace("sqlite:", "sqlite+aiosqlite:"))
    async def read(query=None, page=1):
        async with AsyncSession(engine) as session:
            return await CatalogService(session).rankings(period=1, language=None, topic=None,
                min_stars=0, query=query, page=page, limit=5)
    try:
        first = await read()
        filtered = await read("repo1", 2)
        assert first.meta.collection == filtered.meta.collection
        assert first.meta.collection.succeeded == 19
        assert "fingerprint" not in first.meta.collection.model_dump()
        with factory() as session:
            session.add(RepoSnapshot(repository_id=20, snapshot_date=now.date(), captured_at=now,
                                     stars_count=220, forks_count=1, source="github_api"))
            session.commit()
        tasks.publish_daily_rankings(now.isoformat())
        updated = await read()
        assert updated.meta.collection.succeeded == 20
        assert updated.meta.generated_at >= first.meta.generated_at
        assert len(cache.values) == 3
        with factory() as session:
            for run in session.scalars(select(RankingRun)):
                run.collection_summary = None
                run.published_at = None
            session.commit()
        assert (await read()).meta.collection is None
    finally:
        await engine.dispose()
