"""Durable, bounded README refreshes.

The database owns README bodies and refresh state.  Redis is used only for
short lived request de-duplication and task locking; a Redis outage therefore
cannot erase or replace a stored body.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from app.clients.github import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitError,
    GitHubTransientError,
)
from app.config import get_settings
from app.database import get_sync_session
from app.models import JobRun, Repository, RepositoryReadme
from app.readme_cache import readme_cache_key
from redis import Redis
from redis.exceptions import LockError, RedisError
from sqlalchemy import and_, case, or_, select

logger = logging.getLogger(__name__)


class ReadmeLockUnavailable(RuntimeError):
    """A refresh must stop when durable task locking cannot be proved."""


@contextmanager
def readme_task_lock(timeout: int) -> Iterator[bool]:
    client = Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )
    lock = client.lock("lock:readme-refresh", timeout=timeout, blocking_timeout=0)
    acquired = False
    try:
        try:
            acquired = bool(lock.acquire(blocking=False))
        except RedisError as exc:
            raise ReadmeLockUnavailable("README refresh lock unavailable") from exc
        yield acquired
    finally:
        if acquired:
            try:
                lock.release()
            except (LockError, RedisError):
                logger.warning("README refresh lock release failed", exc_info=True)
        client.close()


def invalidate_readme_cache(repository_id: int) -> None:
    client = Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )
    try:
        client.delete(readme_cache_key(repository_id))
    except RedisError:
        logger.warning("README cache invalidation failed", extra={"repository_id": repository_id})
    finally:
        client.close()


class ReadmeRefreshService:
    def __init__(self, session_factory=get_sync_session, client_factory=GitHubClient) -> None:
        self._session_factory = session_factory
        self._client_factory = client_factory

    def run_batch(
        self,
        *,
        repository_id: int | None = None,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, int | str]:
        settings = get_settings()
        if limit is not None and limit <= 0:
            raise ValueError("README batch limit must be positive")
        current = _utc(now or datetime.now(UTC))
        batch_limit = min(limit if limit is not None else settings.readme_batch_size,
                          settings.readme_batch_size)
        job_key = _job_key(current, repository_id)
        with self._session_factory() as session:
            job = self._start_job(session, job_key, repository_id, current)
            if job is None:
                return {"status": "duplicate", "count": 0}
            if self._collection_active(session, current):
                self._wait_job(session, job, current + timedelta(minutes=5), "collection_active")
                return {"status": "waiting", "count": 0}
            resume_at = self._readme_waiting(session, current)
            if resume_at is not None:
                self._wait_job(session, job, resume_at, "readme_waiting")
                return {"status": "waiting", "count": 0}
            candidates = self._candidates(session, current, batch_limit, repository_id)
            if not candidates:
                self._finish_job(session, job, "completed")
                return {"status": "ok", "count": 0}
            progress = dict(job.progress or {})
            progress["selected"] = len(candidates)
            job.progress = progress
            session.commit()

        client = self._client_factory()
        try:
            snapshot = client.probe_rate_limit()
            if snapshot.remaining is None or snapshot.remaining < settings.readme_quota_reserve:
                resume_at = _quota_resume_at(client, current)
                self._set_waiting(job_key, resume_at, "quota_reserve")
                return {"status": "waiting", "count": 0, "resume_at": resume_at.isoformat()}
            if hasattr(client, "set_request_guard"):
                client.set_request_guard(lambda: self._ensure_quota(client))
            return self._process_candidates(job_key, candidates, client, current)
        except GitHubRateLimitError as exc:
            resume_at = current + timedelta(seconds=exc.retry_after)
            self._set_waiting(job_key, resume_at, "rate_limit")
            return {"status": "waiting", "count": 0, "resume_at": resume_at.isoformat()}
        except Exception as exc:
            logger.exception("README refresh batch failed", extra={"job_key": job_key})
            self._finish_by_key(job_key, "failed", str(exc))
            raise
        finally:
            client.close()

    def status(self, job_key: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        with self._session_factory() as session:
            query = select(JobRun).where(JobRun.task_name == "refresh_repository_readmes")
            if job_key:
                query = query.where(JobRun.job_key == job_key)
            else:
                query = query.order_by(JobRun.started_at.desc()).limit(20)
            jobs = session.scalars(query).all()
            values = [_job_status(job) for job in jobs]
            return values[0] if job_key and values else (values if not job_key else {})

    def cancel(self, job_key: str | None = None) -> int:
        with self._session_factory() as session:
            query = select(JobRun).where(
                JobRun.task_name == "refresh_repository_readmes",
                JobRun.status.in_(("queued", "running", "waiting")),
            )
            if job_key:
                query = query.where(JobRun.job_key == job_key)
            jobs = session.scalars(query).all()
            for job in jobs:
                job.cancel_requested = True
            session.commit()
            return len(jobs)

    def _process_candidates(
        self,
        job_key: str,
        candidates: list[tuple[Repository, RepositoryReadme | None]],
        client: GitHubClient,
        started_at: datetime,
    ) -> dict[str, int | str]:
        settings = get_settings()
        interval = 1 / settings.readme_requests_per_second
        next_request = time.monotonic()
        next_http_request = [next_request]
        if hasattr(client, "set_request_pacer"):
            client.set_request_pacer(lambda: _pace(next_http_request, interval))
        counts = {"succeeded": 0, "not_modified": 0, "failed": 0, "private": 0}
        for repository, readme in candidates:
            with self._session_factory() as session:
                collection_active = self._collection_active(session, datetime.now(UTC))
            if collection_active:
                resume_at = datetime.now(UTC) + timedelta(minutes=5)
                self._set_waiting(job_key, resume_at, "collection_active")
                return {"status": "waiting", "count": sum(counts.values())}
            if self._cancelled(job_key):
                self._finish_by_key(job_key, "cancelled", "Cancelled at user request")
                return {"status": "cancelled", "count": sum(counts.values())}
            if datetime.now(UTC) - started_at > timedelta(seconds=settings.readme_job_timeout_seconds):
                self._set_waiting(job_key, datetime.now(UTC) + timedelta(minutes=5), "time_limit")
                return {"status": "waiting", "count": sum(counts.values())}
            if not client.has_quota(settings.readme_quota_reserve):
                resume_at = _quota_resume_at(client, datetime.now(UTC))
                self._set_waiting(job_key, resume_at, "quota_reserve")
                return {"status": "waiting", "count": sum(counts.values())}
            if not hasattr(client, "set_request_pacer"):
                delay = next_request - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, 2.0))
                next_request = time.monotonic() + interval
            try:
                result = self._refresh_one(repository, readme, client)
                if result == "private":
                    counts["private"] += 1
                elif result == "not_modified":
                    counts["not_modified"] += 1
                else:
                    counts["succeeded"] += 1
            except GitHubRateLimitError as exc:
                self._mark_failure(repository.id, "rate_limited", str(exc), exc.retry_after)
                resume_at = datetime.now(UTC) + timedelta(seconds=exc.retry_after)
                self._set_waiting(job_key, resume_at, "rate_limit")
                return {"status": "waiting", "count": sum(counts.values())}
            except (GitHubClientError, httpx.RequestError) as exc:
                self._mark_failure(repository.id, _error_code(exc), str(exc))
                counts["failed"] += 1
            self._advance_progress(job_key, counts)
        self._finish_by_key(job_key, "completed", None)
        return {"status": "ok", "count": sum(counts.values())}

    def _refresh_one(
        self,
        repository: Repository,
        readme: RepositoryReadme | None,
        client: GitHubClient,
    ) -> str:
        now = datetime.now(UTC)
        if readme is None:
            readme = RepositoryReadme(repository_id=repository.id, next_refresh_at=now)
            with self._session_factory() as session:
                session.add(readme)
                session.commit()
        try:
            metadata, metadata_etag, _ = client.repository_conditional(
                repository.full_name, etag=readme.metadata_etag
            )
        except (GitHubNotFoundError, GitHubPermissionError) as exc:
            self._mark_private(repository.id, _error_code(exc), str(exc))
            return "private"
        except GitHubRateLimitError:
            raise
        except GitHubClientError:
            raise
        with self._session_factory() as session:
            current_repo = session.get(Repository, repository.id)
            current_readme = session.scalar(
                select(RepositoryReadme).where(RepositoryReadme.repository_id == repository.id)
            )
            if current_repo is None or current_readme is None:
                raise GitHubClientError("README refresh target disappeared")
            if metadata is not None:
                current_readme.is_private = metadata.is_private
                current_readme.visibility = metadata.visibility or "unknown"
                current_readme.metadata_etag = metadata_etag
            if current_readme.is_private or current_readme.visibility.lower() != "public":
                session.commit()
                invalidate_readme_cache(repository.id)
                code = "private" if current_readme.is_private else "public_metadata_unknown"
                self._mark_failure(repository.id, code, "Repository is not public")
                return "private"
            current_readme.last_public_verified_at = now
            session.commit()
            repository = current_repo
            readme = current_readme

        result = client.readme_conditional(
            repository.full_name,
            root_etag=readme.root_etag,
            root_entries=readme.root_entries,
            readme_etag=readme.readme_etag,
            cached_path=readme.path,
            cached_endpoint=readme.readme_endpoint,
        )
        with self._session_factory() as session:
            stored = session.scalar(
                select(RepositoryReadme).where(RepositoryReadme.repository_id == repository.id)
            )
            if stored is None:
                raise GitHubClientError("README refresh state disappeared")
            _save_result(stored, result, now)
            session.commit()
        invalidate_readme_cache(repository.id)
        return "not_modified" if result.not_modified else "saved"

    def _candidates(
        self,
        session,
        now: datetime,
        limit: int,
        repository_id: int | None,
    ) -> list[tuple[Repository, RepositoryReadme | None]]:
        missing = or_(
            RepositoryReadme.id.is_(None),
            RepositoryReadme.content.is_(None),
            RepositoryReadme.content == "",
        )
        due = or_(
            RepositoryReadme.id.is_(None),
            RepositoryReadme.next_refresh_at.is_(None),
            RepositoryReadme.next_refresh_at <= now,
        )
        public_or_unknown = or_(
            RepositoryReadme.id.is_(None),
            RepositoryReadme.is_private.is_(False),
            and_(RepositoryReadme.is_private.is_(True), due),
        )
        query = (
            select(Repository, RepositoryReadme)
            .outerjoin(RepositoryReadme, RepositoryReadme.repository_id == Repository.id)
            .where(public_or_unknown, due)
            .order_by(
                case((missing, 0), else_=1),
                RepositoryReadme.next_refresh_at.asc().nullsfirst(),
                Repository.stars_count.desc(),
                Repository.id,
            )
            .limit(limit)
        )
        if repository_id is not None:
            query = query.where(Repository.id == repository_id)
        return list(session.execute(query).all())

    def _collection_active(self, session, now: datetime) -> bool:
        cutoff = now - timedelta(hours=get_settings().readme_collection_pause_hours)
        jobs = session.scalars(
            select(JobRun).where(
                JobRun.task_name.in_(("capture_daily_snapshots", "discover_candidates", "collect_daily_update")),
                JobRun.status.in_(("queued", "running", "waiting")),
            )
        ).all()
        for job in jobs:
            started = _utc(job.started_at) if job.started_at else None
            if started and started >= cutoff:
                resume_at = (job.progress or {}).get("resume_at")
                if job.status in {"running", "queued"} or not resume_at or _utc(datetime.fromisoformat(resume_at)) > now:
                    return True
        return False

    def _readme_waiting(self, session, now: datetime) -> datetime | None:
        jobs = session.scalars(
            select(JobRun).where(
                JobRun.task_name == "refresh_repository_readmes",
                JobRun.status == "waiting",
                JobRun.cancel_requested.is_(False),
            )
        ).all()
        for job in jobs:
            resume_at = (job.progress or {}).get("resume_at")
            if resume_at and _utc(datetime.fromisoformat(resume_at)) > now:
                return _utc(datetime.fromisoformat(resume_at))
        return None

    def _ensure_quota(self, client: GitHubClient) -> None:
        settings = get_settings()
        if client.has_quota(settings.readme_quota_reserve):
            return
        raise GitHubRateLimitError(
            "GitHub README quota reserve reached",
            max(30, _quota_resume_at(client, datetime.now(UTC)).timestamp() - datetime.now(UTC).timestamp()),
        )

    def _start_job(self, session, key: str, repository_id: int | None, now: datetime) -> JobRun | None:
        job = session.scalar(select(JobRun).where(JobRun.job_key == key).with_for_update())
        if job and job.cancel_requested:
            return None
        if job and job.status == "completed":
            return None
        if job and job.status == "running" and job.started_at and _utc(job.started_at) > now - timedelta(
            seconds=get_settings().readme_job_timeout_seconds
        ):
            return None
        if job is None:
            job = JobRun(job_key=key, task_name="refresh_repository_readmes", attempts=0)
            session.add(job)
        progress = dict(job.progress or {})
        progress.update(
            repository_id=repository_id,
            deadline=(now + timedelta(seconds=get_settings().readme_job_timeout_seconds)).isoformat(),
            heartbeat_at=now.isoformat(),
        )
        job.progress = progress
        job.status = "running"
        job.attempts += 1
        job.started_at = now
        job.finished_at = None
        job.error_message = None
        session.commit()
        return job

    def _wait_job(self, session, job: JobRun, resume_at: datetime, reason: str) -> None:
        job.status = "waiting"
        job.progress = dict(job.progress or {}, wait_reason=reason, resume_at=resume_at.isoformat())
        job.error_message = reason
        session.commit()

    def _set_waiting(self, job_key: str, resume_at: datetime, reason: str) -> None:
        with self._session_factory() as session:
            job = session.scalar(select(JobRun).where(JobRun.job_key == job_key))
            if job:
                self._wait_job(session, job, resume_at, reason)

    def _advance_progress(self, job_key: str, counts: dict[str, int]) -> None:
        with self._session_factory() as session:
            job = session.scalar(select(JobRun).where(JobRun.job_key == job_key))
            if job:
                progress = dict(job.progress or {})
                progress.update(counts, processed=sum(counts.values()), heartbeat_at=datetime.now(UTC).isoformat())
                job.progress = progress
                session.commit()

    def _cancelled(self, job_key: str) -> bool:
        with self._session_factory() as session:
            return bool(session.scalar(select(JobRun.cancel_requested).where(JobRun.job_key == job_key)))

    def _finish_job(self, session, job: JobRun, status: str, message: str | None = None) -> None:
        job.status = status
        job.error_message = message[:2000] if message else None
        job.finished_at = datetime.now(UTC)
        session.commit()

    def _finish_by_key(self, job_key: str, status: str, message: str | None) -> None:
        with self._session_factory() as session:
            job = session.scalar(select(JobRun).where(JobRun.job_key == job_key))
            if job:
                self._finish_job(session, job, status, message)

    def _mark_failure(
        self,
        repository_id: int,
        error_code: str,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            stored = session.scalar(
                select(RepositoryReadme).where(RepositoryReadme.repository_id == repository_id)
            )
            if stored is None:
                stored = RepositoryReadme(repository_id=repository_id)
                session.add(stored)
            stored.last_checked_at = now
            stored.last_error_code = error_code
            stored.error_message = message[:2000]
            stored.failure_count = int(stored.failure_count or 0) + 1
            delay = retry_after or get_settings().readme_failure_backoff_seconds * min(
                2 ** max(stored.failure_count - 1, 0), 288
            )
            stored.next_refresh_at = now + timedelta(seconds=min(delay, 86_400))
            session.commit()

    def _mark_private(self, repository_id: int, code: str, message: str) -> None:
        with self._session_factory() as session:
            readme_row = session.scalar(
                select(RepositoryReadme).where(RepositoryReadme.repository_id == repository_id)
            )
            if readme_row:
                readme_row.is_private = True
                readme_row.visibility = "private"
                readme_row.last_public_verified_at = datetime.now(UTC)
            session.commit()
        invalidate_readme_cache(repository_id)
        self._mark_failure(repository_id, code, message)


def _save_result(stored: RepositoryReadme, result, now: datetime) -> None:
    if result.root_etag:
        stored.root_etag = result.root_etag
    if result.root_entries is not None:
        stored.root_entries = _safe_root_entries(result.root_entries)
    stored.root_checked_at = now
    stored.root_selected_path = result.path
    stored.last_checked_at = now
    stored.next_refresh_at = now + timedelta(hours=get_settings().readme_refresh_hours)
    stored.last_error_code = None
    stored.error_message = None
    stored.failure_count = 0
    if result.not_modified:
        if not stored.content:
            raise GitHubClientError("GitHub returned 304 without a stored README body")
        if result.etag:
            stored.readme_etag = result.etag
        if result.readme_endpoint:
            stored.readme_endpoint = result.readme_endpoint
        return
    stored.content = result.content
    stored.path = result.path
    stored.html_url = result.html_url
    stored.readme_etag = result.etag
    stored.readme_endpoint = result.readme_endpoint
    stored.last_success_at = now


def _safe_root_entries(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {key: str(entry[key]) for key in ("name", "type", "path") if entry.get(key) is not None}
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") and entry.get("type")
    ]


def _error_code(exc: Exception) -> str:
    if isinstance(exc, GitHubRateLimitError):
        return "rate_limited"
    if isinstance(exc, GitHubAuthenticationError):
        return "github_authentication"
    if isinstance(exc, GitHubPermissionError):
        return "github_permission"
    if isinstance(exc, GitHubNotFoundError):
        return "github_not_found"
    if isinstance(exc, GitHubTransientError):
        return "github_transient"
    if isinstance(exc, httpx.RequestError):
        return "transport_error"
    return "github_error"


def _job_key(now: datetime, repository_id: int | None) -> str:
    if repository_id is not None:
        return f"readme:repo:{repository_id}:{now.strftime('%Y%m%d%H%M')}"
    interval = get_settings().readme_refresh_interval_seconds
    bucket = int(now.timestamp()) // interval * interval
    return f"readme:batch:{datetime.fromtimestamp(bucket, UTC).strftime('%Y%m%d%H%M')}"


def _job_status(job: JobRun) -> dict[str, Any]:
    return {
        "job_key": job.job_key,
        "task_name": job.task_name,
        "status": job.status,
        "attempts": job.attempts,
        "progress": job.progress or {},
        "error_message": job.error_message,
        "cancel_requested": job.cancel_requested,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _quota_resume_at(client: GitHubClient, now: datetime) -> datetime:
    if client.rate_limit_reset:
        return max(now + timedelta(seconds=30), datetime.fromtimestamp(client.rate_limit_reset + 1, UTC))
    return now + timedelta(minutes=5)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _pace(next_request: list[float], interval: float) -> None:
    delay = next_request[0] - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    next_request[0] = time.monotonic() + interval
