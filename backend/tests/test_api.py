import pytest
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
