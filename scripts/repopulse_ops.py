#!/usr/bin/env python3
"""Safe, cross-platform RepoPulse deployment and database operations.

Every Docker invocation is built as an argument list and uses the main
``docker-compose.yml`` with an explicit env file and project name.  The CLI
does not invoke a shell and never restores Redis data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE_NAME = "docker-compose.yml"
DEFAULT_PROJECT_NAME = "repopulse"
DEFAULT_DRAIN_TIMEOUT_SECONDS = 900.0
WORKER_INSPECT_TIMEOUT_SECONDS = 2.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 1800.0
DEFAULT_QUERY_TIMEOUT_SECONDS = 30.0
DEFAULT_BACKUP_TIMEOUT_SECONDS = 7200.0
POLL_INTERVAL_SECONDS = 2.0
IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class OpsError(RuntimeError):
    """An operator-actionable failure that must leave services quiesced."""


@dataclass(frozen=True)
class ComposeRunner:
    root: Path
    env_file: Path
    project_name: str
    docker_binary: str = "docker"

    @property
    def compose_file(self) -> Path:
        return self.root / COMPOSE_FILE_NAME

    def compose_command(self, args: Sequence[str]) -> list[str]:
        return [*self._compose_prefix(), *args]

    def compose(
        self,
        args: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        stdin: BinaryIO | None = None,
        env_overrides: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[bytes]:
        command = self.compose_command(args)
        return self._run(
            command,
            capture_output=capture_output,
            check=check,
            stdin=stdin,
            env_overrides=env_overrides,
            timeout=timeout,
        )

    def docker(
        self,
        args: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        env_overrides: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(
            [self.docker_binary, *args],
            capture_output=capture_output,
            check=check,
            env_overrides=env_overrides,
            timeout=timeout,
        )

    def _compose_prefix(self) -> list[str]:
        return [
            self.docker_binary,
            "compose",
            "--env-file",
            str(self.env_file),
            "--project-name",
            self.project_name,
            "-f",
            str(self.compose_file),
        ]

    def _run(
        self,
        command: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
        stdin: BinaryIO | None = None,
        env_overrides: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        if env_overrides:
            environment.update(env_overrides)
        try:
            result = subprocess.run(
                list(command),
                cwd=self.root,
                env=environment,
                stdin=stdin,
                capture_output=capture_output,
                shell=False,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpsError(
                f"command timed out after {timeout:.0f}s ({' '.join(command)})"
            ) from exc
        if check and result.returncode:
            raise OpsError(_command_failure(command, result))
        return result


@dataclass(frozen=True)
class BackupResult:
    dump_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def _command_failure(
    command: Sequence[str], result: subprocess.CompletedProcess[bytes]
) -> str:
    stderr = _decode(result.stderr).strip()
    stdout = _decode(result.stdout).strip()
    detail = stderr or stdout or f"exit code {result.returncode}"
    return f"command failed ({' '.join(command)}): {detail[-2000:]}"


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).strftime("%Y%m%dT%H%M%SZ")


def _iso_timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise OpsError(
            f"{label} must match {IDENTIFIER_PATTERN.pattern}; refusing unsafe database identifier"
        )
    return value


def _validate_project_name(value: str) -> str:
    if not PROJECT_PATTERN.fullmatch(value):
        raise OpsError("project name contains unsupported characters")
    return value


def _validate_sha(value: str) -> str:
    normalized = value.lower()
    if not SHA_PATTERN.fullmatch(normalized):
        raise OpsError("git SHA must be 7-64 hexadecimal characters")
    return normalized


def _image_tag(image: str) -> str | None:
    without_digest = image.rsplit("@", maxsplit=1)[0]
    last_part = without_digest.rsplit("/", maxsplit=1)[-1]
    if ":" not in last_part:
        return None
    return last_part.rsplit(":", maxsplit=1)[-1]


def _validate_release_images(sha: str, backend_image: str, frontend_image: str) -> None:
    for label, image in (("backend", backend_image), ("frontend", frontend_image)):
        if not image or _image_tag(image) != sha:
            raise OpsError(f"{label} image must use the exact git SHA as its tag: {sha}")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise OpsError(f"env file does not exist: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise OpsError(f"invalid env assignment at {path}:{line_number}")
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise OpsError(f"invalid env name at {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _setting(values: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, values.get(key, default))


def _load_context(args: argparse.Namespace) -> tuple[ComposeRunner, dict[str, str]]:
    root = Path(args.root).expanduser().resolve() if args.root else PROJECT_ROOT
    env_file = Path(args.env_file).expanduser()
    if not env_file.is_absolute():
        env_file = root / env_file
    env_file = env_file.resolve()
    if not (root / COMPOSE_FILE_NAME).is_file():
        raise OpsError(f"main compose file does not exist: {root / COMPOSE_FILE_NAME}")
    project_name = _validate_project_name(args.project_name)
    values = _read_env_file(env_file)
    return ComposeRunner(root, env_file, project_name), values


def _require_database_settings(values: Mapping[str, str]) -> tuple[str, str]:
    user = _setting(values, "POSTGRES_USER")
    database = _setting(values, "POSTGRES_DB")
    if not user or not database:
        raise OpsError("POSTGRES_USER and POSTGRES_DB must be present in the selected env file")
    return _validate_identifier(user, "POSTGRES_USER"), _validate_identifier(database, "POSTGRES_DB")


def _backend_image(values: Mapping[str, str]) -> str:
    return _setting(values, "BACKEND_IMAGE", "repopulse-backend:local") or "repopulse-backend:local"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_path(dump_path: Path) -> Path:
    return Path(f"{dump_path}.json")


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _pg_dump_command(user: str, database: str) -> list[str]:
    return [
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--username",
        user,
        "--dbname",
        database,
    ]


def _validate_archive(runner: ComposeRunner, dump_path: Path) -> None:
    with dump_path.open("rb") as archive:
        result = runner.compose(
            ["exec", "-T", "postgres", "pg_restore", "--list"],
            capture_output=True,
            check=False,
            stdin=archive,
            timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
        )
    if result.returncode:
        raise OpsError(f"pg_restore --list rejected {dump_path}: {_decode(result.stderr)[-2000:]}")


def _psql(
    runner: ComposeRunner,
    values: Mapping[str, str],
    database: str,
    query: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    user, _ = _require_database_settings(values)
    return runner.compose(
        [
            "exec",
            "-T",
            "postgres",
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--username",
            user,
            "--dbname",
            database,
            "--command",
            query,
        ],
        capture_output=True,
        check=check,
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )


def _query_lines(
    runner: ComposeRunner, values: Mapping[str, str], database: str, query: str
) -> list[str]:
    result = _psql(runner, values, database, query)
    return [line.strip() for line in _decode(result.stdout).splitlines() if line.strip()]


def _alembic_versions(
    runner: ComposeRunner, values: Mapping[str, str], database: str
) -> list[str] | None:
    result = _psql(
        runner,
        values,
        database,
        "SELECT version_num FROM alembic_version ORDER BY version_num;",
        check=False,
    )
    if result.returncode:
        return None
    return [line for line in _decode(result.stdout).splitlines() if line.strip()]


def _image_id(runner: ComposeRunner) -> str | None:
    result = runner.compose(
        ["images", "--quiet", "api"],
        capture_output=True,
        check=False,
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode:
        return None
    return next((line.strip() for line in _decode(result.stdout).splitlines() if line.strip()), None)


def create_backup(
    runner: ComposeRunner,
    values: Mapping[str, str],
    *,
    database: str,
    dump_path: Path,
    kind: str,
    image_sha: str | None = None,
) -> BackupResult:
    user = _require_database_settings(values)[0]
    database = _validate_identifier(database, "database")
    dump_path = dump_path.expanduser().resolve()
    metadata_path = _metadata_path(dump_path)
    if dump_path.exists() or metadata_path.exists():
        raise OpsError(f"backup output already exists: {dump_path}")
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dump_path.parent,
            prefix=f".{dump_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            process = subprocess.Popen(
                runner.compose_command(_pg_dump_command(user, database)),
                cwd=runner.root,
                env=os.environ.copy(),
                stdout=output,
                stderr=subprocess.PIPE,
                shell=False,
            )
            try:
                _, stderr = process.communicate(timeout=DEFAULT_BACKUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                process.terminate()
                try:
                    process.wait(timeout=DEFAULT_QUERY_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise OpsError(
                    f"pg_dump timed out after {DEFAULT_BACKUP_TIMEOUT_SECONDS:.0f}s"
                ) from exc
            output.flush()
            os.fsync(output.fileno())
        if process.returncode:
            raise OpsError(f"pg_dump failed: {_decode(stderr)[-2000:]}")
        digest = _sha256_file(temporary_path)
        _validate_archive(runner, temporary_path)
        versions = _alembic_versions(runner, values, database)
        metadata: dict[str, Any] = {
            "status": "success",
            "kind": kind,
            "created_at": _iso_timestamp(),
            "database": database,
            "format": "postgresql-custom",
            "pg_major": 16,
            "sha256": digest,
            "size_bytes": temporary_path.stat().st_size,
            "image": _backend_image(values),
            "image_id": _image_id(runner),
            "git_sha": image_sha or _setting(values, "GIT_SHA"),
            "alembic_versions": versions,
        }
        os.replace(temporary_path, dump_path)
        temporary_path = None
        try:
            _write_json_atomic(metadata_path, metadata)
        except Exception:
            dump_path.unlink(missing_ok=True)
            raise
        return BackupResult(dump_path, metadata_path, metadata)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def prune_daily_backups(directory: Path, keep: int) -> list[Path]:
    if keep < 1:
        raise OpsError("daily retention must keep at least one successful backup")
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        return []
    candidates: list[tuple[str, Path, Path]] = []
    for metadata_path in directory.glob("repopulse-daily-*.dump.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dump_path = Path(str(metadata_path)[: -len(".json")])
        if (
            metadata.get("status") != "success"
            or metadata.get("kind") != "daily"
            or not dump_path.is_file()
            or dump_path.parent != directory
        ):
            continue
        candidates.append((str(metadata.get("created_at", "")), dump_path, metadata_path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    removed: list[Path] = []
    for _, dump_path, metadata_path in candidates[keep:]:
        dump_path.unlink()
        metadata_path.unlink()
        removed.extend((dump_path, metadata_path))
    return removed


def _container_id(runner: ComposeRunner, service: str) -> str | None:
    result = runner.compose(
        ["ps", "-q", service],
        capture_output=True,
        check=False,
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise OpsError(f"could not inspect Compose service {service}: {_decode(result.stderr)}")
    return next((line.strip() for line in _decode(result.stdout).splitlines() if line.strip()), None)


def _container_running(runner: ComposeRunner, container_id: str) -> bool:
    result = runner.docker(
        ["inspect", "--format", "{{.State.Running}}", container_id],
        capture_output=True,
        check=False,
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode:
        detail = _decode(result.stderr).lower()
        if "no such object" in detail or "no such container" in detail:
            return False
        raise OpsError(f"could not inspect container {container_id}: {_decode(result.stderr)}")
    return _decode(result.stdout).strip().lower() == "true"


def _wait_stopped(runner: ComposeRunner, service: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        container_id = _container_id(runner, service)
        if container_id is None or not _container_running(runner, container_id):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpsError(
                f"{service} did not stop within {timeout:.0f}s; left in its current state without SIGKILL"
            )
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _signal_stop(runner: ComposeRunner, service: str, timeout: float) -> None:
    container_id = _container_id(runner, service)
    if container_id is None or not _container_running(runner, container_id):
        return
    # SIGTERM is deliberately sent with `kill`; `docker stop --timeout` would
    # send SIGKILL after the grace period and could terminate a DB-writing task.
    runner.compose(
        ["kill", "--signal", "SIGTERM", service],
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    _wait_stopped(runner, service, timeout)


def _worker_activity(runner: ComposeRunner, timeout: float) -> dict[str, int]:
    result = runner.compose(
        [
            "exec",
            "-T",
            "worker",
            "python",
            "-m",
            "worker.app.health",
            "activity",
            "--timeout",
            str(timeout),
        ],
        capture_output=True,
        check=False,
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise OpsError(f"worker activity inspect failed: {_decode(result.stderr)[-2000:]}")
    try:
        activity = json.loads(_decode(result.stdout))
    except json.JSONDecodeError as exc:
        raise OpsError("worker activity inspect returned invalid JSON") from exc
    if not isinstance(activity, dict) or any(
        not isinstance(activity.get(name), int) for name in ("active", "reserved", "scheduled")
    ):
        raise OpsError("worker activity inspect returned incomplete counts")
    return {name: int(activity[name]) for name in ("active", "reserved", "scheduled")}


def _cancel_worker_consumer(runner: ComposeRunner, timeout: float) -> None:
    result = runner.compose(
        [
            "exec",
            "-T",
            "worker",
            "python",
            "-m",
            "worker.app.health",
            "cancel-consumer",
            "--timeout",
            str(timeout),
        ],
        capture_output=True,
        check=False,
        timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise OpsError(f"worker did not acknowledge consumer cancellation: {_decode(result.stderr)}")


def drain_worker(runner: ComposeRunner, timeout: float) -> None:
    container_id = _container_id(runner, "worker")
    if container_id is None or not _container_running(runner, container_id):
        return
    _cancel_worker_consumer(runner, min(timeout, WORKER_INSPECT_TIMEOUT_SECONDS))
    deadline = time.monotonic() + timeout
    while True:
        activity = _worker_activity(runner, min(timeout, WORKER_INSPECT_TIMEOUT_SECONDS))
        if all(count == 0 for count in activity.values()):
            _signal_stop(runner, "worker", max(0.0, deadline - time.monotonic()))
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpsError(
                "worker drain timed out with "
                f"active={activity['active']} reserved={activity['reserved']} "
                f"scheduled={activity['scheduled']}; worker remains running"
            )
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def quiesce_writers(runner: ComposeRunner, timeout: float, *, stop_api: bool) -> None:
    _signal_stop(runner, "beat", timeout)
    drain_worker(runner, timeout)
    if stop_api:
        _signal_stop(runner, "api", timeout)


def _release_env(sha: str, backend_image: str, frontend_image: str) -> dict[str, str]:
    return {
        "GIT_SHA": sha,
        "BACKEND_IMAGE": backend_image,
        "FRONTEND_IMAGE": frontend_image,
    }


def _assert_images_exist(
    runner: ComposeRunner, sha: str, backend_image: str, frontend_image: str
) -> None:
    _validate_release_images(sha, backend_image, frontend_image)
    for label, image in (("backend", backend_image), ("frontend", frontend_image)):
        result = runner.docker(
            ["image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
        )
        if result.returncode:
            raise OpsError(f"{label} image is unavailable locally: {image}")


def _run_migrations(runner: ComposeRunner, release_env: Mapping[str, str]) -> None:
    runner.compose(
        [
            "up",
            "--force-recreate",
            "--exit-code-from",
            "migrate",
            "migrate",
        ],
        env_overrides=release_env,
        timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )


def _start_release(
    runner: ComposeRunner, release_env: Mapping[str, str], *, no_deps: bool = False
) -> None:
    args = ["up", "-d", "--wait", "--force-recreate"]
    if no_deps:
        args.append("--no-deps")
    args.extend(("api", "worker", "beat", "frontend"))
    runner.compose(args, env_overrides=release_env, timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS)


def _validate_schema_for_rollback(
    runner: ComposeRunner, release_env: Mapping[str, str]
) -> None:
    for operation in ("current", "heads"):
        result = runner.compose(
            [
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "alembic",
                "api",
                "-c",
                "/app/backend/alembic.ini",
                operation,
            ],
            capture_output=True,
            check=False,
            env_overrides=release_env,
            timeout=DEFAULT_QUERY_TIMEOUT_SECONDS,
        )
        if result.returncode or not _decode(result.stdout).strip():
            raise OpsError(
                f"rollback schema compatibility check failed ({operation}): "
                f"{_decode(result.stderr)[-2000:]}"
            )


def _default_backup_path(directory: Path, kind: str, suffix: str = "") -> Path:
    directory = directory.expanduser().resolve()
    return directory / f"repopulse-{kind}-{_timestamp()}" f"{suffix}.dump"


def _backup_command(args: argparse.Namespace) -> int:
    runner, values = _load_context(args)
    _, database = _require_database_settings(values)
    database = args.database or database
    output = Path(args.output) if args.output else _default_backup_path(Path(args.backup_dir), "daily")
    result = create_backup(
        runner,
        values,
        database=database,
        dump_path=output,
        kind="daily" if args.daily else "manual",
    )
    removed = prune_daily_backups(Path(args.backup_dir), args.keep) if args.daily else []
    print(
        json.dumps(
            {
                "dump": str(result.dump_path),
                "metadata": str(result.metadata_path),
                "sha256": result.metadata["sha256"],
                "pruned": [str(path) for path in removed],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _pre_restore_backup_path(directory: Path) -> Path:
    return _default_backup_path(directory, "pre-restore")


def _database_exists(runner: ComposeRunner, values: Mapping[str, str], database: str) -> bool:
    safe_database = database.replace("'", "''")
    rows = _query_lines(
        runner,
        values,
        "postgres",
        f"SELECT 1 FROM pg_database WHERE datname = '{safe_database}';",
    )
    return rows == ["1"]


def _database_sql_identifier(value: str) -> str:
    safe_value = _validate_identifier(value, "database")
    return f'"{safe_value}"'


def _prepare_restore_database(
    runner: ComposeRunner,
    values: Mapping[str, str],
    target: str,
    existing: bool,
) -> None:
    owner, _ = _require_database_settings(values)
    if existing:
        _psql(
            runner,
            values,
            "postgres",
            f"DROP DATABASE {_database_sql_identifier(target)};",
        )
    _psql(
        runner,
        values,
        "postgres",
        f"CREATE DATABASE {_database_sql_identifier(target)} OWNER {_database_sql_identifier(owner)};",
    )


def _restore_archive(
    runner: ComposeRunner, values: Mapping[str, str], target: str, dump_path: Path
) -> None:
    user, _ = _require_database_settings(values)
    with dump_path.open("rb") as archive:
        result = runner.compose(
            [
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--username",
                user,
                "--dbname",
                _validate_identifier(target, "target database"),
            ],
            capture_output=True,
            check=False,
            stdin=archive,
            timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        )
    if result.returncode:
        raise OpsError(f"pg_restore failed: {_decode(result.stderr)[-2000:]}")


def _verify_restore(
    runner: ComposeRunner, values: Mapping[str, str], target: str
) -> tuple[list[str], dict[str, int]]:
    versions = _alembic_versions(runner, values, target)
    if not versions:
        raise OpsError("restored database has no readable Alembic version")
    records: dict[str, int] = {}
    for table in ("repositories", "repo_snapshots", "ranking_runs"):
        rows = _query_lines(
            runner,
            values,
            target,
            f"SELECT COUNT(*) FROM {_database_sql_identifier(table)};",
        )
        if len(rows) != 1 or not rows[0].isdigit():
            raise OpsError(f"could not verify restored records in {table}")
        records[table] = int(rows[0])
    return versions, records


def _next_restore_database(runner: ComposeRunner, values: Mapping[str, str]) -> str:
    base = f"repopulse_restore_{_timestamp().lower()}"
    candidate = base[:63]
    if not _database_exists(runner, values, candidate):
        return candidate
    for suffix in range(1, 100):
        candidate = f"{base[: 62 - len(str(suffix))]}_{suffix}"
        if not _database_exists(runner, values, candidate):
            return candidate
    raise OpsError("could not choose an unused restore database name")


def _restore_command(args: argparse.Namespace) -> int:
    runner, values = _load_context(args)
    source = Path(args.dump).expanduser().resolve()
    if not source.is_file():
        raise OpsError(f"restore dump does not exist: {source}")
    _validate_archive(runner, source)
    _, active_database = _require_database_settings(values)
    target = (
        _validate_identifier(args.target_db, "target database")
        if args.target_db
        else _next_restore_database(runner, values)
    )
    existing = _database_exists(runner, values, target)
    if existing and (not args.target_db or args.confirm_existing != target):
        raise OpsError(
            f"target database {target} exists; pass --target-db {target} "
            f"and --confirm-existing {target} to replace it"
        )
    backup_dir = Path(args.backup_dir)
    quiesce_writers(runner, args.timeout, stop_api=True)
    pre_restore = create_backup(
        runner,
        values,
        database=target if existing else active_database,
        dump_path=_pre_restore_backup_path(backup_dir),
        kind="pre-restore",
    )
    _prepare_restore_database(runner, values, target, existing)
    _restore_archive(runner, values, target, source)
    versions, records = _verify_restore(runner, values, target)
    record = {
        "status": "verified",
        "verified_at": _iso_timestamp(),
        "target_database": target,
        "source_dump": str(source),
        "source_sha256": _sha256_file(source),
        "pre_restore_backup": str(pre_restore.dump_path),
        "pre_restore_backup_sha256": pre_restore.metadata["sha256"],
        "alembic_versions": versions,
        "records": records,
        "redis_restored": False,
    }
    record_path = Path(args.record_dir).expanduser().resolve() / f"repopulse-restore-{_timestamp()}.json"
    _write_json_atomic(record_path, record)
    print(json.dumps({"record": str(record_path), "target_database": target}, ensure_ascii=False))
    return 0


def _deploy_command(args: argparse.Namespace) -> int:
    runner, values = _load_context(args)
    sha = _validate_sha(args.git_sha)
    backend_image = args.backend_image or f"repopulse-backend:{sha}"
    frontend_image = args.frontend_image or f"repopulse-frontend:{sha}"
    _assert_images_exist(runner, sha, backend_image, frontend_image)
    release_env = _release_env(sha, backend_image, frontend_image)
    quiesce_writers(runner, args.timeout, stop_api=False)
    predeploy = create_backup(
        runner,
        values,
        database=_require_database_settings(values)[1],
        dump_path=_default_backup_path(Path(args.backup_dir), "predeploy", f"-{sha}"),
        kind="predeploy",
        image_sha=sha,
    )
    _run_migrations(runner, release_env)
    _start_release(runner, release_env)
    print(json.dumps({"status": "deployed", "git_sha": sha, "backup": str(predeploy.dump_path)}))
    return 0


def _rollback_command(args: argparse.Namespace) -> int:
    if not args.schema_compatible:
        raise OpsError(
            "rollback requires explicit --schema-compatible confirmation after an application/schema review"
        )
    runner, _values = _load_context(args)
    sha = _validate_sha(args.git_sha)
    backend_image = args.backend_image or f"repopulse-backend:{sha}"
    frontend_image = args.frontend_image or f"repopulse-frontend:{sha}"
    _assert_images_exist(runner, sha, backend_image, frontend_image)
    release_env = _release_env(sha, backend_image, frontend_image)
    quiesce_writers(runner, args.timeout, stop_api=True)
    _validate_schema_for_rollback(runner, release_env)
    _start_release(runner, release_env, no_deps=True)
    print(json.dumps({"status": "rolled-back", "git_sha": sha}))
    return 0


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", default=".env.production")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)


def _add_timeout_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=DEFAULT_DRAIN_TIMEOUT_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RepoPulse production operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create and optionally retain a PostgreSQL custom dump")
    _add_context_arguments(backup)
    backup.add_argument("--database")
    backup.add_argument("--output")
    backup.add_argument("--backup-dir", default="backups")
    backup.add_argument("--daily", action="store_true", help="mark this backup for daily retention")
    backup.add_argument("--keep", type=int, default=7, help="successful daily backups to retain")
    backup.set_defaults(handler=_backup_command)

    restore = subparsers.add_parser("restore", help="restore a dump into a verified database")
    _add_context_arguments(restore)
    restore.add_argument("--dump", required=True)
    restore.add_argument("--target-db")
    restore.add_argument("--confirm-existing", default=None)
    restore.add_argument("--backup-dir", default="backups")
    restore.add_argument("--record-dir", default="backups/restore-records")
    _add_timeout_argument(restore)
    restore.set_defaults(handler=_restore_command)

    deploy = subparsers.add_parser("deploy", help="backup, migrate, and deploy an immutable SHA pair")
    _add_context_arguments(deploy)
    deploy.add_argument("git_sha")
    deploy.add_argument("--backend-image")
    deploy.add_argument("--frontend-image")
    deploy.add_argument("--backup-dir", default="backups/predeploy")
    _add_timeout_argument(deploy)
    deploy.set_defaults(handler=_deploy_command)

    rollback = subparsers.add_parser("rollback", help="switch images without running Alembic downgrade")
    _add_context_arguments(rollback)
    rollback.add_argument("git_sha")
    rollback.add_argument("--backend-image")
    rollback.add_argument("--frontend-image")
    rollback.add_argument(
        "--schema-compatible",
        action="store_true",
        help="confirm the old image has been reviewed against the live schema",
    )
    _add_timeout_argument(rollback)
    rollback.set_defaults(handler=_rollback_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OpsError, OSError) as exc:
        print(f"repopulse-ops: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("repopulse-ops: interrupted; services were not automatically resumed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
