import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import Repository, RepositoryReadme
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def _test_database_url() -> str:
    if os.environ.get("ENVIRONMENT") != "test" or os.environ.get("SEED_DEMO_DATA") != "true":
        raise RuntimeError("README browser fixture requires the explicit demo test environment")
    database_url = os.environ.get("SYNC_DATABASE_URL", "")
    parsed = make_url(database_url)
    database_path = Path(parsed.database or "")
    if (
        parsed.drivername != "sqlite"
        or not database_path.is_absolute()
        or not database_path.name.startswith("repopulse-e2e-")
        or not database_path.is_file()
    ):
        raise RuntimeError("SYNC_DATABASE_URL must target an existing isolated e2e SQLite file")
    return database_url


def main() -> None:
    engine = create_engine(_test_database_url())
    try:
        with Session(engine) as session:
            repository = session.scalar(
                select(Repository).where(Repository.full_name == "fastapi/fastapi")
            )
            if repository is None:
                raise RuntimeError("demo repository fastapi/fastapi was not seeded")

            readme = session.scalar(
                select(RepositoryReadme).where(RepositoryReadme.repository_id == repository.id)
            )
            if readme is None:
                readme = RepositoryReadme(repository_id=repository.id)
                session.add(readme)
            now = datetime.now(UTC)
            readme.path = "README.md"
            readme.content = (
                "# FastAPI\n\n"
                "FastAPI is a modern Python web framework for building APIs.\n\n"
                "```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n"
            )
            readme.html_url = "https://github.com/fastapi/fastapi/blob/master/README.md"
            readme.is_private = False
            readme.visibility = "public"
            readme.last_public_verified_at = now
            readme.last_success_at = now
            readme.last_checked_at = now
            readme.next_refresh_at = now + timedelta(days=7)
            session.commit()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
