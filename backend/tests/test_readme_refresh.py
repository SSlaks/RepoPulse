from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from app.clients.github import (
    GitHubRateLimitError,
    GitHubRateLimitSnapshot,
    GitHubReadmeData,
    GitHubRepositoryData,
    GitHubTransientError,
)
from app.config import Settings
from app.models import Base, JobRun, Repository, RepositoryReadme
from redis.exceptions import RedisError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from worker.app import readme as readme_module
from worker.app import readme_tasks


def _metadata(private: bool = False, full_name: str = "owner/repo") -> GitHubRepositoryData:
    return GitHubRepositoryData(
        github_id=123,
        full_name=full_name,
        description=None,
        html_url="https://github.com/owner/repo",
        language="Python",
        topics=[],
        license_name=None,
        stars_count=10,
        forks_count=1,
        open_issues_count=0,
        is_fork=False,
        archived=False,
        disabled=False,
        pushed_at=None,
        created_at=None,
        is_private=private,
        visibility="private" if private else "public",
    )


class StubGitHubClient:
    def __init__(
        self,
        *,
        readme: GitHubReadmeData | None = None,
        private: bool = False,
        remaining: int = 5000,
        metadata_error: Exception | None = None,
        on_probe: Callable[[], None] | None = None,
        on_readme: Callable[[], None] | None = None,
    ) -> None:
        self.readme_result = readme
        self.private = private
        self.remaining = remaining
        self.metadata_error = metadata_error
        self.on_probe = on_probe
        self.on_readme = on_readme
        self.rate_limit_reset: float | None = None
        self.metadata_calls = 0
        self.readme_calls = 0

    def probe_rate_limit(self) -> GitHubRateLimitSnapshot:
        if self.on_probe:
            self.on_probe()
        return GitHubRateLimitSnapshot(self.remaining, self.rate_limit_reset)

    def has_quota(self, reserve: int) -> bool:
        return self.remaining >= reserve

    def repository_conditional(self, full_name: str, *, etag: str | None = None):
        assert full_name in {"owner/repo", "owner/second"}
        self.metadata_calls += 1
        if self.metadata_error:
            raise self.metadata_error
        return _metadata(self.private, full_name), '"metadata-v1"', False

    def readme_conditional(self, full_name: str, **kwargs: object) -> GitHubReadmeData:
        assert full_name in {"owner/repo", "owner/second"}
        self.readme_calls += 1
        if self.on_readme:
            self.on_readme()
        assert self.readme_result is not None
        return self.readme_result

    def close(self) -> None:
        return None


@pytest.fixture
def readme_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'readme.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    timestamp = datetime.now(UTC)
    with factory() as session:
        session.add(
            Repository(
                github_id=123,
                full_name="owner/repo",
                owner="owner",
                name="repo",
                html_url="https://github.com/owner/repo",
                topics=[],
                stars_count=10,
                first_tracked_at=timestamp,
                last_seen_at=timestamp,
                history_available_from=timestamp,
            )
        )
        session.commit()
    settings = Settings(
        readme_quota_reserve=5,
        readme_requests_per_second=2,
        readme_job_timeout_seconds=60,
    )
    monkeypatch.setattr(readme_module, "get_settings", lambda: settings)
    monkeypatch.setattr(readme_module, "invalidate_readme_cache", lambda _: None)
    try:
        yield factory
    finally:
        engine.dispose()


def _stored_readme(factory, *, content: str = "# old body") -> None:
    with factory() as session:
        repository = session.scalar(select(Repository).where(Repository.full_name == "owner/repo"))
        assert repository is not None
        session.add(
            RepositoryReadme(
                repository_id=repository.id,
                content=content,
                path="README.zh-CN.md",
                html_url="https://github.com/owner/repo/blob/main/README.zh-CN.md",
                visibility="public",
                last_success_at=datetime.now(UTC) - timedelta(days=1),
                next_refresh_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        session.commit()


def _readme_row(factory) -> RepositoryReadme:
    with factory() as session:
        row = session.scalar(select(RepositoryReadme))
        assert row is not None
        return row


def _add_second_repository(factory) -> None:
    timestamp = datetime.now(UTC)
    with factory() as session:
        session.add(
            Repository(
                github_id=124,
                full_name="owner/second",
                owner="owner",
                name="second",
                html_url="https://github.com/owner/second",
                topics=[],
                stars_count=1,
                first_tracked_at=timestamp,
                last_seen_at=timestamp,
                history_available_from=timestamp,
            )
        )
        session.commit()


def test_refresh_persists_body_and_304_keeps_it(readme_database) -> None:
    first_client = StubGitHubClient(
        readme=GitHubReadmeData(
            repository="owner/repo",
            path="README.zh-CN.md",
            content="# 中文正文",
            html_url="https://github.com/owner/repo/blob/main/README.zh-CN.md",
            etag='"readme-v1"',
            root_etag='"root-v1"',
            root_entries=[{"name": "README.zh-CN.md", "type": "file"}],
            readme_endpoint="contents:README.zh-CN.md",
        )
    )
    service = readme_module.ReadmeRefreshService(readme_database, lambda: first_client)
    started_at = datetime.now(UTC)

    assert service.run_batch(now=started_at) == {"status": "ok", "count": 1}
    first = _readme_row(readme_database)
    assert first.content == "# 中文正文"
    assert first.readme_etag == '"readme-v1"'
    assert first.root_etag == '"root-v1"'
    assert first.visibility == "public"

    with readme_database() as session:
        stored = session.scalar(select(RepositoryReadme))
        assert stored is not None
        stored.next_refresh_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    second_client = StubGitHubClient(
        readme=GitHubReadmeData(
            repository="owner/repo",
            path="README.zh-CN.md",
            content="",
            html_url="https://github.com/owner/repo/blob/HEAD/README.zh-CN.md",
            etag='"readme-v1"',
            root_etag='"root-v1"',
            root_entries=[{"name": "README.zh-CN.md", "type": "file"}],
            not_modified=True,
            readme_endpoint="contents:README.zh-CN.md",
        )
    )
    restarted_service = readme_module.ReadmeRefreshService(readme_database, lambda: second_client)

    assert restarted_service.run_batch(now=started_at + timedelta(minutes=11)) == {
        "status": "ok", "count": 1
    }
    second = _readme_row(readme_database)
    assert second.content == "# 中文正文"
    assert second.html_url == first.html_url
    assert second.last_success_at == first.last_success_at


def test_successful_refresh_invalidates_previous_hot_cache(
    readme_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalidated: list[int] = []
    monkeypatch.setattr(readme_module, "invalidate_readme_cache", invalidated.append)
    client = StubGitHubClient(
        readme=GitHubReadmeData(
            repository="owner/repo",
            path="README.md",
            content="# new body",
            html_url="https://github.com/owner/repo/blob/main/README.md",
        )
    )
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    assert service.run_batch(now=datetime.now(UTC)) == {"status": "ok", "count": 1}
    assert invalidated == [1]


def test_refresh_error_keeps_last_good_body(readme_database) -> None:
    _stored_readme(readme_database)
    client = StubGitHubClient(metadata_error=GitHubTransientError("GitHub unavailable"))
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    assert service.run_batch(now=datetime.now(UTC)) == {"status": "ok", "count": 1}
    stored = _readme_row(readme_database)
    assert stored.content == "# old body"
    assert stored.last_error_code == "github_transient"
    assert stored.next_refresh_at is not None


def test_private_metadata_marks_readme_inaccessible(readme_database) -> None:
    _stored_readme(readme_database)
    client = StubGitHubClient(private=True)
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    assert service.run_batch(now=datetime.now(UTC)) == {"status": "ok", "count": 1}
    stored = _readme_row(readme_database)
    assert stored.is_private is True
    assert stored.visibility == "private"
    assert client.readme_calls == 0


def test_quota_reserve_defers_before_repository_requests(readme_database) -> None:
    client = StubGitHubClient(remaining=4)
    client.rate_limit_reset = datetime.now(UTC).timestamp() + 70
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    result = service.run_batch(now=datetime.now(UTC))

    assert result["status"] == "waiting"
    assert client.metadata_calls == 0
    jobs = service.status()
    assert isinstance(jobs, list)
    assert jobs[0]["progress"]["wait_reason"] == "quota_reserve"


def test_rate_limit_retry_after_sets_waiting_state(readme_database) -> None:
    client = StubGitHubClient(metadata_error=GitHubRateLimitError("limited", retry_after=73))
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)
    started_at = datetime.now(UTC)

    result = service.run_batch(now=started_at)

    assert result["status"] == "waiting"
    assert client.metadata_calls == 1
    jobs = service.status()
    assert isinstance(jobs, list)
    assert jobs[0]["progress"]["wait_reason"] == "rate_limit"
    assert datetime.fromisoformat(jobs[0]["progress"]["resume_at"]) >= started_at + timedelta(
        seconds=73
    )


def test_active_collection_defers_readme_without_github(readme_database) -> None:
    with readme_database() as session:
        session.add(
            JobRun(
                job_key="snapshot:active",
                task_name="capture_daily_snapshots",
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        session.commit()
    created = 0

    def new_client() -> StubGitHubClient:
        nonlocal created
        created += 1
        return StubGitHubClient()

    service = readme_module.ReadmeRefreshService(readme_database, new_client)
    result = service.run_batch(now=datetime.now(UTC))

    assert result == {"status": "waiting", "count": 0}
    assert created == 0
    jobs = service.status()
    assert isinstance(jobs, list)
    assert jobs[0]["progress"]["wait_reason"] == "collection_active"


def test_collection_starting_mid_batch_stops_before_next_repository(readme_database) -> None:
    _add_second_repository(readme_database)

    def start_collection() -> None:
        with readme_database() as session:
            session.add(
                JobRun(
                    job_key="snapshot:started-mid-batch",
                    task_name="capture_daily_snapshots",
                    status="running",
                    started_at=datetime.now(UTC),
                )
            )
            session.commit()

    client = StubGitHubClient(
        readme=GitHubReadmeData(
            repository="owner/repo",
            path="README.md",
            content="# first",
            html_url="https://github.com/owner/repo/blob/main/README.md",
        ),
        on_readme=start_collection,
    )
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    result = service.run_batch(now=datetime.now(UTC))

    assert result["status"] == "waiting"
    assert client.metadata_calls == 1
    assert client.readme_calls == 1
    with readme_database() as session:
        count = len(session.scalars(select(RepositoryReadme)).all())
    assert count == 1


def test_previous_rate_limit_wait_blocks_new_batch_bucket(readme_database) -> None:
    started_at = datetime.now(UTC)
    prior_key = readme_module._job_key(started_at, None)
    next_bucket = started_at + timedelta(minutes=11)
    with readme_database() as session:
        session.add(
            JobRun(
                job_key=prior_key,
                task_name="refresh_repository_readmes",
                status="waiting",
                started_at=started_at,
                progress={
                    "wait_reason": "rate_limit",
                    "resume_at": (next_bucket + timedelta(minutes=10)).isoformat(),
                },
            )
        )
        session.commit()
    created = 0

    def new_client() -> StubGitHubClient:
        nonlocal created
        created += 1
        return StubGitHubClient()

    service = readme_module.ReadmeRefreshService(readme_database, new_client)
    result = service.run_batch(now=next_bucket)

    assert result["status"] == "waiting"
    assert created == 0


def test_http_transport_error_backs_off_one_repository_and_keeps_body(readme_database) -> None:
    _stored_readme(readme_database)
    client = StubGitHubClient(metadata_error=httpx.ConnectError("connection failed"))
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    result = service.run_batch(now=datetime.now(UTC))

    assert result["status"] == "ok"
    stored = _readme_row(readme_database)
    assert stored.content == "# old body"
    assert stored.next_refresh_at is not None
    assert stored.last_error_code is not None


def test_stale_job_recovers_then_honors_cancellation(readme_database) -> None:
    started_at = datetime.now(UTC)
    job_key = readme_module._job_key(started_at, None)
    with readme_database() as session:
        session.add(
            JobRun(
                job_key=job_key,
                task_name="refresh_repository_readmes",
                status="running",
                attempts=1,
                started_at=started_at - timedelta(minutes=5),
            )
        )
        session.commit()

    client = StubGitHubClient()
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)
    client.on_probe = lambda: service.cancel(job_key)

    assert service.run_batch(now=started_at) == {"status": "cancelled", "count": 0}
    status = service.status(job_key)
    assert isinstance(status, dict)
    assert status["status"] == "cancelled"
    assert status["attempts"] == 2
    assert client.metadata_calls == 0


def test_elapsed_job_timeout_waits_without_processing(readme_database) -> None:
    client = StubGitHubClient()
    service = readme_module.ReadmeRefreshService(readme_database, lambda: client)

    result = service.run_batch(now=datetime.now(UTC) - timedelta(minutes=2))

    assert result == {"status": "waiting", "count": 0}
    assert client.metadata_calls == 0
    jobs = service.status()
    assert isinstance(jobs, list)
    assert jobs[0]["progress"]["wait_reason"] == "time_limit"


@pytest.mark.parametrize("limit", [0, -1])
def test_batch_limit_must_be_positive(readme_database, limit: int) -> None:
    service = readme_module.ReadmeRefreshService(readme_database, StubGitHubClient)

    with pytest.raises(ValueError, match="positive"):
        service.run_batch(limit=limit)
    assert service.status() == []


def test_task_time_limits_match_runtime_setting() -> None:
    task = readme_tasks.refresh_repository_readmes

    assert task.soft_time_limit == readme_tasks.README_TASK_TIMEOUT
    assert task.time_limit == readme_tasks.README_TASK_TIMEOUT + 60


def test_readme_lock_duplicate_and_redis_failure_are_bounded(
    readme_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeLock:
        def __init__(self, error: bool) -> None:
            self.error = error

        def acquire(self, *, blocking: bool) -> bool:
            assert blocking is False
            if self.error:
                raise RedisError("lock unavailable")
            return False

    class FakeRedis:
        def __init__(self, error: bool) -> None:
            self.error = error

        def lock(self, *_: object, **__: object) -> FakeLock:
            return FakeLock(self.error)

        def close(self) -> None:
            return None

    monkeypatch.setattr("worker.app.readme.Redis.from_url", lambda *_, **__: FakeRedis(False))
    with readme_module.readme_task_lock(60) as acquired:
        assert acquired is False

    monkeypatch.setattr("worker.app.readme.Redis.from_url", lambda *_, **__: FakeRedis(True))
    with pytest.raises(readme_module.ReadmeLockUnavailable), readme_module.readme_task_lock(60):
        pytest.fail("unavailable lock must not run refresh work")
