"""Internal repository recovery command: python -m worker.app.repository_admin owner/name."""

import argparse
from datetime import UTC, datetime

from app.models import Repository
from sqlalchemy import func, select

from worker.app import tasks
from worker.app.availability import restore


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a quarantined repository for next collection")
    parser.add_argument("repository", help="owner/name")
    args = parser.parse_args()
    with tasks._task_lock("repository-collection", timeout=300, required=True) as acquired:
        if not acquired:
            parser.exit(1, "Collection is active; retry after it finishes.\n")
        with tasks.get_sync_session() as session:
            repository = session.scalar(select(Repository).where(
                func.lower(Repository.full_name) == args.repository.lower()))
            if repository is None:
                parser.exit(1, "Repository not found.\n")
            # An administrative override is not proof of a successful GitHub request.
            previous_success = repository.last_success_at
            previous_check = repository.last_checked_at
            previous_error = repository.last_error_code
            restore(repository, datetime.now(UTC))
            repository.last_success_at = previous_success
            repository.last_checked_at = previous_check
            repository.last_error_code = previous_error
            session.commit()
            print(f"Restored {repository.full_name}; manual exclusions still apply.")


if __name__ == "__main__":
    main()
