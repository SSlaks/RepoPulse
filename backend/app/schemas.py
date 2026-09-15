from datetime import datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChartRange = Literal["30d", "90d", "365d"]


class PeriodDays(IntEnum):
    ONE = 1
    SEVEN = 7
    FOURTEEN = 14
    THIRTY = 30


class RankingItemResponse(BaseModel):
    rank: int
    previous_rank: int | None = None
    full_name: str
    owner: str
    owner_github_id: int | None = None
    name: str
    description: str | None
    language: str | None
    topics: list[str]
    total_stars: int
    star_delta: int
    growth_rate: float | None
    baseline_available: bool
    last_updated_at: datetime | None
    github_url: str


class CollectionSummary(BaseModel):
    expected: int
    succeeded: int
    missing: int
    completeness_percent: float
    is_partial: bool


class RankingMeta(BaseModel):
    period_days: PeriodDays
    as_of: datetime
    baseline_at: datetime | None
    generated_at: datetime
    coverage: int
    total: int
    page: int
    limit: int
    data_mode: Literal["demo", "live"]
    collection: CollectionSummary | None = None


class RankingResponse(BaseModel):
    data: list[RankingItemResponse]
    meta: RankingMeta


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str
    owner: str
    owner_github_id: int | None = None
    name: str
    description: str | None
    html_url: str
    language: str | None
    topics: list[str]
    license_name: str | None
    stars_count: int
    forks_count: int
    open_issues_count: int
    pushed_at: datetime | None
    github_created_at: datetime | None
    first_tracked_at: datetime
    last_seen_at: datetime
    history_available_from: datetime


class ReadmeResponse(BaseModel):
    repository: str
    path: str
    content: str
    html_url: str


class SnapshotResponse(BaseModel):
    captured_at: datetime
    stars_count: int
    forks_count: int


class SnapshotSeriesResponse(BaseModel):
    repository: str
    range: ChartRange
    data: list[SnapshotResponse]


class FilterOption(BaseModel):
    value: str
    label: str
    count: int = Field(ge=0)


class FilterResponse(BaseModel):
    languages: list[FilterOption]
    topics: list[FilterOption]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
