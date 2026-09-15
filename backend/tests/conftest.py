from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.models import Base, JobRun
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_collection import repository_data

from worker.app import tasks


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(hour=2, minute=0, second=0, microsecond=0)
    with factory() as session:
        for index in range(1, 4):
            tasks._upsert_repository(session, repository_data(index))
        session.add(JobRun(job_key=f"snapshot:all:{now.date()}",
                           task_name="capture_daily_snapshots", status="running", started_at=now))
        session.commit()
    settings = Settings(repository_auto_quarantine=True, snapshot_requests_per_second=10)
    monkeypatch.setattr(tasks, "get_sync_session", factory)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr("worker.app.snapshots.get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "_task_lock", lambda *a, **kw: nullcontext(True))
    yield factory, now
    engine.dispose()
