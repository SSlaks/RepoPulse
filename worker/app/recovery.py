"""Bounded recovery dispatch; durable state is authoritative over broker messages."""

from datetime import UTC, datetime, timedelta

from app.models import JobRun, Repository, SnapshotRequest
from sqlalchemy import select

from worker.app.availability import utc
from worker.app.publication import candidate, needs_publication


def automatic_capture_ready(job_key: str) -> bool:
    from worker.app import tasks
    now = datetime.now(UTC)
    with tasks.get_sync_session() as session:
        job = session.scalar(select(JobRun).where(JobRun.job_key == job_key))
        if not job or job.cancel_requested or job.status not in {"running", "waiting"}:
            return False
        progress = job.progress or {}
        if utc(datetime.fromisoformat(progress.get("deadline", now.isoformat()))) <= now:
            return False
        resume_at = progress.get("resume_at")
        return not (resume_at and utc(datetime.fromisoformat(resume_at)) > now)


def finish_partial(job_key: str, message: str) -> dict[str, int | str]:
    from worker.app import tasks
    with tasks.get_sync_session() as session:
        job = session.scalars(select(JobRun).where(JobRun.job_key == job_key).with_for_update()).one()
        progress = dict(job.progress or {})
        now = datetime.now(UTC)
        rows = session.scalars(select(SnapshotRequest).where(
            SnapshotRequest.job_run_id == job.id)).all()
        can_resume = any(row.status in {"waiting", "pending"} for row in rows)
        deadline = datetime.fromisoformat(progress.get("deadline", now.isoformat()))
        job.status = "waiting" if can_resume and now < utc(deadline) else "partial"
        job.finished_at = None if job.status == "waiting" else now
        job.error_message = message
        progress.update(missing=sum(row.status != "saved" for row in rows),
                        quarantined=len(session.scalars(select(Repository.id).where(
                            Repository.availability_status == "quarantined")).all()))
        job.progress = progress
        session.commit()
        return {"status": job.status, "count": sum(row.status == "saved" for row in rows)}


def dispatch_recovery() -> int:
    from worker.app import tasks
    now = datetime.now(UTC)
    dispatched = 0
    with tasks._task_lock("recovery-dispatch", timeout=240, required=True) as acquired:
        if not acquired:
            return 0
        with tasks.get_sync_session() as session:
            jobs = session.scalars(select(JobRun).where(
                JobRun.task_name == "capture_daily_snapshots",
                JobRun.status.in_(("running", "waiting", "partial", "completed")),
                JobRun.cancel_requested.is_(False),
            )).all()
            for job in jobs:
                session.refresh(job, with_for_update=True)
                if _dispatch_job(session, job, now):
                    dispatched += 1
            session.commit()
    return dispatched


def _dispatch_job(session, job: JobRun, now: datetime) -> bool:
    from worker.app import tasks
    progress = dict(job.progress or {})
    as_of = progress.get("as_of")
    if not as_of:
        return False
    if job.cancel_requested or job.status in {"failed", "cancelled"}:
        return False
    result = candidate(session, job, datetime.fromisoformat(as_of),
                       tasks.get_settings().ranking_min_completeness_percent)
    published = needs_publication(job, result)
    if published:
        tasks.publish_daily_rankings.delay(as_of, automatic=True)
    if job.status in {"completed", "partial"}:
        return published
    deadline = datetime.fromisoformat(progress["deadline"])
    if utc(deadline) <= now:
        # A live collector drains in-flight requests itself; don't race its final commit.
        if job.status == "running" and job.started_at and (
            now - utc(job.started_at) < timedelta(seconds=7800)
        ):
            return False
        job.status, job.finished_at = "partial", now
        job.error_message = "Collection deadline reached"
        job.progress = dict(progress, capture_reason="deadline")
        result = candidate(session, job, datetime.fromisoformat(as_of),
                           tasks.get_settings().ranking_min_completeness_percent)
        if not published and needs_publication(job, result):
            tasks.publish_daily_rankings.delay(as_of, automatic=True)
            return True
        return published
    if (job.status == "running" and job.started_at
            and now - utc(job.started_at) < timedelta(seconds=7800)):
        return False
    resume_at = progress.get("resume_at")
    if resume_at and datetime.fromisoformat(resume_at) > now:
        return published
    rows = session.scalars(select(SnapshotRequest).where(
        SnapshotRequest.job_run_id == job.id)).all()
    from worker.app.checkpoints import due
    if progress.get("membership_frozen") and (not rows or not (
        any(due(row, now, False) for row in rows) or all(row.status == "saved" for row in rows)
    )):
        return published
    tasks.capture_daily_snapshots.delay(as_of, automatic=True)
    return True
