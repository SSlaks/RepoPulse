from dataclasses import dataclass
from datetime import datetime, timedelta

SUPPORTED_PERIODS = (1, 7, 14, 30)


@dataclass(frozen=True)
class SnapshotPoint:
    captured_at: datetime
    stars_count: int


@dataclass(frozen=True)
class RepositorySeries:
    repository_id: int
    full_name: str
    snapshots: tuple[SnapshotPoint, ...]
    current_stars: int | None = None


@dataclass(frozen=True)
class RankingResult:
    repository_id: int
    full_name: str
    start_stars: int
    end_stars: int
    net_delta: int
    growth_rate: float | None
    baseline_available: bool


def calculate_ranking(
    series: list[RepositorySeries], period_days: int, as_of: datetime
) -> list[RankingResult]:
    if period_days not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported ranking period: {period_days}")

    results = [result for item in series if (result := _calculate_item(item, period_days, as_of))]
    return sorted(
        results,
        key=lambda item: (
            not item.baseline_available,
            -item.net_delta,
            -(item.growth_rate if item.growth_rate is not None else float("-inf")),
            -item.end_stars,
            item.full_name.lower(),
        ),
    )


def _calculate_item(
    series: RepositorySeries, period_days: int, as_of: datetime
) -> RankingResult | None:
    eligible = sorted(
        (snapshot for snapshot in series.snapshots if snapshot.captured_at <= as_of),
        key=lambda snapshot: snapshot.captured_at,
    )
    end = eligible[-1] if eligible else None
    if end is None and series.current_stars is not None:
        end = SnapshotPoint(captured_at=as_of, stars_count=series.current_stars)
    if end is None:
        return None

    target = as_of - timedelta(days=period_days)
    baseline = _find_baseline(eligible, target)
    if baseline is None:
        return RankingResult(
            repository_id=series.repository_id,
            full_name=series.full_name,
            start_stars=end.stars_count,
            end_stars=end.stars_count,
            net_delta=0,
            growth_rate=None,
            baseline_available=False,
        )

    delta = end.stars_count - baseline.stars_count
    growth_rate = delta / baseline.stars_count if baseline.stars_count > 0 else None
    return RankingResult(
        repository_id=series.repository_id,
        full_name=series.full_name,
        start_stars=baseline.stars_count,
        end_stars=end.stars_count,
        net_delta=delta,
        growth_rate=growth_rate,
        baseline_available=True,
    )


def _find_baseline(snapshots: list[SnapshotPoint], target: datetime) -> SnapshotPoint | None:
    candidates = [snapshot for snapshot in snapshots if snapshot.captured_at <= target]
    if not candidates:
        return None
    baseline = candidates[-1]
    return baseline if baseline.captured_at.date() == target.date() else None
