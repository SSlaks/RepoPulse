import pytest
from app.clients.github import GitHubReadmeData
from app.main import app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("seeded_api_database")


def test_health_and_ranking_endpoints() -> None:
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        ranking = client.get("/api/v1/rankings", params={"period": 14})
        one_day = client.get("/api/v1/rankings", params={"period": 1})

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "api"}
    assert ranking.status_code == 200
    payload = ranking.json()
    assert payload["meta"]["period_days"] == 14
    assert payload["meta"]["limit"] == 15
    assert payload["meta"]["data_mode"] in {"demo", "live"}
    assert payload["data"][0]["star_delta"] >= payload["data"][1]["star_delta"]
    assert payload["data"][0]["baseline_available"] is True

    assert one_day.status_code == 200
    assert one_day.json()["meta"]["period_days"] == 1


def test_filters_repository_and_snapshot_endpoints() -> None:
    with TestClient(app) as client:
        filters = client.get("/api/v1/filters")
        repository = client.get("/api/v1/repos/fastapi/fastapi")
        snapshots = client.get("/api/v1/repos/fastapi/fastapi/snapshots", params={"range": "30d"})

    assert filters.status_code == 200
    assert any(item["value"] == "Python" for item in filters.json()["languages"])
    assert repository.status_code == 200
    assert repository.json()["full_name"] == "fastapi/fastapi"
    assert snapshots.status_code == 200
    assert snapshots.json()["range"] == "30d"
    assert len(snapshots.json()["data"]) >= 30


def test_validation_error_uses_public_error_shape() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/rankings", params={"period": 10})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_readme_endpoint_returns_decoded_github_content(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.services.catalog.limiter.acquire", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.catalog.limiter.release", AsyncMock(return_value=True))

    class StubCache:
        async def get(self, key: str) -> None:
            return None

        async def set(self, key: str, value: dict, ttl_seconds: int = 300) -> None:
            return None

    class StubGitHubClient:
        def readme(self, full_name: str) -> GitHubReadmeData:
            assert full_name == "test-owner/test-repo"
            return GitHubReadmeData(
                repository=full_name,
                path="README.zh-CN.md",
                content="# 中文 README",
                html_url="https://github.com/test-owner/test-repo/blob/main/README.zh-CN.md",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.services.catalog.GitHubClient", StubGitHubClient)
    monkeypatch.setattr("app.services.catalog.response_cache", StubCache())
    with TestClient(app) as client:
        response = client.get("/api/v1/repos/test-owner/test-repo/readme")

    assert response.status_code == 200
    assert response.json() == {
        "repository": "test-owner/test-repo",
        "path": "README.zh-CN.md",
        "content": "# 中文 README",
        "html_url": "https://github.com/test-owner/test-repo/blob/main/README.zh-CN.md",
    }
