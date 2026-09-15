from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from app.clients.github import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubPermissionError,
)
from app.models import JobRun, RankingRun, Repository, RepoSnapshot, SnapshotRequest
from sqlalchemy import select
from test_collection import client_for, repository_data

from worker.app import tasks
from worker.app.availability import finalize_health, record_failure, utc
from worker.app.checkpoints import record_error
from worker.app.probes import probe_repositories
from worker.app.recovery import dispatch_recovery
from worker.app.snapshots import SnapshotCollector, SnapshotIncomplete


def collect(factory, now, fetch, automatic=False):
    client = client_for(fetch)
    result = SnapshotCollector(now, factory, lambda: client, tasks._upsert_repository,
                               automatic=automatic).run()
    return result, client


def test_three_distinct_days_and_48_hours_required(environment):
    factory, now = environment
    with factory() as session:
        repo = session.get(Repository, 1)
        for _ in range(3):
            record_failure(repo, "404", now, True)
        assert repo.consecutive_not_found == 1
        assert repo.availability_status == "suspect"
        record_failure(repo, "404", now + timedelta(days=1), True)
        record_failure(repo, "404", now + timedelta(days=2), True)
        assert repo.availability_status == "quarantined"
        assert repo.next_probe_at == now + timedelta(days=3)


def test_bulk_errors_freeze_confirmation(environment):
    factory, now = environment
    with factory() as session:
        rows = [SnapshotRequest(repository_id=1, error_code="404", checked_at=now,
                                availability_applied=False) for _ in range(10)]
        assert finalize_health(session, rows, True)
        assert session.get(Repository, 1).consecutive_not_found == 0


def test_404_keeps_success_and_freezes_membership(environment):
    factory, now = environment
    def fetch(name):
        if name.endswith("1"):
            raise GitHubNotFoundError("missing", 404)
        return repository_data(int(name[-1]))
    with pytest.raises(SnapshotIncomplete):
        collect(factory, now, fetch)
    with factory() as session:
        assert len(session.scalars(select(RepoSnapshot)).all()) == 2
        repo = session.get(Repository, 1)
        assert repo.availability_status == "suspect"
        repo.availability_status = "quarantined"
        tasks._upsert_repository(session, repository_data(4))
        session.commit()
    with pytest.raises(SnapshotIncomplete):
        collect(factory, now, fetch, automatic=True)
    with factory() as session:
        assert len(session.scalars(select(SnapshotRequest)).all()) == 3
        assert session.scalar(select(JobRun.progress))["missing"] == 1


def test_retry_budget_persists_and_only_due_rows_run(environment, monkeypatch):
    factory, now = environment
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    with factory() as session:
        session.query(RepoSnapshot).filter_by(repository_id=1).delete()
        row = session.scalar(select(SnapshotRequest).where(SnapshotRequest.repository_id == 1))
        record_error(row, "transient", "timeout", datetime.now(UTC))
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    # Avoid actual exponential waits while exercising the coordinator and durable ledger.
    from worker.app.collection import RepositoryRequests
    monkeypatch.setattr(RepositoryRequests, "_fetch", lambda self, name: (_ for _ in ()).throw(
        httpx.ReadTimeout("timeout")))
    for retry in (1, 2):
        with pytest.raises(SnapshotIncomplete):
            collect(factory, now, lambda name: None, automatic=True)
        with factory() as session:
            row = session.scalar(select(SnapshotRequest).where(SnapshotRequest.repository_id == 1))
            assert row.retry_count == retry
            assert row.status == ("waiting" if retry == 1 else "failed")
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    with pytest.raises(SnapshotIncomplete):
        collect(factory, now, lambda name: pytest.fail("exhausted retry ran"), automatic=True)


def test_probe_backoff_then_restore(environment, monkeypatch):
    factory, _now = environment
    with factory() as session:
        repo = session.get(Repository, 1)
        repo.availability_status = "quarantined"
        repo.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    client = client_for(lambda _: (_ for _ in ()).throw(GitHubNotFoundError("missing", 404)))
    monkeypatch.setattr(tasks, "GitHubClient", lambda: client)
    assert probe_repositories() == {"checked": 1, "restored": 0}
    with factory() as session:
        repo = session.get(Repository, 1)
        assert repo.probe_backoff_step == 1
        assert utc(repo.next_probe_at) > datetime.now(UTC) + timedelta(days=2)
        repo.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    client.repository.side_effect = lambda _: repository_data(1)
    assert probe_repositories() == {"checked": 1, "restored": 1}
    with factory() as session:
        repo = session.get(Repository, 1)
        assert repo.availability_status == "active"
        assert repo.next_probe_at is None
        assert session.scalar(select(RepoSnapshot)).snapshot_date == datetime.now(UTC).date()


def test_atomic_publication_rolls_back_and_retries(environment, monkeypatch):
    factory, now = environment
    collect(factory, now, lambda name: repository_data(int(name[-1])))
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = "completed"
        job.progress = dict(job.progress, publication="pending")
        session.commit()
    original = tasks._persist_ranking
    def fail_second(session, period, as_of, ids):
        if period == 7:
            raise RuntimeError("simulated database failure")
        return original(session, period, as_of, ids)
    monkeypatch.setattr(tasks, "_persist_ranking", fail_second)
    with pytest.raises(RuntimeError):
        tasks.publish_daily_rankings(now.isoformat())
    with factory() as session:
        assert not session.scalars(select(RankingRun)).all()
        assert session.scalar(select(JobRun.progress))["publication"] == "pending"
    monkeypatch.setattr(tasks, "_persist_ranking", original)
    assert tasks.publish_daily_rankings(now.isoformat())["count"] == 12
    assert tasks.publish_daily_rankings(now.isoformat())["status"] == "duplicate"
    with factory() as session:
        assert len(session.scalars(select(RankingRun)).all()) == 4


def test_recovery_respects_cancellation_and_deadline(environment, monkeypatch):
    factory, now = environment
    with pytest.raises(SnapshotIncomplete):
        collect(factory, now, lambda _: (_ for _ in ()).throw(GitHubNotFoundError("missing", 404)))
    dispatch = Mock()
    monkeypatch.setattr(tasks.capture_daily_snapshots, "delay", dispatch)
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.status = "waiting"
        job.cancel_requested = True
        session.commit()
    assert dispatch_recovery() == 0
    with factory() as session:
        job = session.scalar(select(JobRun))
        job.cancel_requested = False
        job.progress = dict(job.progress, deadline=(datetime.now(UTC)-timedelta(seconds=1)).isoformat())
        session.commit()
    assert dispatch_recovery() == 0
    assert not dispatch.called
    with factory() as session:
        assert session.scalar(select(JobRun.status)) == "partial"


@pytest.mark.parametrize("status,error", [(401, GitHubAuthenticationError),
                                         (403, GitHubPermissionError), (404, GitHubNotFoundError)])
def test_structured_errors(status, error):
    client = GitHubClient(httpx.MockTransport(lambda _: httpx.Response(status, json={})))
    try:
        with pytest.raises(error) as exc:
            client.repository("owner/repo")
        assert exc.value.status_code == status
    finally:
        client.close()


def test_live_collection_rejects_historical_dates():
    with pytest.raises(ValueError, match="current UTC date"):
        tasks.capture_daily_snapshots.run((datetime.now(UTC) - timedelta(days=1)).isoformat())
