from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from app.clients.github import GitHubAuthenticationError, GitHubNotFoundError
from app.models import JobRun, RankingRun, Repository, RepoSnapshot, SnapshotRequest
from sqlalchemy import select
from test_collection import repository_data
from test_repository_recovery import collect

from worker.app import tasks
from worker.app.availability import record_failure
from worker.app.checkpoints import due
from worker.app.recovery import dispatch_recovery
from worker.app.snapshots import SnapshotIncomplete


def test_authentication_failure_stops_and_preserves_prior_success(environment, monkeypatch):
    factory, now = environment
    settings = tasks.get_settings()
    settings.snapshot_concurrency = 1
    def fetch(name):
        if name.endswith("2"):
            raise GitHubAuthenticationError("invalid token", 401)
        return repository_data(int(name[-1]))
    from test_collection import client_for
    client = client_for(fetch)
    monkeypatch.setattr(tasks, "GitHubClient", lambda: client)
    with pytest.raises(GitHubAuthenticationError):
        tasks.capture_daily_snapshots.run(now.isoformat())
    assert client.repository.call_count == 2
    with factory() as session:
        assert session.scalar(select(JobRun.status)) == "failed"
        assert len(session.scalars(select(RepoSnapshot)).all()) == 1


def test_missing_endpoint_never_uses_repository_star_count(environment):
    factory, now = environment
    with factory() as session:
        assert tasks._persist_ranking(session, 1, now) == 0


def test_missing_exact_baseline_does_not_count_multiday_growth(environment):
    factory, now = environment
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    with factory() as session:
        session.add(RepoSnapshot(repository_id=1, snapshot_date=(now-timedelta(days=2)).date(),
                                 captured_at=now-timedelta(days=2), stars_count=100, forks_count=1,
                                 source="github_api"))
        session.commit()
        tasks._persist_ranking(session, 1, now)
        session.flush()
        from app.models import RankingItem
        row = session.scalar(select(RankingItem).where(RankingItem.repository_id == 1))
        assert not row.baseline_available
        assert row.net_delta == 0


def test_recovery_restarts_after_final_save_before_completion(environment, monkeypatch):
    factory, now = environment
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.started_at = datetime.now(UTC) - timedelta(seconds=8000)
        session.commit()
    dispatch = Mock()
    monkeypatch.setattr(tasks.capture_daily_snapshots, "delay", dispatch)
    assert dispatch_recovery() == 1
    dispatch.assert_called_once_with(now.isoformat(), automatic=True)


def test_suspect_and_permission_errors_do_not_auto_retry_same_day():
    now = datetime.now(UTC)
    assert not due(SnapshotRequest(status="failed", error_code="404"), now, False)
    assert not due(SnapshotRequest(status="failed", error_code="403"), now, False)
    assert due(SnapshotRequest(status="failed", error_code="404"), now, True)


def test_observation_mode_records_without_quarantining(environment):
    factory, now = environment
    with factory() as session:
        repo = session.get(Repository, 1)
        for days in range(5):
            record_failure(repo, "404", now+timedelta(days=days), False)
        assert repo.consecutive_not_found == 5
        assert repo.availability_status == "suspect"


def test_quarantined_and_manual_exclusions_are_not_collected(environment):
    factory, now = environment
    tasks.get_settings().excluded_repositories = "OWNER/REPO2"
    with factory() as session:
        session.get(Repository, 1).availability_status = "quarantined"
        session.commit()
    count, client = collect(factory, now, lambda _: repository_data(3))
    assert count == 1
    client.repository.assert_called_once_with("owner/repo3")


def test_incomplete_collection_never_publishes(environment, monkeypatch):
    _factory, now = environment
    from test_collection import client_for
    def fetch(name):
        if name.endswith("1"):
            raise GitHubNotFoundError("missing", 404)
        return repository_data(int(name[-1]))
    monkeypatch.setattr(tasks, "GitHubClient", lambda: client_for(fetch))
    dispatch = Mock()
    monkeypatch.setattr(tasks.publish_daily_rankings, "delay", dispatch)
    assert tasks.capture_daily_snapshots.run(now.isoformat())["status"] == "partial"
    assert not dispatch.called
    assert tasks.build_ranking(1, now.isoformat())["status"] == "blocked"


def test_existing_demo_snapshot_is_replaced_without_unique_conflict(environment):
    factory, now = environment
    with factory() as session:
        session.add(RepoSnapshot(repository_id=1, snapshot_date=now.date(), captured_at=now,
                                 stars_count=1, forks_count=0, source="demo"))
        session.commit()
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    with factory() as session:
        rows = session.scalars(select(RepoSnapshot)).all()
        assert len(rows) == 3
        assert all(row.source == "github_api" for row in rows)


def test_empty_membership_blocks_publication(environment):
    factory, now = environment
    tasks.get_settings().excluded_repositories = "owner/repo1,owner/repo2,owner/repo3"
    with pytest.raises(SnapshotIncomplete):
        collect(factory, now, lambda _: pytest.fail("unexpected request"))
    with factory() as session:
        assert not session.scalars(select(RankingRun)).all()


def test_delayed_duplicate_cannot_bypass_quota_or_restart_failed_job(environment, monkeypatch):
    factory, now = environment
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    capture = Mock()
    monkeypatch.setattr(tasks, "_capture_snapshots", capture)
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = "waiting"
        job.progress = dict(job.progress, resume_at=(datetime.now(UTC)+timedelta(hours=1)).isoformat())
        session.commit()
    assert tasks.capture_daily_snapshots.run(now.isoformat(), automatic=True)["status"] == "duplicate"
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = "failed"
        session.commit()
    assert tasks.capture_daily_snapshots.run(now.isoformat(), automatic=True)["status"] == "duplicate"
    capture.assert_not_called()


def test_publication_automatic_attempts_are_bounded(environment, monkeypatch):
    factory, now = environment
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = "completed"
        job.progress = dict(job.progress, publication="pending", publication_attempts=5)
        session.commit()
    persist = Mock()
    monkeypatch.setattr(tasks, "_persist_ranking", persist)
    assert tasks.publish_daily_rankings(now.isoformat(), automatic=True)["status"] == "failed"
    persist.assert_not_called()
