# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2,<4.0", "pydantic-settings>=2.5,<3.0", "redis>=5.0,<6.0", "sqlalchemy[asyncio]>=2.0,<2.1"]
# ///
"""一次性 UUID 库上的榜单查询离线性能与 EXPLAIN 驱动；执行、审计与门禁评审由 Sol 负责。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from app.models import RankingItem, Repository
    from app.schemas import RankingResponse
    from sqlalchemy.ext.asyncio import AsyncSession

SAFE_HOSTS = frozenset({"127.0.0.1", "localhost"})
RUN_PLAN = ((1, 0, 5000), (7, 0, 5000), (14, 0, 5000), (30, 14, 5000), (30, 0, 5000))
TOPICS = ("ai", "AI", "llm", "developer-tools", "database", "数据库", "c%wild", "under_score")
LANGUAGES = ("Python", "python", "PYTHON", "Go", "Rust", "TypeScript", "C++", "İstanbul-Dil", "日本語", None)
CASES = (("no_filter", 30, None, None, 0, None, 1), ("language", 30, "python", None, 0, None, 1),
         ("language_unicode", 30, "İstanbul-Dil", None, 0, None, 1), ("topic", 30, None, "AI", 0, None, 1),
         ("search_percent", 30, None, None, 0, "50%", 1), ("search_underscore", 30, None, None, 0, "under_score", 1),
         ("search_backslash", 30, None, None, 0, "back\\slash", 1), ("search_unicode", 30, None, None, 0, "İstanbul", 1),
         ("min_stars", 30, None, None, 30_000, None, 1), ("combined", 30, "Python", "ai", 10_000, "needle", 1),
         ("page_2", 30, None, None, 0, None, 2), ("out_of_range", 30, None, None, 0, None, 1000),
         ("period_7", 7, None, None, 0, None, 1))


class Case(NamedTuple):
    name: str; period: int; language: str | None; topic: str | None
    min_stars: int; query: str | None; page: int; limit: int


class _Target(NamedTuple):
    case: str; statement: str; parameters: object


class _Materialized:
    def __init__(self, item_cls: type, repo_cls: type) -> None:
        self._types = (item_cls, repo_cls)
        self.total = 0

    def attach(self, session: Session) -> None:
        def _record(_session: Session, instance: object) -> None:
            if isinstance(instance, self._types):
                self.total += 1

        event.listen(session, "loaded_as_persistent", _record)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RepoPulse 榜单查询性能与 EXPLAIN 驱动")
    parser.add_argument("--admin-url", default=os.environ.get("TEST_POSTGRES_ADMIN_URL"))
    parser.add_argument("--repositories", type=int, default=5000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--page-limit", type=int, default=15)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    if not args.admin_url: parser.error("必须提供 --admin-url 或 TEST_POSTGRES_ADMIN_URL")
    if args.repositories < 100 or args.repetitions < 1 or args.page_limit < 1: parser.error("要求 --repositories>=100、--repetitions>=1、--page-limit>=1")
    return args


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    # Windows 下 psycopg 异步必须用 SelectorEventLoop，Proactor 不受支持。
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None) as runner:
        return runner.run(coro)


def _seed(session: Session, count: int, base: datetime, fingerprint: str) -> None:
    from app.models import RankingItem, RankingRun, Repository

    repos: list[Repository] = []
    for index in range(count):
        owner = f"owner{index % 250}"; name = f"search-needle-{index}" if index % 9 == 0 else f"project-{index}"; stars = (index * 7919) % 60_000
        desc = "；".join([f"第 {index} 个候选仓库", "覆盖筛选/搜索/分页"] + [label for modulus, label in (
            (7, "限时 50% off café"), (11, "under_score 标记"), (13, "back\\slash 字面量"), (5, "İstanbul 大小写"))
            if index % modulus == 0])
        repos.append(Repository(
            github_id=900_000 + index, full_name=f"{owner}/{name}", owner=owner, name=name, description=desc,
            html_url=f"https://github.com/{owner}/{name}", language=LANGUAGES[index % len(LANGUAGES)],
            topics=[TOPICS[(index * 3 + slot) % len(TOPICS)] for slot in range(index % 4)], stars_count=stars,
            pushed_at=base - timedelta(hours=index % 400), first_tracked_at=base - timedelta(days=365),
            last_seen_at=base, history_available_from=base - timedelta(days=365)))
    session.add_all(repos); session.flush()
    for period, days_before, item_count in RUN_PLAN:
        as_of = base - timedelta(days=days_before)
        run = RankingRun(period_days=period, as_of=as_of, baseline_at=as_of - timedelta(days=period),
                         config_version="bench-v1", status="ready", published_at=as_of, collection_summary={
                             "expected": count, "succeeded": count, "missing": 0, "fingerprint": fingerprint,
                             "completeness_percent": 100.0, "is_partial": False})
        session.add(run); session.flush()
        for index in range(min(item_count, count)):
            published = max(0, (index * 7919) % 60_000 + ((index % 11) - 5) * 137); start = max(0, published - (index % 400))
            session.add(RankingItem(ranking_run_id=run.id, repository_id=repos[index].id, rank=index + 1, previous_rank=max(1, index),
                                    start_stars=start, end_stars=published, net_delta=published - start, baseline_available=True,
                                    growth_rate=((published - start) / start) if start else None))


def _legacy_matches(item: RankingItem, repo: Repository, case: Case) -> bool:
    # 还原旧版 _matches：语言/主题精确、end_stars 下限、拼接字段的 Python .lower() 字面子串。
    if case.language and (repo.language or "").lower() != case.language.lower():
        return False
    if case.topic and case.topic.lower() not in [topic.lower() for topic in repo.topics]:
        return False
    if item.end_stars < case.min_stars:
        return False
    return not case.query or case.query.lower() in f"{repo.full_name} {repo.description or ''}".lower()


async def _legacy_response(session: AsyncSession, case: Case) -> tuple[RankingResponse, int]:
    from app.models import RankingItem, RankingRun, Repository
    from app.schemas import CollectionSummary, PeriodDays, RankingMeta, RankingResponse
    from app.services.catalog import CatalogService

    run = await session.scalar(select(RankingRun).where(RankingRun.period_days == case.period,
                               RankingRun.status == "ready").order_by(RankingRun.as_of.desc()).limit(1).with_for_update(read=True))
    if run is None:
        raise SystemExit(f"周期 {case.period} 天没有可用榜单")
    rows = (await session.execute(select(RankingItem, Repository).join(
        Repository, RankingItem.repository_id == Repository.id).where(
        RankingItem.ranking_run_id == run.id).order_by(RankingItem.rank))).all()
    matched = [pair for pair in rows if _legacy_matches(*pair, case)]
    offset = (case.page - 1) * case.limit
    filtered_data = [CatalogService._to_ranking_item(*pair) for pair in matched]
    data = filtered_data[offset:offset + case.limit]
    coverage = int(await session.scalar(select(func.count(Repository.id)).where(
        Repository.is_fork.is_(False), Repository.archived.is_(False), Repository.disabled.is_(False))) or 0)
    meta = RankingMeta(period_days=PeriodDays(case.period), as_of=run.as_of, baseline_at=run.baseline_at,
                       generated_at=run.published_at or run.as_of, coverage=coverage, total=len(matched),
                       collection=CollectionSummary.model_validate(run.collection_summary) if run.collection_summary else None,
                       page=case.page, limit=case.limit, data_mode="demo" if run.config_version == "demo-v1" else "live")
    return RankingResponse(data=data, meta=meta), len(rows)


def _ranking_sql(name: str, captured: list[dict[str, object]]) -> _Target | None:
    for entry in captured:
        if isinstance(statement := entry["statement"], str) and "filtered_ranking" in statement and "ranking_items" in statement:
            return _Target(name, statement, entry["parameters"])
    return None


async def _benchmark(args: argparse.Namespace, fingerprint: str) -> tuple[dict[str, Any], list[_Target]]:
    from app.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    from app import cache as cache_module
    from app.database import async_engine, async_session_factory, sync_engine
    from app.models import Base, RankingItem, Repository
    from app.schemas import PeriodDays
    from app.services import catalog as catalog_module

    class _NoCache(cache_module.ResponseCache):
        async def get(self, key: str) -> dict[str, Any] | None:
            return None

        async def set(self, key: str, value: dict[str, Any], ttl_seconds: int = 300) -> None:
            return None

    cache = _NoCache(); original_cache = cache_module.response_cache
    catalog_module.response_cache = cache_module.response_cache = cache
    captured: list[dict[str, object]] = []

    def _on_execute(_conn: object, _cursor: object, statement: str, parameters: object,
                    _context: object, _executemany: bool) -> None:
        captured.append({"statement": statement, "parameters": parameters})

    try:
        with sync_engine.begin() as connection:
            Base.metadata.create_all(connection)
        with sessionmaker(sync_engine, expire_on_commit=False)() as session:
            _seed(session, args.repositories, datetime(2026, 9, 30, tzinfo=UTC), fingerprint)
            session.commit()
        with sync_engine.begin() as connection:
            for table in ("repositories", "ranking_items", "ranking_runs"):
                connection.execute(text(f"ANALYZE {table}"))
        event.listen(async_engine.sync_engine, "after_cursor_execute", _on_execute)
        cases: list[dict[str, Any]] = []; targets: list[_Target] = []
        for row in CASES:
            case = Case(*row, args.page_limit)
            new_dump = old_dump = target = None
            new_latency: list[float] = []; old_latency: list[float] = []; new_mat = old_mat = run_rows = 0
            for repetition in range(args.repetitions):
                captured.clear()
                async with async_session_factory() as session:
                    counter = _Materialized(RankingItem, Repository); counter.attach(session.sync_session)
                    started = perf_counter()
                    response = await catalog_module.CatalogService(session).rankings(
                        period=PeriodDays(case.period), language=case.language, topic=case.topic,
                        min_stars=case.min_stars, query=case.query, page=case.page, limit=case.limit)
                    new_latency.append(perf_counter() - started)
                    if repetition == 0:
                        new_mat = counter.total; new_dump = response.model_dump(mode="json")
                        target = _ranking_sql(case.name, captured)
            for _ in range(args.repetitions):
                async with async_session_factory() as session:
                    counter = _Materialized(RankingItem, Repository); counter.attach(session.sync_session)
                    started = perf_counter()
                    legacy, run_rows = await _legacy_response(session, case)
                    old_latency.append(perf_counter() - started)
                    if old_dump is None:
                        old_mat = counter.total; old_dump = legacy.model_dump(mode="json")
            assert new_dump is not None and old_dump is not None
            size = len(new_dump["data"])
            checks = {"records": new_dump["data"] == old_dump["data"], "dumps": new_dump == old_dump,
                      "total": new_dump["meta"]["total"] == old_dump["meta"]["total"], "new_mat": new_mat == 2 * size,
                      "coverage": new_dump["meta"]["coverage"] == old_dump["meta"]["coverage"], "explain": target is not None,
                      "old_mat": old_mat == 2 * run_rows}
            cases.append({"name": case.name, "service_total": new_dump["meta"]["total"], "size": size,
                          "legacy_total": old_dump["meta"]["total"], "run_rows": run_rows, "checks": checks,
                          "new_mat": new_mat, "old_mat": old_mat, "ok": all(checks.values()), "explain": [],
                          "new_ms": round(statistics.median(new_latency) * 1000, 3), "old_ms": round(statistics.median(old_latency) * 1000, 3),
                          "new_sample": new_dump["data"][:3], "old_sample": old_dump["data"][:3]})
            if target is not None:
                targets.append(target)
        evidence = {"fingerprint": fingerprint, "generated_at": datetime.now(UTC).isoformat(),
                    "repetitions": args.repetitions, "cases": cases, "all_ok": all(c["ok"] for c in cases),
                    "dataset": {"repositories": args.repositories, "runs": len(RUN_PLAN),
                                "items": sum(min(c, args.repositories) for _, _, c in RUN_PLAN)}}
        return evidence, targets
    finally:
        if event.contains(async_engine.sync_engine, "after_cursor_execute", _on_execute):
            event.remove(async_engine.sync_engine, "after_cursor_execute", _on_execute)
        await cache.close(); await original_cache.close(); await async_engine.dispose(); sync_engine.dispose()


def _attach_explain(evidence: dict[str, Any], targets: list[_Target], database_url: str) -> None:
    plans: dict[str, dict[str, Any]] = {}
    if targets:
        dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
        with psycopg.connect(dsn, autocommit=True) as connection:
            for target in targets:
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + target.statement, target.parameters)
                    row = cursor.fetchone()
                plans[target.case] = {"statement": target.statement, "plan": row[0] if row else None, "parameters": target.parameters}
    for case in evidence["cases"]: entry = plans.get(case["name"]); case["explain"] = [entry] if entry else []


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    fingerprint = uuid4().hex
    admin_url = make_url(args.admin_url)
    if (not admin_url.drivername.startswith("postgresql") or admin_url.host not in SAFE_HOSTS or not admin_url.username
            or "test" not in admin_url.username.lower() or admin_url.database != "postgres"):
        raise SystemExit("admin-url 必须为回环、测试用户（含 test）、管理库 postgres")
    name = f"repopulse_bench_{fingerprint[:12]}"[:63]
    sync_url = admin_url.set(drivername="postgresql+psycopg", database=name)
    async_dsn = admin_url.set(drivername="postgresql+psycopg_async" if admin_url.drivername == "postgresql+psycopg_async" else "postgresql+psycopg", database=name).render_as_string(hide_password=False)
    admin_dsn = admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with tempfile.TemporaryDirectory(prefix=f"repopulse-bench-{fingerprint[:8]}-") as directory:
        os.environ.update({"DATABASE_URL": async_dsn, "SYNC_DATABASE_URL": sync_url.render_as_string(hide_password=False),
                           "REDIS_URL": "redis://127.0.0.1:1/15", "ENVIRONMENT": "test", "SEED_DEMO_DATA": "false",
                           "GITHUB_TOKEN": "", "SENTRY_DSN": "", "AVATAR_CACHE_DIR": str(Path(directory) / "avatars")})
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            evidence, targets = _run(_benchmark(args, fingerprint))
            _attach_explain(evidence, targets, async_dsn)
        finally:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
                             "AND pid <> pg_backend_pid()", (name,))
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    print(f"all_ok={evidence['all_ok']} cases={len(evidence['cases'])} repos={evidence['dataset']['repositories']}")
    for case in evidence["cases"]:
        failed = ",".join(key for key, ok in case["checks"].items() if not ok) or "-"
        print(f"  {case['name']:<18} total={case['service_total']}/{case['legacy_total']} new={case['new_ms']}ms "
              f"old={case['old_ms']}ms mat={case['new_mat']}/{case['old_mat']} {'ok' if case['ok'] else 'FAIL:' + failed}")
    if args.output:
        path = Path(args.output)
        if not path.parent.exists(): raise SystemExit(f"--output 父目录不存在：{path.parent}")
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return 0 if evidence["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
