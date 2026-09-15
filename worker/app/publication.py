"""Publish a complete atomic batch once enough of the frozen daily cohort is available."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from app.models import JobRun, RankingItem, RankingRun, RepoSnapshot, SnapshotRequest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PublicationCandidate:
    repository_ids: list[int]
    expected: int
    reason: str | None

    @property
    def fingerprint(self) -> str:
        return sha256(
            ",".join(str(value) for value in self.repository_ids).encode("ascii")
        ).hexdigest()

    @property
    def summary(self) -> dict:
        succeeded = len(self.repository_ids)
        return {"expected": self.expected, "succeeded": succeeded,
                "missing": self.expected - succeeded,
                "completeness_percent": round(succeeded * 100 / self.expected, 2),
                "is_partial": succeeded < self.expected, "fingerprint": self.fingerprint}


def candidate(session: Session, job: JobRun, as_of: datetime, threshold: int) -> PublicationCandidate:
    cohort = set(session.scalars(select(SnapshotRequest.repository_id).where(
        SnapshotRequest.job_run_id == job.id)))
    saved = set(session.scalars(select(RepoSnapshot.repository_id).where(
        RepoSnapshot.snapshot_date == as_of.date(), RepoSnapshot.source == "github_api",
        RepoSnapshot.repository_id.in_(cohort))))
    reason = None
    if job.cancel_requested or job.status == "cancelled":
        reason = "cancelled"
    elif job.status == "failed" or (job.progress or {}).get("global_failure"):
        reason = "global_failure"
    elif job.status not in {"waiting", "partial", "completed"}:
        reason = "collection_in_progress"
    elif not cohort:
        reason = "empty_cohort"
    elif len(saved) * 100 < len(cohort) * threshold:
        reason = "below_completeness_threshold"
    return PublicationCandidate(sorted(saved), len(cohort), reason)


def needs_publication(job: JobRun, result: PublicationCandidate) -> bool:
    progress = job.progress or {}
    if result.reason or progress.get("published_fingerprint") == result.fingerprint:
        return False
    attempts = progress.get("publication_attempts_by_version", {})
    return attempts.get(result.fingerprint, 0) < 5


def publish(as_of: datetime, automatic: bool) -> dict[str, int | str]:
    from worker.app import tasks
    key = f"snapshot:all:{as_of.date().isoformat()}"
    # The shared collection lock also protects Repository updates and in-flight snapshots.
    with (tasks._task_lock("repository-collection", timeout=3600, required=True) as collection_lock,
          tasks._task_lock(f"publish:{key}", timeout=3600, required=True) as publication_lock):
        if not collection_lock or not publication_lock:
            return {"status": "duplicate", "count": 0}
        return _publish_locked(tasks, key, as_of, automatic)


def _locked_job(session: Session, key: str) -> JobRun | None:
    return session.scalar(select(JobRun).where(JobRun.job_key == key).with_for_update()
                          .execution_options(populate_existing=True))


def _publish_locked(tasks, key: str, as_of: datetime, automatic: bool) -> dict[str, int | str]:
    with tasks.get_sync_session() as session:
        job = _locked_job(session, key)
        if job is None:
            return {"status": "blocked", "count": 0}
        result = candidate(session, job, as_of, tasks.get_settings().ranking_min_completeness_percent)
        if result.reason:
            job.progress = dict(job.progress or {}, publication_reason=result.reason)
            session.commit()
            return {"status": "blocked", "count": 0}
        if (job.progress or {}).get("published_fingerprint") == result.fingerprint:
            return {"status": "duplicate", "count": 0}
        attempts = dict((job.progress or {}).get("publication_attempts_by_version", {}))
        if automatic and attempts.get(result.fingerprint, 0) >= 5:
            return {"status": "failed", "count": 0}
        attempts[result.fingerprint] = attempts.get(result.fingerprint, 0) + 1
        job.progress = dict(job.progress or {}, publication="pending",
                            publication_attempts_by_version=attempts)
        session.commit()
        try:
            job = _locked_job(session, key)
            if job is None:
                return {"status": "blocked", "count": 0}
            fresh = candidate(session, job, as_of,
                              tasks.get_settings().ranking_min_completeness_percent)
            if fresh.reason or fresh.fingerprint != result.fingerprint:
                return {"status": "blocked", "count": 0}
            count = _replace_batch(session, tasks, as_of, result)
            job.progress = dict(job.progress or {}, publication="published",
                                published_fingerprint=result.fingerprint,
                                publication_reason=None, publication_error=None)
            session.commit()
            return {"status": "ok", "count": count}
        except Exception as exc:
            session.rollback()
            job = _locked_job(session, key)
            if job is not None:
                job.progress = dict(job.progress or {}, publication="failed"
                                    if attempts[result.fingerprint] >= 5 else "pending",
                                    publication_error=str(exc)[:2000])
                session.commit()
            raise


def _replace_batch(session: Session, tasks, as_of: datetime, result: PublicationCandidate) -> int:
    old_ids = list(session.scalars(select(RankingRun.id).where(
        RankingRun.as_of == as_of, RankingRun.period_days.in_((1, 7, 14, 30)))))
    session.execute(delete(RankingItem).where(RankingItem.ranking_run_id.in_(old_ids)))
    session.execute(delete(RankingRun).where(RankingRun.id.in_(old_ids)))
    count = sum(tasks._persist_ranking(session, period, as_of, result.repository_ids)
                for period in (1, 7, 14, 30))
    if count != len(result.repository_ids) * 4:
        raise ValueError("Ranking batch does not match the published repository cohort")
    session.flush()
    published_at = datetime.now(UTC)
    for run in session.scalars(select(RankingRun).where(RankingRun.as_of == as_of)):
        run.collection_summary = result.summary
        run.published_at = published_at
    return count
