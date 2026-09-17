from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from app.clients.github import GitHubRateLimitError
from app.config import Settings
from app.models import Base, RankingItem, Repository, RepoSnapshot
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from worker.app import tasks
from worker.app.snapshots import SnapshotIncomplete


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshots.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 8, 2, tzinfo=UTC).replace(tzinfo=None)
    with factory() as session:
        session.add_all([
            Repository(
                github_id=index, full_name=f"owner/repo{index}", owner="owner",
                name=f"repo{index}", html_url=f"https://github.com/owner/repo{index}",
                first_tracked_at=now, last_seen_at=now, history_available_from=now,
                stars_count=150,
            )
            for index in (1, 2)
        ])
        session.commit()
    monkeypatch.setattr(tasks, "get_sync_session", factory)
    yield factory
    engine.dispose()


@pytest.mark.parametrize("failure", [
    httpx.RemoteProtocolError("connection interrupted"),
    GitHubRateLimitError("rate limited"),
])
def test_capture_preserves_progress_and_resumes(sessions, monkeypatch, failure):
    client = Mock()
    data = Mock(stars_count=175, forks_count=10)
    def fetch(name):
        if name == "owner/repo2":
            raise failure
        return data
    client.repository.side_effect = fetch
    monkeypatch.setattr("worker.app.snapshots.get_settings", lambda: Settings(snapshot_concurrency=1))
    monkeypatch.setattr(tasks, "GitHubClient", lambda: client)
    monkeypatch.setattr(tasks, "_upsert_repository", lambda session, data, **kwargs: None)
    now = datetime(2026, 9, 8, 2, tzinfo=UTC).replace(tzinfo=None)

    expected = GitHubRateLimitError if isinstance(failure, GitHubRateLimitError) else SnapshotIncomplete
    with pytest.raises(expected):
        tasks._capture_snapshots(now)
    with sessions() as session:
        assert len(session.scalars(select(RepoSnapshot)).all()) == 1

    client.repository.reset_mock()
    client.repository.side_effect = [data]
    assert tasks._capture_snapshots(now) == 1
    client.repository.assert_called_once_with("owner/repo2")
    with sessions() as session:
        assert len(session.scalars(select(RepoSnapshot)).all()) == 2


def test_live_ranking_excludes_demo_baselines(sessions):
    now = datetime(2026, 9, 8, 2, tzinfo=UTC).replace(tzinfo=None)
    with sessions() as session:
        for days, stars, source in [(7, 10, "demo"), (1, 100, "github_api"),
                                    (0, 150, "github_api")]:
            captured = now - timedelta(days=days)
            session.add(RepoSnapshot(
                repository_id=1, snapshot_date=captured.date(), captured_at=captured,
                stars_count=stars, forks_count=0, source=source,
            ))
        session.commit()
        tasks._persist_ranking(session, 7, now)
        session.commit()
        weekly = session.scalar(select(RankingItem).where(RankingItem.repository_id == 1))
        assert weekly.baseline_available is False
        assert weekly.net_delta == 0
        tasks._persist_ranking(session, 1, now)
        session.commit()
        daily = session.scalars(select(RankingItem).where(
            RankingItem.repository_id == 1
        ).order_by(RankingItem.id.desc())).first()
        assert daily.baseline_available is True
        assert daily.net_delta == 50
