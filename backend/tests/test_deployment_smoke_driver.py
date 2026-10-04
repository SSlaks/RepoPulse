import importlib.util
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from app.schemas import (
    FilterOption,
    FilterResponse,
    HealthResponse,
    PeriodDays,
    RankingItemResponse,
    RankingMeta,
    RankingResponse,
    RepositoryResponse,
    SnapshotResponse,
    SnapshotSeriesResponse,
)
from pydantic import BaseModel

_DRIVER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "test_deployment_smoke.py"


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deployment_smoke_driver", _DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()

API_HOST = "api.test"
FRONTEND_HOST = "frontend.test"
API_BASE = f"http://{API_HOST}"
FRONTEND_BASE = f"http://{FRONTEND_HOST}"
DEMO_COUNT = 12
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
Handler = Callable[[httpx.Request], httpx.Response]


def _json_response(status: int, payload: BaseModel) -> httpx.Response:
    return httpx.Response(
        status,
        content=payload.model_dump_json().encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _error_response(status: int, code: str) -> httpx.Response:
    body = json.dumps({"error": {"code": code, "message": "mocked"}}).encode("utf-8")
    return httpx.Response(status, content=body, headers={"content-type": "application/json"})


def _html_response(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        content=b"<!DOCTYPE html><html><body>mocked</body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )


def _ranking_response(period: int) -> RankingResponse:
    data = [
        RankingItemResponse(
            rank=index, full_name=f"demo/repo-{index}", owner="demo", name=f"repo-{index}",
            description=None, language="Python", topics=[], total_stars=1_000 + index,
            star_delta=index, growth_rate=None, baseline_available=True,
            last_updated_at=AS_OF, github_url=f"https://github.com/demo/repo-{index}",
        )
        for index in range(1, DEMO_COUNT + 1)
    ]
    return RankingResponse(
        data=data,
        meta=RankingMeta(
            period_days=PeriodDays(period),
            as_of=AS_OF,
            baseline_at=None,
            generated_at=AS_OF,
            coverage=DEMO_COUNT,
            total=DEMO_COUNT,
            page=1,
            limit=15,
            data_mode="demo",
        ),
    )


def _filter_response() -> FilterResponse:
    return FilterResponse(
        languages=[FilterOption(value="Python", label="Python", count=4)],
        topics=[],
    )


def _repository_response() -> RepositoryResponse:
    return RepositoryResponse(
        full_name="fastapi/fastapi", owner="fastapi", name="fastapi", description=None,
        html_url="https://github.com/fastapi/fastapi", language="Python", topics=["python"],
        license_name="MIT", stars_count=81_240, forks_count=1, open_issues_count=1,
        first_tracked_at=AS_OF - timedelta(days=365), last_seen_at=AS_OF,
        history_available_from=AS_OF - timedelta(days=365),
        pushed_at=None, github_created_at=None,
    )


def _snapshot_response() -> SnapshotSeriesResponse:
    return SnapshotSeriesResponse(
        repository="fastapi/fastapi",
        range="30d",
        data=[
            SnapshotResponse(
                captured_at=AS_OF - timedelta(days=29 - index),
                stars_count=81_240,
                forks_count=1,
            )
            for index in range(30)
        ],
    )


def _demo_route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path == "/internal/limits/acquire":
        if request.url.host == FRONTEND_HOST:
            return _html_response(404)
        return _error_response(403, "HTTP_403")
    if request.url.host == FRONTEND_HOST and path == "/api/health":
        return _json_response(200, HealthResponse(status="ok", service="frontend"))
    if path == "/":
        return _html_response(200)
    if path in {"/api/v1/health", "/api/v1/ready"}:
        return _json_response(200, HealthResponse(status="ok", service="api"))
    if path == "/api/v1/rankings":
        period = int(request.url.params.get("period", "7"))
        if period not in {1, 7, 14, 30}:
            return _error_response(422, "VALIDATION_ERROR")
        return _json_response(200, _ranking_response(period))
    if path == "/api/v1/filters":
        return _json_response(200, _filter_response())
    if path == "/api/v1/repos/fastapi/fastapi":
        return _json_response(200, _repository_response())
    if path == "/api/v1/repos/fastapi/fastapi/snapshots":
        return _json_response(200, _snapshot_response())
    if path == "/api/v1/repos/missing-owner/missing-repository":
        return _error_response(404, "HTTP_404")
    raise AssertionError(f"unexpected demo request: {request.method} {request.url}")


def _production_route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path == "/internal/limits/acquire":
        if request.url.host == FRONTEND_HOST:
            return _html_response(404)
        return _error_response(403, "HTTP_403")
    if request.url.host == FRONTEND_HOST and path == "/api/health":
        return _json_response(200, HealthResponse(status="ok", service="frontend"))
    if path == "/":
        return _html_response(200)
    if path in {"/api/v1/health", "/api/v1/ready"}:
        return _json_response(200, HealthResponse(status="ok", service="api"))
    if path == "/api/v1/rankings":
        period = int(request.url.params.get("period", "7"))
        if period == 10:
            return _error_response(422, "VALIDATION_ERROR")
        return _error_response(503, "HTTP_503")
    if path == "/api/v1/filters":
        return _json_response(200, FilterResponse(languages=[], topics=[]))
    if path == "/api/v1/repos/fastapi/fastapi":
        return _error_response(404, "HTTP_404")
    if path == "/api/v1/repos/missing-owner/missing-repository":
        return _error_response(404, "HTTP_404")
    raise AssertionError(f"unexpected production request: {request.method} {request.url}")


def _with_override(
    base: Handler, override: Callable[[httpx.Request], httpx.Response | None]
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        replacement = override(request)
        if replacement is not None:
            return replacement
        return base(request)

    return handler


def _run_smoke(phase_value: str, handler: Handler) -> int:
    config = driver.SmokeConfig(
        phase=driver.SmokePhase(phase_value), api_base=API_BASE, frontend_base=FRONTEND_BASE
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler), timeout=1.0, trust_env=False
    ) as client:
        report = driver.run_smoke(config, client)
    return len(report.checks)


def test_valid_demo_deployment_passes() -> None:
    assert _run_smoke("demo", _demo_route) > 0


def test_valid_production_deployment_passes() -> None:
    assert _run_smoke("production", _production_route) > 0


def test_unhealthy_ready_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == API_HOST and request.url.path == "/api/v1/ready":
            return _json_response(200, HealthResponse(status="degraded", service="api"))
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", _with_override(_demo_route, override))

    assert error.value.check == "direct_api_ready"


def test_wrong_frontend_identity_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/health":
            return _json_response(200, HealthResponse(status="ok", service="api"))
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", _with_override(_demo_route, override))

    assert error.value.check == "frontend_health"


def test_wrong_demo_count_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v1/rankings" and request.url.params.get("period") == "1":
            payload = _ranking_response(1)
            payload.meta.total = DEMO_COUNT - 1
            return _json_response(200, payload)
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", _with_override(_demo_route, override))

    assert error.value.check == "demo_rankings_1d"


def test_wrong_demo_mode_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v1/rankings" and request.url.params.get("period") == "1":
            payload = _ranking_response(1)
            payload.meta.data_mode = "live"
            return _json_response(200, payload)
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", _with_override(_demo_route, override))

    assert error.value.check == "demo_rankings_1d"


def test_wrong_proxy_error_status_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v1/rankings" and request.url.params.get("period") == "10":
            return _error_response(200, "VALIDATION_ERROR")
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", _with_override(_demo_route, override))

    assert error.value.check == "proxied_validation_error"


def test_unexpected_seed_in_production_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v1/rankings" and request.url.params.get("period") == "7":
            return _json_response(200, _ranking_response(7))
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("production", _with_override(_production_route, override))

    assert error.value.check == "production_rankings_unseeded"


def test_malformed_json_fails_smoke() -> None:
    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == API_HOST and request.url.path == "/api/v1/health":
            return httpx.Response(
                200, content=b"{not-json", headers={"content-type": "application/json"}
            )
        return None

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", _with_override(_demo_route, override))

    assert error.value.check == "direct_api_health"


def test_transport_failure_fails_smoke() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.url}")

    with pytest.raises(driver.SmokeFailure) as error:
        _run_smoke("demo", handler)

    assert error.value.check == "direct_api_health"
