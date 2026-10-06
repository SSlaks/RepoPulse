# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "anyio>=4.0,<5.0",
#   "httpx>=0.27,<1.0",
#   "psycopg[binary]>=3.2,<4.0",
#   "sqlalchemy[asyncio]>=2.0,<2.1",
#   "uvicorn[standard]>=0.30,<1.0",
# ]
# ///
r"""RepoPulse 榜单 HTTP 真实链路 QA 驱动（一次性数据库 + 自持 Uvicorn，只读公开 HTTP 契约）。

用法（沿用 backend/.venv，Python 3.12；PEP 723 仅记录依赖）：
    backend/.venv/Scripts/python.exe scripts/verify_ranking_http.py ^
        --postgres-admin-url "postgresql+psycopg://repopulse_test:...@127.0.0.1:56560/postgres" ^
        --redis-url "redis://127.0.0.1:56559/15" [--output report.json]

安全边界：只接受回环地址、名称含 test 的测试用户、名为 postgres 的管理库、Redis db > 0；
自建 UUID 临时库；子进程工作目录为私有临时目录、不读仓库 .env、清空 GITHUB_TOKEN/SENTRY_DSN；
结束后释放全部表锁、只停自持 PID、只终止本库连接并 DROP，不清空共享 Redis、不访问 GitHub、不迁移。
TEST-only 覆盖 RANKING_MAX_INFLIGHT=1、RANKING_LOAD_TIMEOUT_SECONDS=2，生产默认值不变。
Windows 下自持 Uvicorn 必须运行在 SelectorEventLoop，否则 Psycopg 3.3 async 拒绝 Proactor。
驱动只输出自身断言证据；最终执行、审计与门禁由 Sol 负责。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import anyio
import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Json
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker

SAFE_HOSTS = frozenset({"127.0.0.1", "localhost"}); RESERVED_PORTS = frozenset({8000, 3000}); BACKEND = Path(__file__).resolve().parents[1] / "backend"
PERIOD, STARTUP_TIMEOUT, HTTP_TIMEOUT, LOAD_TIMEOUT, BARRIER, SAME_KEY = 7, 45.0, 10.0, 2.0, 1.5, 50
OVERRIDES = {"RANKING_MAX_INFLIGHT": "1", "RANKING_LOAD_TIMEOUT_SECONDS": "2"}
STRIP = (
    "DATABASE_URL", "SYNC_DATABASE_URL", "REDIS_URL", "REDIS_PASSWORD", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "ENVIRONMENT", "SEED_DEMO_DATA", "GITHUB_TOKEN", "SENTRY_DSN", "AVATAR_CACHE_DIR", "PYTHONPATH",
    "RANKING_MAX_INFLIGHT", "RANKING_LOAD_TIMEOUT_SECONDS", "RANKING_MAX_WAITERS_PER_KEY",
)


class HarnessFailure(AssertionError):
    """驱动断言或隔离步骤失败。"""


def _need(condition: bool, message: str) -> None:
    if not condition: raise HarnessFailure(message)


# ------------------------------------------------------------------ CLI、隔离与 API 加载

def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RepoPulse 榜单 HTTP 真实链路 QA 驱动")
    parser.add_argument("--postgres-admin-url", default=os.environ.get("TEST_POSTGRES_ADMIN_URL"))
    parser.add_argument("--redis-url", default=os.environ.get("TEST_REDIS_URL"))
    parser.add_argument("--output", default=None, help="可选：写入完整证据 JSON 的文件路径")
    args = parser.parse_args(argv)
    if not args.postgres_admin_url or not args.redis_url: parser.error("必须提供 --postgres-admin-url 与 --redis-url（或 TEST_POSTGRES_ADMIN_URL / TEST_REDIS_URL）")
    return args


def _safe(url: URL, kind: str) -> None:
    if kind == "pg":
        _need(url.drivername.startswith("postgresql"), "admin-url 必须使用 postgresql 驱动"); _need(url.host in SAFE_HOSTS, "PostgreSQL 必须为回环地址")
        _need(bool(url.username) and "test" in url.username.lower(), "PostgreSQL 用户必须含 test"); _need(url.database == "postgres", "admin-url 必须指向 postgres 管理库")
    else:
        _need(url.drivername in {"redis", "rediss"}, "redis-url 必须使用 redis 驱动"); _need(url.host in SAFE_HOSTS, "Redis 必须为回环地址")
        _need(int((url.database or "/0").lstrip("/")) > 0, "Redis 必须使用非 0 数据库")


def _dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _load_api() -> dict[str, Any]:
    if str(BACKEND) not in sys.path: sys.path.insert(0, str(BACKEND))
    from app import schemas
    from app.models import Repository
    return {"schemas": schemas, "model": Repository}


# ------------------------------------------------------------------ 一次性库、端口与指纹

def _admin(admin_dsn: str, name: str, drop: bool) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        if not drop:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))); return
        connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,)); connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _free_port() -> int:
    for _ in range(20):
        with socket.socket() as probe: probe.bind(("127.0.0.1", 0)); port = int(probe.getsockname()[1])
        if port not in RESERVED_PORTS: return port
    raise SystemExit("无法分配非 8000/3000 的本地端口")


def _salt(dsn: str, salt: str) -> int:
    with psycopg.connect(dsn) as connection:
        runs = [row[0] for row in connection.execute("SELECT id FROM ranking_runs").fetchall()]
        counts = dict(connection.execute("SELECT ranking_run_id, count(*) FROM ranking_items GROUP BY ranking_run_id").fetchall())
        for run_id in runs:
            n = int(counts.get(run_id, 0))
            connection.execute("UPDATE ranking_runs SET collection_summary = %s WHERE id = %s", (Json({"expected": n, "succeeded": n, "missing": 0, "completeness_percent": 100.0, "is_partial": False, "fingerprint": salt}), run_id))
        connection.commit()
    return len(runs)


# ------------------------------------------------------------------ 自持 Uvicorn 生命周期

def _child_code(port: int) -> str:
    return ("import asyncio, sys, uvicorn\n"
            f"config = uvicorn.Config('app.main:app', host='127.0.0.1', port={port}, log_level='info')\n"
            "runner = asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) "
            "if sys.platform == 'win32' else asyncio.Runner()\n"
            "runner.run(uvicorn.Server(config).serve())\n")


def _child_env(async_url: URL, sync_url: URL, redis_url: str, root: Path) -> dict[str, str]:
    return {**{key: value for key, value in os.environ.items() if key not in STRIP}, "DATABASE_URL": async_url.render_as_string(hide_password=False), "SYNC_DATABASE_URL": sync_url.render_as_string(hide_password=False),
            "REDIS_URL": redis_url, "ENVIRONMENT": "test", "SEED_DEMO_DATA": "true", "GITHUB_TOKEN": "", "SENTRY_DSN": "", "AVATAR_CACHE_DIR": str(root / "avatars"), "PYTHONPATH": str(BACKEND), "PYTHONUNBUFFERED": "1", **OVERRIDES}


def _spawn(env: dict[str, str], root: Path, port: int, log_path: Path) -> tuple[Any, Any]:
    stream = log_path.open("w", encoding="utf-8")
    return subprocess.Popen([sys.executable, "-c", _child_code(port)], cwd=str(root), env=env, stdout=stream,
                            stderr=subprocess.STDOUT, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)), stream


def _stop(process: Any, stream: Any) -> None:
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
    stream.close()


def _tail(path: Path, limit: int = 40) -> str:
    try: lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError: return ""
    return "\n".join(lines[-limit:])


# ------------------------------------------------------------------ HTTP 与并发原语

@dataclass
class _Pending:
    event: Any; call: Any = None; error: BaseException | None = None


@dataclass(eq=False)
class _Lock:
    conn: Any; pid: int; released: bool = False


@dataclass
class _Ctx:
    base: str; dsn: str; datname: str; sync_url: str; salt: str; api: dict[str, Any]; client: Any = None; control: Any = None; locks: list[Any] = field(default_factory=list)


async def _get(ctx: _Ctx, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
    return await ctx.client.get(path, params=params)


def _rank(ctx: _Ctx, response: httpx.Response) -> Any:
    _need(response.status_code == 200, f"{response.url.path} 期望 200，实际 {response.status_code}")
    return ctx.api["schemas"].RankingResponse.model_validate(response.json())


def _names(response: Any) -> list[str]:
    return [item.full_name for item in response.data]


async def _status(ctx: _Ctx, path: str, params: dict[str, Any] | None, expected: int) -> httpx.Response:
    response = await _get(ctx, path, params); _need(response.status_code == expected, f"{path} 期望 {expected}，实际 {response.status_code}"); return response


def _summon(ctx: _Ctx, pending: _Pending, params: dict[str, Any], task_group: Any) -> None:
    async def run() -> None:
        try: pending.call = await _get(ctx, "/api/v1/rankings", params)
        except Exception as exc: pending.error = exc  # noqa: BLE001 - 错误写入证据，绝不静默
        finally: pending.event.set()
    task_group.start_soon(run)


async def _join(pending: _Pending, timeout: float = HTTP_TIMEOUT + 5.0) -> httpx.Response:
    with anyio.fail_after(timeout): await pending.event.wait()
    _need(pending.error is None, f"后台请求失败：{type(pending.error).__name__}: {pending.error}"); _need(pending.call is not None, "后台请求没有返回结果"); return pending.call


def _acquire(ctx: _Ctx) -> _Lock:
    connection = psycopg.connect(ctx.dsn); connection.execute("LOCK TABLE repositories IN ACCESS EXCLUSIVE MODE")
    row = connection.execute("SELECT pg_backend_pid()").fetchone(); _need(row is not None, "无法读取表锁连接的 backend pid")
    lock = _Lock(connection, int(row[0])); ctx.locks.append(lock); return lock


def _release(ctx: _Ctx, lock: _Lock | None) -> None:
    if lock is None or lock.released: return
    lock.released = True
    if lock in ctx.locks: ctx.locks.remove(lock)
    try: lock.conn.rollback()
    finally: lock.conn.close()


def _waiters(dsn: str, datname: str, blocker: int) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as connection:
        rows = connection.execute(
            "SELECT pid, pg_blocking_pids(pid), left(query, 160) FROM pg_stat_activity WHERE datname = %s "
            "AND state = 'active' AND wait_event_type = 'Lock' AND query ILIKE %s ORDER BY pid", (datname, "%repositories%")).fetchall()
    return [{"pid": int(pid), "blockers": [int(x) for x in (blockers or [])], "query": query} for pid, blockers, query in rows if blocker in [int(x) for x in (blockers or [])]]


async def _wait_blocked(ctx: _Ctx, blocker: int) -> dict[str, Any]:
    started = perf_counter()
    while perf_counter() < started + BARRIER:
        if blocked := await anyio.to_thread.run_sync(_waiters, ctx.dsn, ctx.datname, blocker): return {"blocked": True, "elapsed_s": round(perf_counter() - started, 3), "blocker_pid": blocker, "waiters": blocked}
        await anyio.sleep(0.02)
    raise HarnessFailure("bounded barrier 未观测到被 repositories 表锁阻塞的榜单 SELECT")


# ------------------------------------------------------------------ 场景（checks 字典的来源）

async def check_catalog(ctx: _Ctx) -> dict[str, Any]:
    control = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "limit": 50}))
    _need(bool(control.data), "演示榜单为空"); ctx.control = control
    again = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "limit": 50})); ranks = [i.rank for i in again.data]
    _need(int(again.meta.period_days) == PERIOD and again.meta.page == 1 and again.meta.limit == 50, "meta period/page/limit 不符")
    _need(len(again.data) == again.meta.total and ranks == list(range(1, len(again.data) + 1)), "total/rank 不连续"); _need(_names(again) == _names(control), "happy 榜单与 control 不一致")
    first = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "page": 1, "limit": 5}))
    second = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "page": 2, "limit": 5})); combined = first.data + second.data
    _need([i.rank for i in combined] == list(range(1, len(combined) + 1)), "跨页 rank 不连续"); _need([i.full_name for i in combined] == _names(control)[:len(combined)], "跨页顺序与 control 不一致")
    _need(first.meta.total == second.meta.total == control.meta.total, "跨页 total 不一致")
    language = control.data[0].language; _need(bool(language), "首个榜单项缺少 language")
    expected = [i.full_name for i in control.data if (i.language or "").lower() == language.lower()]; variants: dict[str, Any] = {}
    for value in sorted({language, language.lower(), language.upper()}):
        response = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "language": value, "limit": 50}))
        _need(all((i.language or "").lower() == language.lower() for i in response.data), f"language={value!r} 有不匹配行"); _need(_names(response) == expected, f"language={value!r} 与 control 不一致")
        variants[value] = {"total": response.meta.total, "rows": len(response.data)}
    topic = next((t for item in control.data for t in item.topics), None); _need(bool(topic), "演示数据没有 topic")
    expected = [i.full_name for i in control.data if topic.lower() in [str(t).lower() for t in i.topics]]
    for value in sorted({topic, topic.upper()}):
        response = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "topic": value, "limit": 50}))
        _need(all(topic.lower() in [str(t).lower() for t in i.topics] for i in response.data), f"topic={value!r} 有不匹配行"); _need(_names(response) == expected, f"topic={value!r} 与 control 不一致")
        variants[value] = {"total": response.meta.total, "rows": len(response.data)}
    item = control.data[0]; literals = {}
    for literal in ("%", "_", "\\"):
        response = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "q": literal, "limit": 50}))
        _need(response.meta.total == 0, f"字面量 {literal!r} 命中 {response.meta.total} 行"); literals[literal] = response.meta.total
    description = item.description or ""; unicode_query = "".join(c for c in description if ord(c) > 127)[:2]; _need(bool(unicode_query), "description 没有非 ASCII 字符")
    unicode_hit = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "q": unicode_query, "limit": 50})); _need(item.full_name in _names(unicode_hit), "Unicode 搜索未命中目标")
    _need(len(item.name) >= 3 and len(description) >= 2, "name/description 太短，无法构造分隔符查询"); separator = f"{item.name[-3:]} {description[:2]}"
    separator_hit = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "q": separator, "limit": 50})); _need(item.full_name in _names(separator_hit), "分隔符搜索未命中目标")
    return {"total": control.meta.total, "page_size": len(control.data), "data_mode": control.meta.data_mode, "period_days": int(control.meta.period_days), "sample": _names(control)[:5], "ranks": ranks, "page_ranks": [i.rank for i in combined], "language": language, "topic": topic, "variants": variants, "literals": literals, "unicode_query": unicode_query, "separator_query": separator}


async def check_repository(ctx: _Ctx) -> dict[str, Any]:
    item, schemas = ctx.control.data[0], ctx.api["schemas"]
    repository = schemas.RepositoryResponse.model_validate((await _status(ctx, f"/api/v1/repos/{item.owner}/{item.name}", None, 200)).json()); _need(repository.full_name == item.full_name and repository.html_url == item.github_url, "仓库详情与榜单项不一致")
    series = schemas.SnapshotSeriesResponse.model_validate((await _status(ctx, f"/api/v1/repos/{item.owner}/{item.name}/snapshots", {"range": "90d"}, 200)).json())
    _need(series.repository == item.full_name and bool(series.data) and [p.captured_at for p in series.data] == sorted(p.captured_at for p in series.data), "快照序列异常")
    options = schemas.FilterResponse.model_validate((await _status(ctx, "/api/v1/filters", None, 200)).json()); _need(bool(options.languages) and bool(options.topics), "筛选选项为空")
    _need(schemas.HealthResponse.model_validate((await _status(ctx, "/api/v1/health", None, 200)).json()).status == "ok", "health 非 ok"); await _status(ctx, "/api/v1/repos/definitely/absent-xyz", None, 404)
    bad = await _status(ctx, "/api/v1/rankings", {"period": PERIOD, "limit": 999}, 422); await _status(ctx, "/api/v1/rankings", {"period": 5}, 422)
    code = ((bad.json() or {}).get("error") or {}).get("code"); _need(code == "VALIDATION_ERROR", "422 响应缺少 VALIDATION_ERROR")
    published, token, model = int(item.total_stars), uuid4().hex[:8], ctx.api["model"]

    def mutate() -> str:
        engine = create_engine(ctx.sync_url)
        try:
            with sessionmaker(engine, expire_on_commit=False)() as session:
                target = session.scalar(select(model).where(model.full_name == item.full_name)); _need(target is not None, "ORM 探针未找到目标仓库")
                target.description = f"{target.description or ''} qa-orm-{token}"; target.stars_count = published + 500_000
                session.commit(); stored = session.scalar(select(model.search_text_lower).where(model.id == target.id))
                return str(stored or "")
        finally: engine.dispose()

    stored = await anyio.to_thread.run_sync(mutate); _need(token in stored, "before_update 事件未刷新 search_text_lower")
    search = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "q": f"qa-orm-{token}", "limit": 50})); _need(item.full_name in _names(search), "ORM 改写的 description 未被搜索命中")
    above = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "minStars": published + 1, "limit": 50})); _need(item.full_name not in _names(above), "实时 Star 变化泄漏进已发布 end_stars 筛选")
    at = _rank(ctx, await _get(ctx, "/api/v1/rankings", {"period": PERIOD, "minStars": published, "limit": 50})); _need(item.full_name in _names(at), "minStars=published 缺少该行")
    _need(_names(at) == [i.full_name for i in ctx.control.data if int(i.total_stars) >= published], "minStars=published 结果集与 control 不一致")
    return {"snapshot_points": len(series.data), "languages": len(options.languages), "topics": len(options.topics), "not_found": 404, "validation": code, "search_text_refreshed": True, "above_published_absent": True, "at_published_present": True, "published_total_stars": published, "live_stars_bumped_to": published + 500_000}


async def check_overload(ctx: _Ctx) -> dict[str, Any]:
    token = uuid4().hex[:10]; same = {"period": PERIOD, "q": f"zz-{token}-a", "limit": 5}; other = {"period": PERIOD, "q": f"zz-{token}-b", "limit": 5}
    lock = _acquire(ctx)
    try:
        started = perf_counter()
        async with anyio.create_task_group() as task_group:
            first = _Pending(anyio.Event()); _summon(ctx, first, same, task_group); blocked = await _wait_blocked(ctx, lock.pid)
            follower = _Pending(anyio.Event()); _summon(ctx, follower, same, task_group)
            different = await _get(ctx, "/api/v1/rankings", other); release_elapsed = round(perf_counter() - started, 3)
            _release(ctx, lock); call_first, call_follower = await _join(first), await _join(follower)
        _need(different.status_code == 503, f"不同 key 期望 503，实际 {different.status_code}"); _need(different.headers.get("retry-after") == "5", "503 缺少 Retry-After: 5"); _need(different.headers.get("cache-control", "").lower() == "no-store", "503 缺少 Cache-Control: no-store")
        _need(call_first.status_code == 200 and call_follower.status_code == 200 and call_first.json() == call_follower.json(), "同 key 首/跟随并非 200 同体"); _need(release_elapsed < LOAD_TIMEOUT, f"{release_elapsed}s 内未在超时前释放表锁")
        return {"blocked": blocked, "release_elapsed_s": release_elapsed, "different_key_status": different.status_code, "different_retry_after": different.headers.get("retry-after"), "same_body": True}
    finally: _release(ctx, lock)


async def check_timeout(ctx: _Ctx) -> dict[str, Any]:
    params = {"period": PERIOD, "q": f"zz-{uuid4().hex[:10]}-t", "limit": 5}; lock = _acquire(ctx)
    try:
        started = perf_counter()
        async with anyio.create_task_group() as task_group:
            pending = _Pending(anyio.Event()); _summon(ctx, pending, params, task_group); blocked = await _wait_blocked(ctx, lock.pid); call = await _join(pending)
        elapsed = round(perf_counter() - started, 3)
    finally: _release(ctx, lock)
    _need(call.status_code == 503, f"阻塞查询期望 503 超时，实际 {call.status_code}"); _need(elapsed >= LOAD_TIMEOUT * 0.9, f"超时在 {elapsed}s 返回，早于 {LOAD_TIMEOUT}s")
    retry = _rank(ctx, await _get(ctx, "/api/v1/rankings", params)); _need(retry.meta.total == 0, "释放后重试应返回空字面量结果")
    return {"blocked": blocked, "timeout_status": call.status_code, "timeout_elapsed_s": elapsed, "retry_status": 200, "retry_total": retry.meta.total}


async def check_samekey(ctx: _Ctx) -> dict[str, Any]:
    params = {"period": PERIOD, "q": f"zz-{uuid4().hex[:10]}-sk", "limit": 5}; calls: list[Any] = [None] * SAME_KEY; errors: list[str] = []

    async def one(index: int) -> None:
        try: calls[index] = await _get(ctx, "/api/v1/rankings", params)
        except Exception as exc: errors.append(f"{index}:{type(exc).__name__}:{exc}")  # noqa: BLE001 - 失败写入证据，绝不静默

    async with anyio.create_task_group() as task_group:
        for index in range(SAME_KEY): task_group.start_soon(one, index)
    _need(not errors, f"同 key 请求出现错误：{errors[:3]}")
    bodies = [call.json() for call in calls if call is not None]; statuses = sorted({call.status_code for call in calls if call is not None})
    _need(len(bodies) == SAME_KEY and statuses == [200], f"同 key 调用异常：{len(bodies)} 次 / {statuses}"); _need(all(body == bodies[0] for body in bodies), "同 key 响应体不一致")
    return {"requested": SAME_KEY, "returned": len(bodies), "statuses": statuses, "all_same_body": True}


CHECKS = (
    ("catalog_ranking_filters_search", check_catalog),
    ("repository_detail_snapshot_health_errors_orm", check_repository),
    ("overload_different_key_503", check_overload),
    ("load_timeout_and_retry", check_timeout),
    ("same_key_50_concurrent", check_samekey),
)


# ------------------------------------------------------------------ 运行、退出码与报告

async def _ready(ctx: _Ctx, process: Any, log_path: Path) -> None:
    deadline, last = perf_counter() + STARTUP_TIMEOUT, "no response"
    while perf_counter() < deadline:
        if process.poll() is not None:
            raise HarnessFailure(f"Uvicorn 提前退出（code {process.returncode}）：\n{_tail(log_path)}")
        try:
            response = await _get(ctx, "/api/v1/ready"); last = f"status={response.status_code} body={response.text!r}"
            if response.status_code == 200 and isinstance(response.json(), dict) and response.json().get("status") == "ok": return
        except httpx.HTTPError as exc: last = f"{type(exc).__name__}: {exc}"
        await anyio.sleep(0.2)
    raise HarnessFailure(f"等待 /api/v1/ready 超时（{last}）：\n{_tail(log_path)}")


async def _harness(ctx: _Ctx, process: Any, log_path: Path) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}; failures: list[str] = []
    timeout = httpx.Timeout(connect=5.0, read=HTTP_TIMEOUT, write=HTTP_TIMEOUT, pool=HTTP_TIMEOUT)
    async with httpx.AsyncClient(base_url=ctx.base, timeout=timeout, follow_redirects=False, trust_env=False) as client:
        ctx.client = client
        try:
            await _ready(ctx, process, log_path); await anyio.to_thread.run_sync(_salt, ctx.dsn, ctx.salt)
            for name, check in CHECKS:
                try: checks[name] = await check(ctx)
                except Exception as exc: failures.append(name); checks[name] = {"failed": f"{type(exc).__name__}: {exc}"}  # noqa: BLE001 - 失败写证据并非零退出
        finally:
            for lock in list(ctx.locks): _release(ctx, lock)
    return checks, failures


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    admin_url, redis_url = make_url(args.postgres_admin_url), make_url(args.redis_url); _safe(admin_url, "pg"); _safe(redis_url, "redis")
    fingerprint = uuid4().hex; database_name = f"repopulse_http_{fingerprint[:12]}"[:63]
    sync_url = admin_url.set(drivername="postgresql+psycopg", database=database_name); async_url = admin_url.set(drivername="postgresql+psycopg_async", database=database_name)
    admin_dsn = _dsn(admin_url); root = Path(tempfile.mkdtemp(prefix=f"repopulse-http-{fingerprint[:8]}-")); log_path = root / "uvicorn.log"; port = _free_port()
    ctx = _Ctx(f"http://127.0.0.1:{port}", _dsn(sync_url), database_name, sync_url.render_as_string(hide_password=False), fingerprint, _load_api()); env = _child_env(async_url, sync_url, args.redis_url, root)
    process = stream = None; checks: dict[str, Any] = {}; failures: list[str] = []; startup_error = teardown_error = None; log_tail = ""
    try:
        _admin(admin_dsn, database_name, drop=False); process, stream = _spawn(env, root, port, log_path); checks, failures = anyio.run(_harness, ctx, process, log_path)
    except Exception as exc: startup_error = f"{type(exc).__name__}: {exc}"  # noqa: BLE001 - 启动失败留证并非零退出
    finally:
        if process is not None and stream is not None: _stop(process, stream)
        log_tail = _tail(log_path)
        try: _admin(admin_dsn, database_name, drop=True)
        except Exception as exc: teardown_error = f"{type(exc).__name__}: {exc}"  # noqa: BLE001 - teardown 失败留证并非零退出
        shutil.rmtree(root, ignore_errors=True)
    failed = [*failures, *(["startup"] if startup_error else []), *(["teardown"] if teardown_error else [])]
    report = {"phase": "verify_ranking_http", "generated_at": datetime.now(UTC).isoformat(), "environment": {"database_name": database_name, "redis_db": int((redis_url.database or "/0").lstrip("/")), "uvicorn_port": port, "uvicorn_pid": getattr(process, "pid", None), "env_file_isolated": True, "github_token_present": False, "ranking_max_inflight": 1, "ranking_load_timeout_seconds": LOAD_TIMEOUT},
              "startup_error": startup_error, "teardown_error": teardown_error, "log_tail": log_tail if startup_error else "", "checks": checks, "failures": failed, "verdict": {"passed": not failed, "failed": failed, "note": "仅为驱动自身断言证据；最终执行、审计与门禁由 Sol 负责。"}}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        if not output_path.parent.exists(): raise SystemExit(f"--output 父目录不存在：{output_path.parent}")
        output_path.write_text(text, encoding="utf-8", newline="\n"); print(json.dumps({"passed": not failed, "failed": failed, "checks": list(checks)}, ensure_ascii=False), flush=True)
    else: print(text, flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
