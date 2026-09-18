"""Celery entry points for bounded README refresh work."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import get_settings
from celery import Task

from worker.app.celery_app import celery_app
from worker.app.readme import ReadmeRefreshService, readme_task_lock

README_TASK_TIMEOUT = get_settings().readme_job_timeout_seconds


@celery_app.task(
    bind=True,
    soft_time_limit=README_TASK_TIMEOUT,
    time_limit=README_TASK_TIMEOUT + 60,
    name="worker.app.readme_tasks.refresh_repository_readmes",
)
def refresh_repository_readmes(
    self: Task,
    repository_id: int | None = None,
    limit: int | None = None,
) -> dict[str, int | str]:
    del self
    timeout = get_settings().readme_job_timeout_seconds + 60
    with readme_task_lock(timeout) as acquired:
        if not acquired:
            return {"status": "duplicate", "count": 0}
        return ReadmeRefreshService().run_batch(
            repository_id=repository_id,
            limit=limit,
            now=datetime.now(UTC),
        )
