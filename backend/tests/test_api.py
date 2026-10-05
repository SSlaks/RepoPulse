import asyncio

import pytest
from app.database import async_session_factory
from app.main import app
from app.models import RankingItem, RankingRun, Repository
from fastapi.testclient import TestClient
from sqlalchemy import select

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


@pytest.mark.parametrize("filter_name", ["q", "language", "topic"])
@pytest.mark.parametrize("dash_first", [False, True])
def test_ranking_cache_distinguishes_missing_filter_from_dash(
    filter_name: str, dash_first: bool
) -> None:
    with TestClient(app) as client:
        # 使用另一种分页大小取得完整对照，避免预先填充待验证的缓存键。
        control = client.get("/api/v1/rankings", params={"period": 7, "limit": 50})
        assert control.status_code == 200
        all_items = control.json()["data"]
        assert control.json()["meta"]["total"] == len(all_items)
        if filter_name == "q":
            dash_items = [
                item for item in all_items
                if "-" in f"{item['full_name']} {item['description'] or ''}"
            ]
        else:
            dash_items = []
        assert len(dash_items) < len(all_items)

        values = ["-", None] if dash_first else [None, "-"]
        for value in values:
            params = {"period": "7", "limit": "15"}
            if value is not None:
                params[filter_name] = value
            response = client.get("/api/v1/rankings", params=params)
            assert response.status_code == 200
            expected = all_items if value is None else dash_items
            assert response.json()["meta"]["total"] == len(expected)
            assert response.json()["data"] == expected[:15]


@pytest.mark.parametrize(
    ("period", "published_stars", "latest_stars", "expected_count"),
    [(1, 98, 105, 0), (7, 105, 98, 1), (14, 100, 0, 1), (30, 99, 200, 0)],
)
def test_minimum_stars_uses_published_ranking(
    period: int, published_stars: int, latest_stars: int, expected_count: int
) -> None:
    async def advance_repository_data() -> None:
        async with async_session_factory() as session:
            repository = await session.scalar(
                select(Repository).where(Repository.full_name == "fastapi/fastapi")
            )
            run = await session.scalar(
                select(RankingRun)
                .where(RankingRun.period_days == period, RankingRun.status == "ready")
                .order_by(RankingRun.as_of.desc())
                .limit(1)
            )
            assert repository is not None and run is not None
            item = await session.scalar(
                select(RankingItem).where(
                    RankingItem.ranking_run_id == run.id,
                    RankingItem.repository_id == repository.id,
                )
            )
            assert item is not None
            item.end_stars = published_stars
            repository.stars_count = latest_stars
            await session.commit()

    asyncio.run(advance_repository_data())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/rankings",
            params={"period": period, "q": "fastapi/fastapi", "minStars": 100},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == expected_count
    assert len(payload["data"]) == expected_count
    if expected_count:
        assert payload["data"][0]["total_stars"] == published_stars
