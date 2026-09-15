"""Snapshot persistence and cancellation, coordinated on one database thread."""

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import httpx
from app.clients.github import (
    GitHubClient,
    GitHubClientError,
    GitHubRateLimitError,
    GitHubRepositoryData,
)
from app.config import get_settings
from app.models import JobRun, Repository, RepoSnapshot, SnapshotRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from worker.app import checkpoints
from worker.app.availability import error_code, finalize_health, restore, utc
from worker.app.collection import RepositoryRequests


class SnapshotCancelled(RuntimeError):
    pass


class SnapshotIncomplete(RuntimeError):
    pass


class SnapshotCollector:
    def __init__(
        self, captured_at: datetime, session_factory: Callable[[], Session],
        client_factory: Callable[[], GitHubClient], upsert: Callable[..., Any],
        automatic: bool = False,
    ) -> None:
        self.captured_at = captured_at
        self.job_key = f"snapshot:all:{captured_at.date().isoformat()}"
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.upsert = upsert
        self.settings = get_settings()
        self.count = 0
        self.existing = 0
        self.total = 0
        self.failures: dict[str, str] = {}
        self.started = monotonic()
        self.last_flush = self.started
        self.buffered = 0
        self.cancelled = False
        self.automatic = automatic
        self.rows: dict[int, SnapshotRequest] = {}
        self.deadline: datetime | None = None
        self.expired = False
        self.collection_date = datetime.now(UTC).date()

    def run(self) -> int:
        # Cancellation reads must not implicitly flush each individual snapshot.
        with self.session_factory() as session, session.no_autoflush:
            job = session.scalar(select(JobRun).where(JobRun.job_key == self.job_key))
            progress = dict(job.progress or {}) if job else {}
            concurrency = 1 if progress.get("serial_fallback") else self.settings.snapshot_concurrency
            requests = RepositoryRequests(
                concurrency, self.settings.snapshot_requests_per_second, self.client_factory,
            )
            try:
                self._collect(session, requests, concurrency)
                if (self.rows and not requests.rate_limit and not self.cancelled
                        and not requests.authentication_error):
                    frozen = finalize_health(session, list(self.rows.values()),
                                             self.settings.repository_auto_quarantine)
                    if job:
                        job.progress = dict(job.progress or {}, access_anomaly=frozen)
                self._flush(session, requests)
                if self.cancelled:
                    raise SnapshotCancelled("Cancelled at user request")
                if requests.authentication_error:
                    raise requests.authentication_error
                if requests.rate_limit and self.existing + self.count < self.total:
                    raise requests.rate_limit
                if self.failures or self.existing + self.count < self.total or not self.total:
                    raise SnapshotIncomplete(f"{len(self.failures)} repositories failed")
                return self.count
            finally:
                requests.close()

    def _repositories(self, session: Session) -> list[Repository]:
        job = session.scalar(select(JobRun).where(JobRun.job_key == self.job_key))
        if job:
            rows = checkpoints.initialize(session, job, self.captured_at, self.settings)
            checkpoints.reconcile_saved(session, rows, self.captured_at)
            self.rows = {row.repository_id: row for row in rows}
            self.deadline = datetime.fromisoformat(job.progress["deadline"])
            self.total = len(rows)
            self.existing = sum(row.status == "saved" for row in rows)
            now = datetime.now(UTC)
            ids = [row.repository_id for row in rows
                   if checkpoints.due(row, now, not self.automatic)]
            return list(session.scalars(select(Repository).where(
                Repository.id.in_(ids)).order_by(Repository.id)))
        repositories = session.scalars(select(Repository).where(
            Repository.is_fork.is_(False), Repository.archived.is_(False),
            Repository.disabled.is_(False),
            Repository.availability_status != "quarantined",
        ).order_by(Repository.id)).all()
        exclusions = self.settings.repository_exclusions
        eligible = [repo for repo in repositories if repo.full_name.lower() not in exclusions]
        saved = set(session.scalars(select(RepoSnapshot.repository_id).where(
            RepoSnapshot.snapshot_date == self.captured_at.date(),
            RepoSnapshot.source == "github_api",
        )))
        self.total = len(eligible)
        self.existing = sum(repo.id in saved for repo in eligible)
        return [repo for repo in eligible if repo.id not in saved]

    def _is_cancelled(self, session: Session) -> bool:
        # Select scalar columns so an identity-map cached JobRun cannot hide user cancellation.
        return bool(session.scalar(select(JobRun.cancel_requested).where(
            JobRun.job_key == self.job_key,
        )))

    def _collect(self, session: Session, requests: RepositoryRequests, concurrency: int) -> None:
        remaining = iter(self._repositories(session))
        pending: dict[Future[GitHubRepositoryData | None], Repository] = {}
        exhausted = False
        while True:
            self.cancelled = self.cancelled or self._is_cancelled(session)
            self.expired = bool(self.deadline and datetime.now(UTC) >= utc(self.deadline))
            if self.cancelled or self.expired:
                requests.stop()
            while not exhausted and not requests.stopped.is_set() and len(pending) < concurrency:
                repository = next(remaining, None)
                if repository is None:
                    exhausted = True
                    break
                row = self.rows.get(repository.id)
                if row and row.error_code == "transient" and row.retry_count < 2:
                    row.retry_count += 1
                    session.commit()
                pending[requests.submit(repository.full_name)] = repository
            if not pending:
                break
            done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                self._save_result(session, pending.pop(future), future)
                if self.buffered >= self.settings.snapshot_batch_size:
                    self._flush(session, requests)
            if (self.buffered >= self.settings.snapshot_batch_size
                    or monotonic() - self.last_flush >= self.settings.snapshot_flush_seconds):
                self._flush(session, requests)

    def _save_result(
        self, session: Session, repository: Repository,
        future: Future[GitHubRepositoryData | None],
    ) -> None:
        try:
            data = future.result()
        except GitHubRateLimitError:
            return  # The shared request gate carries the longest required cooldown.
        except (GitHubClientError, httpx.RequestError) as exc:
            self.failures[repository.full_name] = str(exc)
            if row := self.rows.get(repository.id):
                checkpoints.record_error(row, error_code(exc), str(exc), datetime.now(UTC))
            return
        if data is None:
            return
        if datetime.now(UTC).date() != self.collection_date:
            self.failures[repository.full_name] = "Response arrived after UTC day boundary"
            if row := self.rows.get(repository.id):
                checkpoints.record_error(row, "day_boundary", self.failures[repository.full_name],
                                         datetime.now(UTC))
            return
        self.upsert(session, data, repository=repository)
        restore(repository, datetime.now(UTC))
        checkpoints.save_snapshot(session, repository.id, data, self.captured_at)
        self.count += 1
        self.buffered += 1
        if row := self.rows.get(repository.id):
            row.status, row.error_code, row.error_message = "saved", None, None
            row.checked_at, row.next_attempt_at = datetime.now(UTC), None
            row.availability_applied = True

    def _flush(self, session: Session, requests: RepositoryRequests) -> None:
        job = session.scalar(select(JobRun).where(JobRun.job_key == self.job_key))
        if job:
            progress = dict(job.progress or {})
            failures = self.failures
            if self.rows:
                failures = {str(row.repository_id): row.error_message or row.error_code or "unknown"
                            for row in self.rows.values() if row.error_code}
            rate = self.count / max(monotonic() - self.started, 0.001)
            progress.update(
                total=self.total, saved=self.existing + self.count,
                failed=len(failures), failures=failures,
                rate_per_second=round(rate, 3),
                eta_seconds=round((self.total - self.existing - self.count) / rate) if rate else None,
                updated_at=datetime.now(UTC).isoformat(),
                missing=self.total - self.existing - self.count,
                expired=self.expired,
            )
            if requests.rate_limit and requests.rate_limit.secondary:
                progress["serial_fallback"] = True
            job.progress = progress
        session.commit()
        self.buffered = 0
        self.last_flush = monotonic()
