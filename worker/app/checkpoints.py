"""Durable daily membership, retries and publication eligibility."""

from datetime import UTC, datetime, timedelta

from app.models import JobRun, Repository, RepoSnapshot, SnapshotRequest
from sqlalchemy import select

from worker.app.availability import utc


def initialize(session, job: JobRun, captured_at: datetime, settings) -> list[SnapshotRequest]:
    progress = initialize_progress(job.progress, captured_at, settings)
    if not progress.get("membership_frozen"):
        repositories = session.scalars(select(Repository).where(
            Repository.is_fork.is_(False), Repository.archived.is_(False),
            Repository.disabled.is_(False), Repository.availability_status != "quarantined",
        ).order_by(Repository.id)).all()
        session.add_all(SnapshotRequest(job_run_id=job.id, repository_id=repo.id)
                        for repo in repositories
                        if repo.full_name.lower() not in settings.repository_exclusions)
        progress.update(membership_frozen=True, publication="blocked")
        job.progress = progress
        session.commit()
    return list(session.scalars(select(SnapshotRequest).where(
        SnapshotRequest.job_run_id == job.id)).all())


def initialize_progress(progress, captured_at: datetime, settings) -> dict:
    progress = dict(progress or {})
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    progress.setdefault("as_of", captured_at.isoformat())
    progress.setdefault("deadline", min(
        now + timedelta(hours=settings.snapshot_deadline_hours), midnight).isoformat())
    return progress


def reconcile_saved(session, rows: list[SnapshotRequest], captured_at: datetime) -> None:
    saved = set(session.scalars(select(RepoSnapshot.repository_id).where(
        RepoSnapshot.snapshot_date == captured_at.date(), RepoSnapshot.source == "github_api",
    )))
    for row in rows:
        if row.repository_id in saved:
            row.status = "saved"
            row.error_code = None
            row.error_message = None
            row.next_attempt_at = None


def record_error(row: SnapshotRequest, code: str, message: str, now: datetime) -> None:
    row.error_code, row.error_message = code, message[:2000]
    row.checked_at, row.availability_applied = now, False
    row.status = "failed"
    row.next_attempt_at = None
    if code == "transient" and row.retry_count < 2:
        row.status = "waiting"
        row.next_attempt_at = now + timedelta(minutes=(10, 30)[row.retry_count])


def due(row: SnapshotRequest, now: datetime, manual: bool) -> bool:
    if row.status == "saved":
        return False
    if manual or row.status == "pending":
        return True
    return row.status == "waiting" and (
        row.next_attempt_at is None or utc(row.next_attempt_at) <= now)


def save_snapshot(session, repository_id: int, data, captured_at: datetime) -> None:
    snapshot = session.scalar(select(RepoSnapshot).where(
        RepoSnapshot.repository_id == repository_id,
        RepoSnapshot.snapshot_date == captured_at.date()))
    if snapshot is not None and snapshot.source == "github_api":
        return
    if snapshot is None:
        snapshot = RepoSnapshot(repository_id=repository_id, snapshot_date=captured_at.date())
        session.add(snapshot)
    snapshot.captured_at = captured_at
    snapshot.stars_count = data.stars_count
    snapshot.forks_count = data.forks_count
    snapshot.source = "github_api"
