from pathlib import Path
from typing import Self
from unittest.mock import Mock

import pytest
from app.api.routes import repositories
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse


class StubSession:
    def __init__(self, avatar_url: str | None) -> None:
        self.avatar_url = avatar_url

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def scalar(self, _: object) -> str | None:
        return self.avatar_url


@pytest.mark.asyncio
async def test_cached_avatar_is_returned_without_refresh(tmp_path: Path, monkeypatch) -> None:
    cached = tmp_path / "1.img"
    cached.write_bytes(b"cached-avatar")
    enqueue = Mock()
    monkeypatch.setattr(repositories, "avatar_path", lambda _: cached)
    monkeypatch.setattr(repositories, "is_fresh", lambda _: True)
    monkeypatch.setattr(repositories, "enqueue_avatar_refresh", enqueue)
    background_tasks = BackgroundTasks()

    response = await repositories.get_avatar(1, background_tasks)

    assert isinstance(response, FileResponse)
    assert response.status_code == 200
    assert not background_tasks.tasks
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_stale_avatar_is_served_while_refresh_is_queued(
    tmp_path: Path, monkeypatch
) -> None:
    cached = tmp_path / "2.img"
    cached.write_bytes(b"stale-avatar")
    enqueue = Mock(return_value=True)
    monkeypatch.setattr(repositories, "avatar_path", lambda _: cached)
    monkeypatch.setattr(repositories, "is_fresh", lambda _: False)
    monkeypatch.setattr(repositories, "enqueue_avatar_refresh", enqueue)
    background_tasks = BackgroundTasks()

    response = await repositories.get_avatar(2, background_tasks)
    await background_tasks()

    assert isinstance(response, FileResponse)
    assert response.status_code == 200
    enqueue.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_missing_known_avatar_is_queued_and_returns_no_store_404(
    tmp_path: Path, monkeypatch
) -> None:
    enqueue = Mock(return_value=False)
    monkeypatch.setattr(repositories, "avatar_path", lambda _: tmp_path / "missing.img")
    monkeypatch.setattr(
        repositories,
        "async_session_factory",
        lambda: StubSession("https://avatars.githubusercontent.com/u/3?v=4"),
    )
    monkeypatch.setattr(repositories, "enqueue_avatar_refresh", enqueue)
    background_tasks = BackgroundTasks()

    response = await repositories.get_avatar(3, background_tasks)
    await background_tasks()

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    enqueue.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_unknown_avatar_returns_404_without_queueing(tmp_path: Path, monkeypatch) -> None:
    enqueue = Mock()
    monkeypatch.setattr(repositories, "avatar_path", lambda _: tmp_path / "missing.img")
    monkeypatch.setattr(repositories, "async_session_factory", lambda: StubSession(None))
    monkeypatch.setattr(repositories, "enqueue_avatar_refresh", enqueue)
    background_tasks = BackgroundTasks()

    response = await repositories.get_avatar(4, background_tasks)

    assert response.status_code == 404
    assert not background_tasks.tasks
    enqueue.assert_not_called()
