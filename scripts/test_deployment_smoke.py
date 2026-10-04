#!/usr/bin/env python3
"""Read-only RepoPulse deployment smoke checks run inside the API container."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never

import httpx
from app.schemas import (
    FilterResponse,
    HealthResponse,
    RankingResponse,
    RepositoryResponse,
    SnapshotSeriesResponse,
)
from pydantic import BaseModel, ConfigDict, ValidationError

REQUEST_TIMEOUT_SECONDS: Final = 10.0
DEMO_PERIODS: Final = (1, 7, 14, 30)
DEMO_REPOSITORY_COUNT: Final = 12
DEMO_PYTHON_COUNT: Final = 4
DEMO_FASTAPI: Final = "fastapi/fastapi"
DEMO_FASTAPI_STARS: Final = 81_240
DEMO_SNAPSHOT_MINIMUM: Final = 29
INTERNAL_ACQUIRE_BODY: Final = {"policy": "ai_generate", "kind": "internal"}


class SmokePhase(StrEnum):
    DEMO = "demo"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    phase: SmokePhase
    api_base: str
    frontend_base: str


@dataclass(frozen=True, slots=True)
class RequestSpec:
    check: str
    url: str
    expected_status: int = 200
    method: str = "GET"
    json_body: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str


@dataclass(frozen=True, slots=True)
class SmokeReport:
    phase: SmokePhase
    checks: tuple[CheckResult, ...]


class SmokeFailure(RuntimeError):
    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check


class ApiErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class ApiErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: ApiErrorBody


def _request(client: httpx.Client, spec: RequestSpec) -> httpx.Response:
    try:
        response = client.request(spec.method, spec.url, json=spec.json_body)
    except httpx.HTTPError as exc:
        raise SmokeFailure(spec.check, f"transport failure: {exc}") from exc
    if response.status_code != spec.expected_status:
        raise SmokeFailure(
            spec.check, f"expected HTTP {spec.expected_status}, received {response.status_code}"
        )
    return response


def _parse[ModelT: BaseModel](
    response: httpx.Response, model: type[ModelT], check: str
) -> ModelT:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        raise SmokeFailure(check, "response body did not match the expected schema") from exc


def _expect_health(client: httpx.Client, spec: RequestSpec, service: str) -> CheckResult:
    payload = _parse(_request(client, spec), HealthResponse, spec.check)
    if payload.status != "ok" or payload.service != service:
        raise SmokeFailure(spec.check, "unexpected health identity")
    return CheckResult(spec.check)


def _expect_error(client: httpx.Client, spec: RequestSpec, code: str) -> CheckResult:
    payload = _parse(_request(client, spec), ApiErrorEnvelope, spec.check)
    if payload.error.code != code:
        raise SmokeFailure(spec.check, f"unexpected error code {payload.error.code!r}")
    return CheckResult(spec.check)


def _direct_api_checks(client: httpx.Client, config: SmokeConfig) -> list[CheckResult]:
    api = config.api_base
    return [
        _expect_health(client, RequestSpec("direct_api_health", f"{api}/api/v1/health"), "api"),
        _expect_health(client, RequestSpec("direct_api_ready", f"{api}/api/v1/ready"), "api"),
    ]


def _frontend_checks(client: httpx.Client, config: SmokeConfig) -> list[CheckResult]:
    front = config.frontend_base
    health = _expect_health(
        client, RequestSpec("frontend_health", f"{front}/api/health"), "frontend"
    )
    ready = _expect_health(
        client, RequestSpec("proxied_api_ready", f"{front}/api/v1/ready"), "api"
    )
    root = _request(client, RequestSpec("frontend_root_html", f"{front}/"))
    if not root.headers.get("content-type", "").startswith("text/html"):
        raise SmokeFailure("frontend_root_html", "frontend root did not return HTML")
    return [health, ready, CheckResult("frontend_root_html")]


def _shared_error_checks(client: httpx.Client, config: SmokeConfig) -> list[CheckResult]:
    api, front = config.api_base, config.frontend_base
    validation = _expect_error(
        client,
        RequestSpec("proxied_validation_error", f"{front}/api/v1/rankings?period=10", 422),
        "VALIDATION_ERROR",
    )
    unknown = _expect_error(
        client,
        RequestSpec("unknown_repository", f"{api}/api/v1/repos/missing-owner/missing-repository", 404),
        "HTTP_404",
    )
    acquire = _expect_error(
        client,
        RequestSpec(
            "internal_acquire_requires_token",
            f"{api}/internal/limits/acquire",
            expected_status=403,
            method="POST",
            json_body=INTERNAL_ACQUIRE_BODY,
        ),
        "HTTP_403",
    )
    _request(
        client,
        RequestSpec(
            "frontend_internal_not_exposed",
            f"{front}/internal/limits/acquire",
            expected_status=404,
            method="POST",
            json_body=INTERNAL_ACQUIRE_BODY,
        ),
    )
    return [validation, unknown, acquire, CheckResult("frontend_internal_not_exposed")]


def _demo_rankings(client: httpx.Client, config: SmokeConfig) -> list[CheckResult]:
    results: list[CheckResult] = []
    for period in DEMO_PERIODS:
        name = f"demo_rankings_{period}d"
        spec = RequestSpec(name, f"{config.frontend_base}/api/v1/rankings?period={period}")
        payload = _parse(_request(client, spec), RankingResponse, name)
        if payload.meta.period_days != period or payload.meta.total != DEMO_REPOSITORY_COUNT:
            raise SmokeFailure(name, "demo ranking period or total changed")
        if payload.meta.coverage != DEMO_REPOSITORY_COUNT or payload.meta.data_mode != "demo":
            raise SmokeFailure(name, "demo ranking coverage or data mode changed")
        if len(payload.data) != DEMO_REPOSITORY_COUNT:
            raise SmokeFailure(name, "demo ranking did not contain the full catalog")
        if len({item.full_name for item in payload.data}) != DEMO_REPOSITORY_COUNT:
            raise SmokeFailure(name, "demo ranking contained duplicate repositories")
        results.append(CheckResult(name))
    return results


def _demo_filters(client: httpx.Client, config: SmokeConfig) -> CheckResult:
    name = "demo_filters_python"
    spec = RequestSpec(name, f"{config.frontend_base}/api/v1/filters")
    payload = _parse(_request(client, spec), FilterResponse, name)
    counts = {option.value: option.count for option in payload.languages}
    if counts.get("Python") != DEMO_PYTHON_COUNT:
        raise SmokeFailure(name, "demo Python language count changed")
    return CheckResult(name)


def _demo_repository(client: httpx.Client, config: SmokeConfig) -> CheckResult:
    name = "demo_repository_fastapi"
    spec = RequestSpec(name, f"{config.frontend_base}/api/v1/repos/{DEMO_FASTAPI}")
    payload = _parse(_request(client, spec), RepositoryResponse, name)
    if payload.full_name != DEMO_FASTAPI or payload.stars_count != DEMO_FASTAPI_STARS:
        raise SmokeFailure(name, "demo FastAPI repository identity changed")
    return CheckResult(name)


def _demo_snapshots(client: httpx.Client, config: SmokeConfig) -> CheckResult:
    name = "demo_snapshots_fastapi"
    url = f"{config.frontend_base}/api/v1/repos/{DEMO_FASTAPI}/snapshots?range=30d"
    payload = _parse(_request(client, RequestSpec(name, url)), SnapshotSeriesResponse, name)
    if payload.repository != DEMO_FASTAPI or payload.range != "30d":
        raise SmokeFailure(name, "snapshot series identity changed")
    if len(payload.data) < DEMO_SNAPSHOT_MINIMUM:
        raise SmokeFailure(name, "snapshot series was shorter than the demo history")
    timestamps = [point.captured_at for point in payload.data]
    if timestamps != sorted(timestamps):
        raise SmokeFailure(name, "snapshot timestamps were not ascending")
    if payload.data[-1].stars_count != DEMO_FASTAPI_STARS:
        raise SmokeFailure(name, "latest snapshot stars changed")
    return CheckResult(name)


def _demo_checks(client: httpx.Client, config: SmokeConfig) -> list[CheckResult]:
    return [
        *_demo_rankings(client, config),
        _demo_filters(client, config),
        _demo_repository(client, config),
        _demo_snapshots(client, config),
    ]


def _production_checks(client: httpx.Client, config: SmokeConfig) -> list[CheckResult]:
    front = config.frontend_base
    absent = _expect_error(
        client,
        RequestSpec("production_rankings_unseeded", f"{front}/api/v1/rankings?period=7", 503),
        "HTTP_503",
    )
    repository = _expect_error(
        client,
        RequestSpec("production_demo_repository_absent", f"{front}/api/v1/repos/{DEMO_FASTAPI}", 404),
        "HTTP_404",
    )
    empty = _parse(
        _request(client, RequestSpec("production_filters_empty", f"{front}/api/v1/filters")),
        FilterResponse,
        "production_filters_empty",
    )
    if empty.languages or empty.topics:
        raise SmokeFailure("production_filters_empty", "fresh production database exposed filters")
    return [absent, repository, CheckResult("production_filters_empty")]


def run_smoke(config: SmokeConfig, client: httpx.Client) -> SmokeReport:
    checks = _direct_api_checks(client, config)
    checks.extend(_frontend_checks(client, config))
    checks.extend(_shared_error_checks(client, config))
    match config.phase:
        case SmokePhase.DEMO:
            checks.extend(_demo_checks(client, config))
        case SmokePhase.PRODUCTION:
            checks.extend(_production_checks(client, config))
        case unreachable:
            assert_never(unreachable)
    return SmokeReport(phase=config.phase, checks=tuple(checks))


def _parse_config(argv: Sequence[str] | None) -> SmokeConfig:
    parser = argparse.ArgumentParser(description="RepoPulse deployment smoke checks")
    parser.add_argument("--phase", required=True, choices=[phase.value for phase in SmokePhase])
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend-base", default="http://frontend:3000")
    args = parser.parse_args(argv)
    return SmokeConfig(
        phase=SmokePhase(args.phase),
        api_base=args.api_base.rstrip("/"), frontend_base=args.frontend_base.rstrip("/"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = _parse_config(argv)
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        ) as client:
            report = run_smoke(config, client)
    except SmokeFailure as exc:
        print(f"deployment-smoke: {exc}", file=sys.stderr)
        return 1
    checks = [item.name for item in report.checks]
    summary = {"phase": report.phase.value, "status": "ok", "checks": checks}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
