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


def test_readme_refresh_uses_cooldown_marker_and_named_task(monkeypatch) -> None:
    class MarkerRedis:
        def __init__(self) -> None:
            self.marked = False

        def set(self, key: str, value: str, **kwargs: object) -> bool:
            assert key == "readme:queue:v2:42"
            assert value == "1"
            assert kwargs["nx"] is True
            assert int(kwargs["ex"]) > 0
            if self.marked:
                return False
            self.marked = True
            return True

        def close(self) -> None:
            return None

    marker = MarkerRedis()
    send_task = Mock()
    monkeypatch.setattr(task_queue.Redis, "from_url", lambda *_, **__: marker)
    monkeypatch.setattr(task_queue.task_sender, "send_task", send_task)

    assert task_queue.enqueue_readme_refresh(42) is True
    assert task_queue.enqueue_readme_refresh(42) is True
    send_task.assert_called_once_with(
        "worker.app.readme_tasks.refresh_repository_readmes",
        args=[42],
        ignore_result=True,
        retry=False,
    )


def test_readme_queue_failure_removes_cooldown_marker(monkeypatch) -> None:
    class MarkerRedis:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def set(self, *_: object, **__: object) -> bool:
            return True

        def delete(self, key: str) -> None:
            self.deleted.append(key)

        def close(self) -> None:
            return None

    marker = MarkerRedis()
    monkeypatch.setattr(task_queue.Redis, "from_url", lambda *_, **__: marker)
    monkeypatch.setattr(
        task_queue.task_sender,
        "send_task",
        Mock(side_effect=OperationalError("broker unavailable")),
    )

    assert task_queue.enqueue_readme_refresh(42) is False
    assert marker.deleted == ["readme:queue:v2:42"]
