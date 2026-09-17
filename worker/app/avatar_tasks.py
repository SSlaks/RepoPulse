import httpx
from app.avatar_cache import download_avatar
from app.database import get_sync_session
from app.models import Repository
from celery import Task
from sqlalchemy import select

from worker.app.celery_app import celery_app


@celery_app.task(
    bind=True, max_retries=2, name="worker.app.avatar_tasks.refresh_avatar"
)
def refresh_avatar(self: Task, owner_id: int) -> dict[str, int | str]:
    with get_sync_session() as session:
        url = session.scalar(
            select(Repository.owner_avatar_url).where(
                Repository.owner_github_id == owner_id
            )
        )
    if not url:
        return {"status": "missing", "owner_id": owner_id}
    try:
        download_avatar(owner_id, url)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))
    return {"status": "ok", "owner_id": owner_id}


@celery_app.task(name="worker.app.avatar_tasks.warmup_avatars")
def warmup_avatars() -> dict[str, int]:
    with get_sync_session() as session:
        owners = session.scalars(
            select(Repository.owner_github_id)
            .where(Repository.owner_github_id.is_not(None))
            .order_by(Repository.stars_count.desc())
            .limit(100)
        ).all()
    for owner_id in dict.fromkeys(owners):
        if owner_id is not None:
            refresh_avatar.delay(owner_id)
    return {"count": len(owners)}
