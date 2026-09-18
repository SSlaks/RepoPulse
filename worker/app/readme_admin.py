"""README warmup and durable job inspection CLI.

Examples inside the worker container::

    python -m worker.app.readme_admin warmup --limit 100
    python -m worker.app.readme_admin status
    python -m worker.app.readme_admin cancel --job-key readme:batch:202609171200
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from app.database import get_sync_session
from app.models import JobRun, Repository, RepositoryReadme
from celery.result import AsyncResult
from sqlalchemy import and_, func, or_, select

from worker.app.readme import ReadmeRefreshService
from worker.app.readme_tasks import refresh_repository_readmes


def _status(job_key: str | None) -> dict[str, object]:
    service = ReadmeRefreshService()
    jobs = service.status(job_key)
    with get_sync_session() as session:
        total = int(session.scalar(select(func.count(Repository.id))) or 0)
        stored = int(session.scalar(select(func.count(RepositoryReadme.id))) or 0)
        available_condition = and_(
            RepositoryReadme.content.is_not(None),
            RepositoryReadme.content != "",
            RepositoryReadme.is_private.is_(False),
            RepositoryReadme.visibility == "public",
            RepositoryReadme.last_public_verified_at.is_not(None),
        )
        hidden_condition = or_(
            RepositoryReadme.is_private.is_(True),
            and_(
                RepositoryReadme.content.is_not(None),
                RepositoryReadme.content != "",
                or_(
                    RepositoryReadme.visibility.is_(None),
                    RepositoryReadme.visibility != "public",
                    RepositoryReadme.last_public_verified_at.is_(None),
                ),
            ),
        )
        available = int(
            session.scalar(
                select(func.count(RepositoryReadme.id)).where(available_condition)
            )
            or 0
        )
        hidden = int(
            session.scalar(
                select(func.count(RepositoryReadme.id)).where(hidden_condition)
            )
            or 0
        )
        next_refresh = session.scalar(
            select(func.min(RepositoryReadme.next_refresh_at)).where(
                available_condition,
                RepositoryReadme.next_refresh_at.is_not(None),
            )
        )
        waiting = session.scalars(
            select(JobRun).where(
                JobRun.task_name == "refresh_repository_readmes",
                JobRun.status == "waiting",
                JobRun.cancel_requested.is_(False),
            )
        ).all()
    resume_at = _earliest_resume(waiting)
    return {
        "repositories": {
            "total": total,
            "with_readme_row": stored,
            "available": available,
            "missing": max(total - available - hidden, 0),
            "hidden": hidden,
        },
        "next_refresh_at": _iso(next_refresh),
        "resume_at": resume_at,
        "jobs": jobs,
    }


def _earliest_resume(jobs: Sequence[JobRun]) -> str | None:
    now = datetime.now(UTC)
    values: list[datetime] = []
    for job in jobs:
        value = (job.progress or {}).get("resume_at")
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if parsed > now:
            values.append(parsed)
    return min(values).isoformat() if values else None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RepoPulse README operations")
    commands = parser.add_subparsers(dest="command", required=True)

    warmup = commands.add_parser("warmup", help="enqueue one bounded README refresh")
    warmup.add_argument("--limit", type=_positive_int, default=None)
    warmup.add_argument("--repository-id", type=_positive_int, default=None)

    status = commands.add_parser("status", help="show refresh jobs and body coverage")
    status.add_argument("--job-key", default=None)

    cancel = commands.add_parser("cancel", help="request cancellation for active refresh jobs")
    cancel.add_argument("--job-key", default=None)

    args = parser.parse_args(argv)
    if args.command == "warmup":
        result: AsyncResult = refresh_repository_readmes.delay(args.repository_id, args.limit)
        print(json.dumps({"status": "queued", "task_id": result.id}, ensure_ascii=False))
        return 0
    if args.command == "cancel":
        print(json.dumps({"status": "cancel_requested", "count": ReadmeRefreshService().cancel(args.job_key)}))
        return 0
    print(json.dumps(_status(args.job_key), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
