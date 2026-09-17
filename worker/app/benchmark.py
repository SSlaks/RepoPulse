"""Real API benchmark in a disposable database; never publishes production rankings."""

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Timer
from time import monotonic

import httpx
from app.clients.github import GitHubClient, GitHubRateLimitError
from app.config import get_settings
from app.database import get_sync_session
from app.models import Base, JobRun, Repository, RepoSnapshot
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from worker.app.snapshots import (
    SnapshotCancelled,
    SnapshotCollector,
    SnapshotIncomplete,
)
from worker.app.tasks import _persist_ranking, _upsert_repository


def run_benchmark(limit: int, timeout: int) -> dict:
    settings = get_settings()
    with get_sync_session() as source:
        query = select(Repository).where(
            Repository.is_fork.is_(False), Repository.archived.is_(False),
            Repository.disabled.is_(False),
        ).order_by(Repository.id)
        rows = [
            {column.name: getattr(repo, column.name) for column in Repository.__table__.columns}
            for repo in source.scalars(query)
            if repo.full_name.lower() not in settings.repository_exclusions
        ]
    rows = rows[:limit] if limit else rows
    if not settings.github_tokens:
        return {"status": "deferred", "reason": "No authenticated API token"}
    response = httpx.get(
        "https://api.github.com/rate_limit", timeout=20,
        headers={"Authorization": f"Bearer {settings.github_tokens[0]}"},
    )
    response.raise_for_status()
    quota = response.json()["resources"]["core"]
    if quota["remaining"] < len(rows) + 200:
        return {"status": "deferred", "reason": "Insufficient API quota", "quota": quota}
    with TemporaryDirectory(prefix="repopulse-benchmark-") as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'benchmark.sqlite3'}")
        factory = sessionmaker(engine, expire_on_commit=False)
        Base.metadata.create_all(engine)
        captured_at = datetime.now(UTC)
        key = f"snapshot:all:{captured_at.date().isoformat()}"
        with factory() as session:
            session.add_all(Repository(**row) for row in rows)
            session.add(JobRun(job_key=key, task_name="benchmark", status="running",
                               attempts=1, started_at=captured_at))
            session.commit()
        def cancel():
            with factory() as session:
                session.execute(update(JobRun).values(cancel_requested=True))
                session.commit()
        timer = Timer(timeout, cancel)
        timer.start()
        started = monotonic()
        print(json.dumps({"status": "started", "repositories": len(rows), "quota": quota}), flush=True)
        status, error = "completed", None
        ranking_seconds = None
        try:
            SnapshotCollector(captured_at, factory, GitHubClient, _upsert_repository).run()
            snapshot_seconds = monotonic() - started
            rank_started = monotonic()
            with factory() as session:
                ranking_as_of = captured_at.replace(tzinfo=None)
                for period in (1, 7, 14, 30):
                    _persist_ranking(session, period, ranking_as_of)
                    session.commit()
            ranking_seconds = monotonic() - rank_started
        except (SnapshotCancelled, SnapshotIncomplete, GitHubRateLimitError) as exc:
            status, error = type(exc).__name__, str(exc)
            snapshot_seconds = monotonic() - started
        finally:
            timer.cancel()
            timer.join()
        with factory() as session:
            saved = session.scalar(select(func.count()).select_from(RepoSnapshot))
            progress = session.scalar(select(JobRun.progress))
        engine.dispose()
    return {
        "status": status, "error": error, "repositories": len(rows), "saved": saved,
        "snapshot_seconds": round(snapshot_seconds, 2),
        "ranking_seconds": round(ranking_seconds, 2) if ranking_seconds is not None else None,
        "progress": progress, "database": "disposable SQLite; production read-only",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200, help="0 selects all eligible repositories")
    parser.add_argument("--timeout", type=int, default=2100)
    args = parser.parse_args()
    if args.limit < 0 or args.timeout <= 0:
        parser.error("limit must be non-negative and timeout must be positive")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result = run_benchmark(args.limit, args.timeout)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["status"] == "completed" else 1)
