from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.ranking.calculator import RepositorySeries, SnapshotPoint, calculate_ranking

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def series(
    repository_id: int,
    name: str,
    points: list[tuple[int, int]],
    current_stars: int | None = None,
) -> RepositorySeries:
    return RepositorySeries(
        repository_id=repository_id,
        full_name=name,
        snapshots=tuple(
            SnapshotPoint(captured_at=NOW - timedelta(days=days_ago), stars_count=stars)
            for days_ago, stars in points
        ),
        current_stars=current_stars,
    )


@pytest.mark.parametrize("period", [1, 7, 14, 30])
def test_calculates_supported_periods(period: int) -> None:
    repositories = [series(1, "owner/repo", [(period, 100), (0, 175)])]

    result = calculate_ranking(repositories, period, NOW)

    assert len(result) == 1
    assert result[0].net_delta == 75
    assert result[0].growth_rate == pytest.approx(0.75)


def test_sorts_by_delta_then_growth_rate_then_name() -> None:
    repositories = [
        series(1, "z/repo", [(7, 100), (0, 150)]),
        series(2, "a/repo", [(7, 200), (0, 250)]),
        series(3, "leader/repo", [(7, 100), (0, 180)]),
    ]

    result = calculate_ranking(repositories, 7, NOW)

    assert [item.full_name for item in result] == ["leader/repo", "z/repo", "a/repo"]


def test_marks_series_without_recent_baseline_as_no_growth() -> None:
    stale = series(1, "owner/stale", [(9, 100), (0, 150)])

    result = calculate_ranking([stale], 7, NOW)

    assert len(result) == 1
    assert result[0].net_delta == 0
    assert result[0].baseline_available is False


def test_matches_baseline_by_utc_target_day() -> None:
    as_of = datetime(2026, 9, 6, 0, 30, tzinfo=UTC)
    target_in_new_york = datetime(
        2026, 9, 4, 20, 0, tzinfo=timezone(timedelta(hours=-4))
    )
    repository = RepositorySeries(
        repository_id=1,
        full_name="owner/utc-boundary",
        snapshots=(
            SnapshotPoint(captured_at=target_in_new_york, stars_count=100),
            SnapshotPoint(captured_at=as_of, stars_count=110),
        ),
    )

    result = calculate_ranking([repository], 1, as_of)

    assert result[0].baseline_available is True
    assert result[0].net_delta == 10


def test_uses_latest_target_day_snapshot_before_cutoff() -> None:
    as_of = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    repository = RepositorySeries(
        repository_id=1,
        full_name="owner/intraday",
        snapshots=(
            SnapshotPoint(captured_at=as_of - timedelta(days=1, hours=2), stars_count=90),
            SnapshotPoint(captured_at=as_of - timedelta(days=1, minutes=1), stars_count=100),
            SnapshotPoint(captured_at=as_of - timedelta(days=1) + timedelta(minutes=1), stars_count=1),
            SnapshotPoint(captured_at=as_of, stars_count=115),
        ),
    )

    result = calculate_ranking([repository], 1, as_of)

    assert result[0].start_stars == 100
    assert result[0].net_delta == 15


def test_includes_series_without_snapshots_using_current_stars() -> None:
    result = calculate_ranking(
        [series(1, "owner/new", [], current_stars=240)],
        1,
        NOW,
    )

    assert len(result) == 1
    assert result[0].end_stars == 240
    assert result[0].net_delta == 0
    assert result[0].baseline_available is False


def test_sorts_no_baseline_after_baselined_series() -> None:
    repositories = [
        series(1, "owner/new", [], current_stars=1_000_000),
        series(2, "owner/growing", [(7, 100), (0, 110)]),
    ]

    result = calculate_ranking(repositories, 7, NOW)

    assert [item.full_name for item in result] == ["owner/growing", "owner/new"]


def test_allows_negative_growth_and_zero_baseline() -> None:
    repositories = [
        series(1, "owner/decrease", [(7, 100), (0, 90)]),
        series(2, "owner/new", [(7, 0), (0, 40)]),
    ]

    result = calculate_ranking(repositories, 7, NOW)

    new_repo = next(item for item in result if item.full_name == "owner/new")
    decreased = next(item for item in result if item.full_name == "owner/decrease")
    assert new_repo.baseline_available is True
    assert new_repo.net_delta == 40
    assert new_repo.growth_rate is None
    assert decreased.baseline_available is True
    assert decreased.net_delta == -10
    assert decreased.growth_rate == pytest.approx(-0.1)


def test_no_baseline_does_not_report_synthetic_growth() -> None:
    result = calculate_ranking([series(1, "owner/new", [(0, 50)])], 7, NOW)

    assert result[0].start_stars == 50
    assert result[0].end_stars == 50
    assert result[0].net_delta == 0
    assert result[0].growth_rate is None
    assert result[0].baseline_available is False


def test_rejects_unsupported_period() -> None:
    with pytest.raises(ValueError, match="unsupported ranking period"):
        calculate_ranking([], 10, NOW)
