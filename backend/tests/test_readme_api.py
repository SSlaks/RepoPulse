import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from app.clients.github import GitHubClient
from app.database import async_session_factory
from app.internal.limiter import LimiterUnavailable
from app.main import app
from app.models import Repository, RepositoryReadme
from app.task_queue import task_sender
from fastapi.testclient import TestClient
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("seeded_api_database")


def _reject_github_readme(self: GitHubClient, full_name: str) -> None:
    raise AssertionError(f"README request reached GitHub: {full_name}")


def _store_readme(*, private: bool = False, content: str | None = "# 持久中文正文\n\nsecret-for-private-test") -> None:
    async def write() -> None:
        async with async_session_factory() as session:
            repository = await session.scalar(
                select(Repository).where(Repository.full_name == "fastapi/fastapi")
            )
            assert repository is not None
            session.add(
                RepositoryReadme(
                    repository_id=repository.id,
                    path="README.zh-CN.md",
                    content=content,
                    html_url="https://github.com/fastapi/fastapi/blob/main/README.zh-CN.md",
                    is_private=private,
                    visibility="private" if private else "public",
                    last_public_verified_at=datetime.now(UTC) if not private else None,
                    last_success_at=datetime.now(UTC) if content else None,
                    next_refresh_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
            await session.commit()

    asyncio.run(write())


def test_unknown_repository_does_not_fetch_or_enqueue_readme(monkeypatch: pytest.MonkeyPatch) -> None:
    send_task = Mock()
    monkeypatch.setattr(GitHubClient, "readme", _reject_github_readme)
    monkeypatch.setattr(task_sender, "send_task", send_task)

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/unknown/missing/readme")

    assert response.status_code == 404
    send_task.assert_not_called()


def test_known_repository_without_readme_queues_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    class MarkerRedis:
        def set(self, *_: object, **__: object) -> bool:
            return True

        def close(self) -> None:
            return None

    send_task = Mock()
    monkeypatch.setattr(GitHubClient, "readme", _reject_github_readme)
    monkeypatch.setattr(task_sender, "send_task", send_task)
    monkeypatch.setattr("app.task_queue.Redis.from_url", lambda *_, **__: MarkerRedis())

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/fastapi/fastapi/readme")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert int(response.headers["retry-after"]) > 0
    assert response.json()["error"]["message"] == "README 正在准备，请稍后刷新"
    send_task.assert_called_once()


def test_missing_readme_reports_unavailable_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.readme.enqueue_readme_refresh", Mock(return_value=False))

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/fastapi/fastapi/readme")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "120"
    assert response.json()["error"]["message"] == "README 暂时不可用，请稍后重试"


def test_failed_readme_waits_until_retry_without_requeue(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_readme(content=None)
    enqueue = Mock()
    monkeypatch.setattr("app.services.readme.enqueue_readme_refresh", enqueue)

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/fastapi/fastapi/readme")

    assert response.status_code == 503
    assert int(response.headers["retry-after"]) > 0
    assert response.json()["error"]["message"] == "README 暂时无法更新，系统会自动重试"
    enqueue.assert_not_called()


def test_persisted_readme_survives_redis_and_github_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_readme()
    monkeypatch.setattr(GitHubClient, "readme", _reject_github_readme)
    send_task = Mock(side_effect=AssertionError("unexpected refresh enqueue"))
    monkeypatch.setattr(task_sender, "send_task", send_task)
    monkeypatch.setattr(
        "app.services.readme.limiter.record",
        AsyncMock(side_effect=LimiterUnavailable("Redis unavailable")),
    )

    async def unavailable_cache(_: str) -> None:
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr("app.services.readme.response_cache.get", unavailable_cache)

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/fastapi/fastapi/readme")

    assert response.status_code == 200
    assert response.json() == {
        "repository": "fastapi/fastapi",
        "path": "README.zh-CN.md",
        "content": "# 持久中文正文\n\nsecret-for-private-test",
        "html_url": "https://github.com/fastapi/fastapi/blob/main/README.zh-CN.md",
    }
    send_task.assert_not_called()


def test_private_repository_never_exposes_persisted_or_legacy_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_readme(private=True)
    monkeypatch.setattr(GitHubClient, "readme", _reject_github_readme)
    send_task = Mock()
    monkeypatch.setattr(task_sender, "send_task", send_task)

    async def poisoned_cache(_: str) -> dict[str, str]:
        return {
            "repository": "fastapi/fastapi",
            "path": "README.md",
            "content": "legacy-redis-secret",
            "html_url": "https://github.com/fastapi/fastapi/blob/main/README.md",
        }

    monkeypatch.setattr("app.services.readme.response_cache.get", poisoned_cache)

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/fastapi/fastapi/readme")

    assert response.status_code in {403, 404}
    assert "secret-for-private-test" not in response.text
    assert "legacy-redis-secret" not in response.text
    send_task.assert_not_called()
