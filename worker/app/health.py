"""Container health checks and the Beat scheduler heartbeat.

The Beat heartbeat is written by the scheduler's own ``tick`` loop.  There is
no helper thread that could report a healthy Beat process after its scheduler
has stopped making progress.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from celery.beat import PersistentScheduler
from redis.exceptions import RedisError

from worker.app.celery_app import celery_app

logger = logging.getLogger(__name__)

BEAT_HEARTBEAT_INTERVAL_SECONDS = 30.0
BEAT_HEARTBEAT_TTL_SECONDS = 90
BEAT_HEARTBEAT_FILE = "/tmp/repopulse-beat-heartbeat"
WORKER_INSPECT_TIMEOUT_SECONDS = 2.0


class HealthCheckError(RuntimeError):
    """Raised when a Celery or Redis health query cannot prove health."""


def worker_node_name(hostname: str | None = None) -> str:
    return f"worker@{hostname or socket.gethostname()}"


def beat_heartbeat_path() -> Path:
    return Path(os.environ.get("REPOPULSE_BEAT_HEARTBEAT_FILE", BEAT_HEARTBEAT_FILE))


class HeartbeatScheduler(PersistentScheduler):
    """Scheduler that records liveness from the Beat main loop itself."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._last_heartbeat_at = 0.0
        super().__init__(*args, **kwargs)
        # Keep the scheduler loop short enough that a stalled Beat expires
        # promptly instead of waiting for a far-future task.
        self.max_interval = min(float(self.max_interval or 5.0), 5.0)

    def tick(self, *args: Any, **kwargs: Any) -> float:
        self._refresh_heartbeat()
        return super().tick(*args, **kwargs)

    def _refresh_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_at < BEAT_HEARTBEAT_INTERVAL_SECONDS:
            return
        heartbeat_path = beat_heartbeat_path()
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="ascii",
                dir=heartbeat_path.parent,
                prefix=f".{heartbeat_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(str(time.time()))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, heartbeat_path)
        except OSError:
            logger.warning("Beat heartbeat update failed", exc_info=True)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return
        self._last_heartbeat_at = now


def beat_heartbeat_is_fresh(hostname: str | None = None) -> bool:
    try:
        age = time.time() - beat_heartbeat_path().stat().st_mtime
    except OSError:
        return False
    return 0 <= age <= BEAT_HEARTBEAT_TTL_SECONDS


def worker_ping_is_healthy(
    hostname: str | None = None,
    timeout: float = WORKER_INSPECT_TIMEOUT_SECONDS,
) -> bool:
    node = worker_node_name(hostname)
    replies = celery_app.control.inspect(destination=[node], timeout=timeout).ping()
    return bool(replies and _is_pong(replies.get(node)))


def worker_activity(
    hostname: str | None = None,
    timeout: float = WORKER_INSPECT_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """Return active, reserved, and scheduled counts for exactly one Worker."""

    node = worker_node_name(hostname)
    inspector = celery_app.control.inspect(destination=[node], timeout=timeout)
    snapshots = {
        "active": inspector.active(),
        "reserved": inspector.reserved(),
        "scheduled": inspector.scheduled(),
    }
    if any(snapshot is None or node not in snapshot for snapshot in snapshots.values()):
        raise HealthCheckError(f"worker {node} did not answer inspect")
    return {
        name: _count_entries(snapshot[node])
        for name, snapshot in snapshots.items()
    }


def cancel_worker_consumer(
    hostname: str | None = None,
    queue: str = "celery",
    timeout: float = WORKER_INSPECT_TIMEOUT_SECONDS,
) -> None:
    node = worker_node_name(hostname)
    replies = celery_app.control.cancel_consumer(
        queue,
        destination=[node],
        reply=True,
        timeout=timeout,
    )
    if not replies or not any(node in reply for reply in replies if isinstance(reply, Mapping)):
        raise HealthCheckError(f"worker {node} did not acknowledge consumer cancellation")


def _is_pong(reply: Any) -> bool:
    return isinstance(reply, Mapping) and reply.get("ok") == "pong"


def _count_entries(entries: Any) -> int:
    return len(entries) if isinstance(entries, list) else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RepoPulse container health checks")
    parser.add_argument(
        "role",
        choices=("worker", "beat", "activity", "cancel-consumer"),
    )
    parser.add_argument("--hostname", default=None)
    parser.add_argument("--timeout", type=float, default=WORKER_INSPECT_TIMEOUT_SECONDS)
    parser.add_argument("--queue", default="celery")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.role == "worker":
            return 0 if worker_ping_is_healthy(args.hostname, args.timeout) else 1
        if args.role == "beat":
            return 0 if beat_heartbeat_is_fresh(args.hostname) else 1
        if args.role == "activity":
            print(json.dumps(worker_activity(args.hostname, args.timeout), sort_keys=True))
            return 0
        cancel_worker_consumer(args.hostname, args.queue, args.timeout)
        return 0
    except (HealthCheckError, OSError, RedisError) as exc:
        logger.error("Health check failed: %s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
