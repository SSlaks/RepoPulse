from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from threading import Lock, get_ident
from time import monotonic, sleep
from unittest.mock import Mock

import httpx
import pytest
from app.clients.github import (
    GitHubClient,
    GitHubClientError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubRepositoryData,
)
from app.config import Settings
from app.models import Base, JobRun, Repository, RepoSnapshot
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from worker.app import tasks
from worker.app.collection import RepositoryRequests
from worker.app.snapshots import SnapshotCancelled, SnapshotCollector, SnapshotIncomplete

AS_OF = datetime(2026, 9, 10, 2, tzinfo=UTC)
KEY = "snapshot:all:2026-09-10"


def repository_data(index=1):
    return GitHubRepositoryData(
        github_id=index, full_name=f"owner/repo{index}", description="fresh",
        html_url=f"https://github.com/owner/repo{index}", language="Python", topics=[],
        license_name="MIT", stars_count=200 + index, forks_count=3,
        open_issues_count=0, is_fork=False, archived=False, disabled=False,
        pushed_at=None, created_at=None,
    )


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'collection.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        for index in range(1, 9):
            tasks._upsert_repository(session, repository_data(index))
        session.add(JobRun(job_key=KEY, task_name="capture_daily_snapshots",
                           status="running", attempts=1, started_at=AS_OF))
        session.commit()
    monkeypatch.setattr(tasks, "get_sync_session", factory)
    monkeypatch.setattr(tasks, "_validate_capture_date", lambda _: None)
    monkeypatch.setattr("worker.app.snapshots.get_settings", lambda: Settings(
        snapshot_concurrency=4, snapshot_requests_per_second=10, snapshot_batch_size=3,
    ))
    yield factory
    engine.dispose()


def client_for(fetch):
    client = Mock(spec=GitHubClient)
    client.repository.side_effect = fetch
    client.rate_limit_remaining = None
    client.rate_limit_reset = None
    return client


def test_discovery_fetches_only_new_names_including_disabled(database, monkeypatch):
    with database() as session:
        session.get(Repository, 2).disabled = True
        session.commit()
    client = client_for(lambda _: repository_data(9))
    monkeypatch.setattr(tasks, "GitHubClient", lambda: client)
    assert tasks._hydrate_names(
        ["OWNER/REPO1", "owner/repo2", "owner/repo9", "owner/repo9"], only_new=True,
    ) == 1
    client.repository.assert_called_once_with("owner/repo9")
    with database() as session:
        assert session.get(Repository, 2).disabled
        assert len(session.scalars(select(Repository)).all()) == 9


def test_snapshots_are_fresh_unique_and_database_stays_on_main_thread(database):
    main_thread = get_ident()
    threads = set()
    def fetch(name):
        threads.add(get_ident())
        return replace(repository_data(int(name.removeprefix("owner/repo"))), stars_count=999)
    def upsert(session, data, **kwargs):
        assert get_ident() == main_thread
        tasks._upsert_repository(session, data, **kwargs)
    client = client_for(fetch)
    assert SnapshotCollector(AS_OF, database, lambda: client, upsert).run() == 8
    assert SnapshotCollector(AS_OF, database, lambda: client, upsert).run() == 0
    assert client.repository.call_count == 8
    assert threads and main_thread not in threads
    with database() as session:
        snapshots = session.scalars(select(RepoSnapshot)).all()
        assert len(snapshots) == 8
        assert all(row.stars_count == 999 for row in snapshots)
        progress = session.scalar(select(JobRun.progress))
        assert progress["saved"] == progress["total"] == 8


def test_partial_failure_saved_and_resume_only_missing(database):
    def fetch(name):
        if name == "owner/repo3":
            raise GitHubClientError("GitHub request failed: 403")
        return repository_data(int(name.removeprefix("owner/repo")))
    client = client_for(fetch)
    with pytest.raises(SnapshotIncomplete):
        SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run()
    with database() as session:
        assert len(session.scalars(select(RepoSnapshot)).all()) == 7
        assert session.scalar(select(JobRun.progress))["failed"] == 1
    client.repository.side_effect = lambda _: repository_data(3)
    client.repository.reset_mock()
    assert SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run() == 1
    client.repository.assert_called_once_with("owner/repo3")


def test_cancel_drains_inflight_and_prevents_more_requests(database):
    def fetch(name):
        with database() as session:
            job = session.scalar(select(JobRun))
            job.cancel_requested = True
            session.commit()
        return repository_data(int(name.removeprefix("owner/repo")))
    client = client_for(fetch)
    with pytest.raises(SnapshotCancelled):
        SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run()
    assert 1 <= client.repository.call_count <= 4
    with database() as session:
        assert len(session.scalars(select(RepoSnapshot)).all()) == client.repository.call_count
    assert not tasks._start_job(KEY, "capture_daily_snapshots")
    with database() as session:
        assert session.scalar(select(JobRun.status)) == "cancelled"


def test_permission_error_is_not_rate_limit():
    client = GitHubClient(httpx.MockTransport(lambda _: httpx.Response(
        403, json={"message": "Resource not accessible by personal access token"},
    )))
    try:
        with pytest.raises(GitHubClientError) as exc:
            client.repository("owner/repo")
        assert type(exc.value) is GitHubPermissionError
    finally:
        client.close()


def test_rate_headers_and_secondary_fallback(database):
    from app.clients.github import GitHubRateLimitError
    client = GitHubClient(httpx.MockTransport(lambda _: httpx.Response(
        429, headers={"retry-after": "123"}, json={"message": "secondary rate limit"},
    )))
    with pytest.raises(GitHubRateLimitError) as exc:
        SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run()
    assert exc.value.retry_after == 123
    assert exc.value.secondary
    with database() as session:
        assert session.scalar(select(JobRun.progress))["serial_fallback"]


def test_primary_limit_uses_reset_header():
    from app.clients.github import GitHubRateLimitError
    reset = (datetime.now(UTC) + timedelta(seconds=120)).timestamp()
    client = GitHubClient(httpx.MockTransport(lambda _: httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
        json={"message": "API rate limit exceeded"},
    )))
    try:
        with pytest.raises(GitHubRateLimitError) as exc:
            client.repository("owner/repo")
        assert 115 <= exc.value.retry_after <= 121
        assert not exc.value.secondary
    finally:
        client.close()


def test_four_workers_rate_pacing_and_thread_local_clients():
    lock = Lock()
    active = peak = 0
    starts = []
    clients = []
    def factory():
        owner_thread = get_ident()
        def fetch(_):
            nonlocal active, peak
            assert get_ident() == owner_thread
            with lock:
                starts.append(monotonic())
                active += 1
                peak = max(peak, active)
            sleep(0.4)
            with lock:
                active -= 1
            return repository_data()
        client = client_for(fetch)
        clients.append(client)
        return client
    requests = RepositoryRequests(4, 10, factory)
    try:
        futures = [requests.submit(str(index)) for index in range(8)]
        for future in futures:
            assert future.result() is not None
    finally:
        requests.close()
    assert 2 <= peak <= 4
    assert all(b - a >= 0.09 for a, b in pairwise(starts))
    assert len(clients) <= 4
    assert all(client.close.call_count == 1 for client in clients)


def test_one_second_latency_benchmark():
    def run(concurrency):
        client = client_for(lambda _: (sleep(1), repository_data())[1])
        requests = RepositoryRequests(concurrency, 4, lambda: client)
        start = monotonic()
        try:
            with ThreadPoolExecutor(max_workers=1) as coordinator:
                futures = [requests.submit(str(index)) for index in range(8)]
                coordinator.submit(lambda: [future.result() for future in futures]).result()
        finally:
            requests.close()
        return monotonic() - start
    serial, parallel = run(1), run(4)
    print(f"one-second latency benchmark: serial={serial:.2f}s parallel={parallel:.2f}s")
    assert parallel < serial * 0.55


def test_retry_delays_are_one_two_four_seconds(monkeypatch):
    waits = []
    client = client_for(lambda _: repository_data())
    client.repository.side_effect = [httpx.ReadTimeout("slow")] * 3 + [repository_data()]
    requests = RepositoryRequests(1, 4, lambda: client)
    monkeypatch.setattr(requests, "_admit", lambda: True)
    monkeypatch.setattr(requests.stopped, "wait", lambda seconds: waits.append(seconds) or False)
    try:
        assert requests.submit("owner/repo1").result() == repository_data()
        assert waits == [1, 2, 4]
    finally:
        requests.close()


def test_success_exhausting_quota_stops_next_request():
    client = client_for(lambda _: repository_data())
    client.rate_limit_remaining = 0
    client.rate_limit_reset = datetime.now(UTC).timestamp() + 120
    requests = RepositoryRequests(1, 4, lambda: client)
    try:
        assert requests.submit("owner/repo1").result() is not None
        assert requests.submit("owner/repo2").result() is None
        assert requests.rate_limit is not None
        assert not requests.rate_limit.secondary
        assert client.repository.call_count == 1
    finally:
        requests.close()


def test_retry_pins_date_and_does_not_publish_rankings(database, monkeypatch):
    monkeypatch.setattr(tasks, "_task_lock", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(tasks, "_parse_as_of", lambda _: AS_OF)
    capture = Mock(side_effect=GitHubRateLimitError("exhausted", 125))
    monkeypatch.setattr(tasks, "_capture_snapshots", capture)
    publish = Mock()
    monkeypatch.setattr(tasks.publish_daily_rankings, "delay", publish)
    assert tasks.capture_daily_snapshots.run()["status"] == "waiting"
    assert not publish.called
    with database() as session:
        job = session.scalar(select(JobRun))
        assert job.status == "waiting"
        assert job.progress["wait_reason"] == "quota_exhausted"
        assert "resume_at" in job.progress
        assert job.progress["quota_retries"] == 1


@pytest.mark.parametrize("error,status", [
    (SnapshotIncomplete("one failure"), "partial"),
    (SnapshotCancelled("cancelled"), "cancelled"),
])
def test_unsuccessful_collection_never_publishes(database, monkeypatch, error, status):
    monkeypatch.setattr(tasks, "_task_lock", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(tasks, "_capture_snapshots", Mock(side_effect=error))
    publish = Mock()
    monkeypatch.setattr(tasks.publish_daily_rankings, "delay", publish)
    assert tasks.capture_daily_snapshots.run(AS_OF.isoformat())["status"] == status
    assert not publish.called
    with database() as session:
        assert session.scalar(select(JobRun.status)) == status


def test_completed_snapshot_publishes_four_rankings(database, monkeypatch):
    monkeypatch.setattr(tasks, "_task_lock", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(tasks, "_capture_snapshots", Mock(return_value=8))
    publish = Mock()
    monkeypatch.setattr(tasks.publish_daily_rankings, "delay", publish)
    assert tasks.capture_daily_snapshots.run(AS_OF.isoformat())["status"] == "ok"
    publish.assert_called_once_with(AS_OF.isoformat(), automatic=True)


def test_cancelled_delayed_task_does_not_restart(database, monkeypatch):
    with database() as session:
        job = session.scalar(select(JobRun))
        job.status = "waiting"
        job.cancel_requested = True
        session.commit()
    monkeypatch.setattr(tasks, "_task_lock", lambda *args, **kwargs: nullcontext(True))
    capture = Mock()
    monkeypatch.setattr(tasks, "_capture_snapshots", capture)
    tasks.capture_daily_snapshots.run(AS_OF.isoformat())
    capture.assert_not_called()


def test_secondary_limit_resumes_serially(database, monkeypatch):
    with database() as session:
        job = session.scalar(select(JobRun))
        job.progress = {"serial_fallback": True}
        session.commit()
    lock = Lock()
    active = peak = 0
    def fetch(name):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        sleep(0.15)
        with lock:
            active -= 1
        return repository_data(int(name.removeprefix("owner/repo")))
    client = client_for(fetch)
    assert SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run() == 8
    assert peak == 1


def test_batch_flush_commits_at_most_configured_size(database):
    from sqlalchemy import event
    batch_sizes = []
    def count_batch(session):
        batch_sizes.append(sum(isinstance(row, RepoSnapshot) for row in session.new))
    event.listen(database, "before_flush", lambda session, context, instances: count_batch(session))
    client = client_for(lambda name: repository_data(int(name.removeprefix("owner/repo"))))
    assert SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run() == 8
    assert sum(batch_sizes) == 8
    assert max(batch_sizes) <= 3


def test_timed_flush_preserves_success_while_other_request_is_slow(database, monkeypatch):
    monkeypatch.setattr("worker.app.snapshots.get_settings", lambda: Settings(
        snapshot_concurrency=4, snapshot_requests_per_second=10,
        snapshot_batch_size=50, snapshot_flush_seconds=0.1,
    ))
    observed_saved = []
    def fetch(name):
        if name == "owner/repo1":
            sleep(1.5)
            with database() as session:
                observed_saved.append(len(session.scalars(select(RepoSnapshot)).all()))
        return repository_data(int(name.removeprefix("owner/repo")))
    client = client_for(fetch)
    assert SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run() == 8
    assert observed_saved[0] > 0


def test_excluded_repositories_not_requested(database, monkeypatch):
    monkeypatch.setattr("worker.app.snapshots.get_settings", lambda: Settings(
        excluded_repositories="OWNER/REPO2", snapshot_requests_per_second=10,
    ))
    client = client_for(lambda name: repository_data(int(name.removeprefix("owner/repo"))))
    assert SnapshotCollector(AS_OF, database, lambda: client, tasks._upsert_repository).run() == 7
    assert "owner/repo2" not in [call.args[0] for call in client.repository.call_args_list]


def test_renamed_discovery_does_not_reenable_existing_repository(database, monkeypatch):
    with database() as session:
        session.get(Repository, 1).disabled = True
        session.commit()
    client = client_for(lambda _: replace(repository_data(1), full_name="new/name"))
    monkeypatch.setattr(tasks, "GitHubClient", lambda: client)
    assert tasks._hydrate_names(["new/name"], only_new=True) == 0
    with database() as session:
        assert session.get(Repository, 1).disabled
