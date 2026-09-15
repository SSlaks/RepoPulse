"""Repository health transitions shared by collection and isolated probes."""

from datetime import UTC, datetime, timedelta
from math import ceil

import httpx
from app.clients.github import (
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubTransientError,
)
from app.models import Repository, SnapshotRequest


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def error_code(error: Exception) -> str:
    for error_type, code in (
        (GitHubAuthenticationError, "401"), (GitHubNotFoundError, "404"),
        (GitHubPermissionError, "403"), (GitHubTransientError, "transient"),
        (httpx.RequestError, "transient"),
    ):
        if isinstance(error, error_type):
            return code
    return "client_error"


def restore(repository: Repository, now: datetime) -> None:
    repository.availability_status = "active"
    repository.consecutive_not_found = 0
    repository.first_not_found_at = None
    repository.last_not_found_date = None
    repository.next_probe_at = None
    repository.probe_backoff_step = 0
    repository.last_error_code = None
    repository.last_checked_at = now
    repository.last_success_at = now


def record_failure(repository: Repository, code: str, now: datetime,
                   auto_quarantine: bool) -> None:
    repository.last_checked_at = now
    repository.last_error_code = code
    if code != "404":
        repository.consecutive_not_found = 0
        repository.first_not_found_at = None
        repository.last_not_found_date = None
        if repository.availability_status == "suspect":
            repository.availability_status = "active"
        return
    if repository.last_not_found_date == now.date():
        return
    repository.last_not_found_date = now.date()
    repository.first_not_found_at = repository.first_not_found_at or now
    repository.consecutive_not_found += 1
    repository.availability_status = "suspect"
    elapsed = now - utc(repository.first_not_found_at)
    if auto_quarantine and repository.consecutive_not_found >= 3 and elapsed >= timedelta(hours=48):
        repository.availability_status = "quarantined"
        repository.next_probe_at = now + timedelta(days=1)
        repository.probe_backoff_step = 0


def finalize_health(session, rows: list[SnapshotRequest], auto_quarantine: bool) -> bool:
    access_errors = sum(row.error_code in {"403", "404"} for row in rows)
    frozen = access_errors >= max(10, ceil(len(rows) * 0.05))
    for row in rows:
        if row.availability_applied or not row.checked_at:
            continue
        repository = session.get(Repository, row.repository_id)
        if row.error_code and not frozen:
            record_failure(repository, row.error_code, utc(row.checked_at), auto_quarantine)
        row.availability_applied = True
    return frozen
