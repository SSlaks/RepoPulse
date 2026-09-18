import logging

from celery import Celery
from celery.exceptions import OperationalError
from redis import Redis
from redis.exceptions import RedisError

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
task_sender = Celery("repopulse-api", broker=settings.redis_url)
task_sender.conf.update(
    broker_connection_timeout=1,
    task_ignore_result=True,
)


def enqueue_avatar_refresh(owner_id: int) -> bool:
    try:
        task_sender.send_task(
            "worker.app.avatar_tasks.refresh_avatar",
            args=[owner_id],
            ignore_result=True,
            retry=False,
        )
    except OperationalError:
        logger.warning(
            "Avatar refresh enqueue failed",
            extra={"owner_id": owner_id},
            exc_info=True,
        )
        return False
    return True


def enqueue_readme_refresh(repository_id: int) -> bool:
    """Queue one README refresh per repository cooldown window.

    The marker is deliberately short lived and is separate from the Celery
    payload.  A Redis failure is reported to the caller; the database body is
    never replaced by this best-effort request path.
    """
    marker = f"readme:queue:v2:{repository_id}"
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )
    marked = False
    try:
        marked = bool(
            client.set(
                marker,
                "1",
                ex=settings.readme_queue_cooldown_seconds,
                nx=True,
            )
        )
        if not marked:
            return True
        task_sender.send_task(
            "worker.app.readme_tasks.refresh_repository_readmes",
            args=[repository_id],
            ignore_result=True,
            retry=False,
        )
    except (OperationalError, RedisError):
        logger.warning(
            "README refresh enqueue failed",
            extra={"repository_id": repository_id},
            exc_info=True,
        )
        if marked:
            try:
                client.delete(marker)
            except RedisError:
                logger.debug("README queue marker cleanup failed", exc_info=True)
        return False
    finally:
        client.close()
    return True
