"""Low-volume recovery checks for quarantined repositories."""

from datetime import UTC, datetime, timedelta

import httpx
from app.clients.github import GitHubAuthenticationError, GitHubClientError, GitHubRateLimitError
from app.models import Repository
from sqlalchemy import select

from worker.app.availability import error_code, restore
from worker.app.checkpoints import save_snapshot
from worker.app.collection import RepositoryRequests


def probe_repositories() -> dict[str, int]:
    from worker.app import tasks
    settings = tasks.get_settings()
    if not settings.repository_probe_enabled:
        return {"checked": 0, "restored": 0}
    with tasks._task_lock("repository-collection", timeout=7800, required=True) as acquired:
        if not acquired:
            return {"checked": 0, "restored": 0}
        return _probe_batch(tasks, settings)


def _probe_batch(tasks, settings) -> dict[str, int]:
    checked = restored = 0
    requests = RepositoryRequests(1, 1, tasks.GitHubClient)
    try:
        with tasks.get_sync_session() as session:
            repositories = session.scalars(select(Repository).where(
                Repository.availability_status == "quarantined",
                Repository.next_probe_at <= datetime.now(UTC),
                Repository.archived.is_(False), Repository.disabled.is_(False),
                Repository.is_fork.is_(False),
            ).order_by(Repository.next_probe_at).limit(50)).all()
            for repository in repositories:
                if repository.full_name.lower() in settings.repository_exclusions:
                    continue
                checked += 1
                try:
                    data = requests.submit(repository.full_name).result()
                except GitHubRateLimitError as exc:
                    repository.next_probe_at = datetime.now(UTC) + timedelta(seconds=exc.retry_after)
                    session.commit()
                    break
                except GitHubAuthenticationError:
                    # Leave due state intact so a corrected credential can resume the scan.
                    raise
                except (GitHubClientError, httpx.RequestError) as exc:
                    _probe_failed(repository, error_code(exc))
                else:
                    if data is None:
                        break
                    _probe_succeeded(session, repository, data, tasks)
                    restored += 1
                session.commit()
    finally:
        requests.close()
    return {"checked": checked, "restored": restored}


def _probe_failed(repository: Repository, code: str) -> None:
    now = datetime.now(UTC)
    repository.last_checked_at, repository.last_error_code = now, code
    if code == "404":
        repository.probe_backoff_step = min(repository.probe_backoff_step + 1, 2)
        repository.next_probe_at = now + timedelta(days=(1, 3, 7)[repository.probe_backoff_step])
    else:
        repository.next_probe_at = now + timedelta(hours=1)


def _probe_succeeded(session, repository, data, tasks) -> None:
    now = datetime.now(UTC)
    tasks._upsert_repository(session, data, repository=repository)
    restore(repository, now)
    save_snapshot(session, repository.id, data, now)
