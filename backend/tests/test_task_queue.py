from unittest.mock import Mock

from app import task_queue
from app.config import get_settings
from celery.exceptions import OperationalError


def test_avatar_sender_uses_configured_redis_broker() -> None:
    assert task_queue.task_sender.conf.broker_url == get_settings().redis_url


def test_avatar_refresh_uses_named_worker_task(monkeypatch) -> None:
    send_task = Mock()
    monkeypatch.setattr(task_queue.task_sender, "send_task", send_task)

    assert task_queue.enqueue_avatar_refresh(42) is True
    send_task.assert_called_once_with(
        "worker.app.avatar_tasks.refresh_avatar",
        args=[42],
        ignore_result=True,
        retry=False,
    )


def test_avatar_refresh_broker_failure_is_non_fatal(monkeypatch) -> None:
    send_task = Mock(side_effect=OperationalError("broker unavailable"))
    monkeypatch.setattr(task_queue.task_sender, "send_task", send_task)

    assert task_queue.enqueue_avatar_refresh(42) is False
