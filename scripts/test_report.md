# Sol high 测试准备交接（2026-09-16）

本轮只修改测试、Playwright、CI 与 `scripts/test_*`，未改生产代码、配置/Compose/文档/package 文件；未提交。`backend/backend-start.log`、真实 DB、环境文件和卷均未触碰。`ai-pair-programmer` 技能及完整编码规范已读取；仓库无 `scripts/skill-stats`。

## 已完成

- `backend/tests/conftest.py`：在任何 app/worker 导入前设置进程唯一临时 SQLite，禁用 `Settings` 的 `.env` 读取；默认测试将缓存替为逐测试内存缓存，阻断真实 HTTP/GitHub、Redis 命令和队列发送；保留 `httpx.MockTransport`、FastAPI `TestClient` 及 Windows asyncio 自身 loopback socket。显式 API 种子 fixture 每例重建数据。
- `backend/tests/test_api.py`、`test_config.py`：API 只使用显式 fixture 数据；配置测试逐例移除套件环境变量，保持 `Settings(_env_file=None)` 默认值语义。
- `backend/tests/test_ranking_calculator.py`：补 UTC 日期归一、截止前最后快照、负增长、零基线及缺少基线不合成增长。
- `frontend/tests/ranking.spec.ts`：补正/负/零/无基线展示、当前页有效基线摘要、负增长页及全无基线页行为。
- `backend/tests/test_infrastructure_integration.py`：默认跳过，显式 `RUN_INTEGRATION_TESTS=1` 且仅限 loopback 专用测试地址；逐例创建唯一 PostgreSQL 数据库并销毁，验证空库与上一修订迁移、保留数据、事务回滚/行锁；Redis 仅使用非零 DB 的唯一键，验证原子递增/TTL、worker lock 排他与重获，结束只删除测试键。真实 limiter API 尚未定稿，后续补相应 hook 断言。
- `frontend/playwright.config.ts`：`npm start` 生产模式、不复用已有服务器、2/4 workers、明确 timeout、desktop/mobile Chromium 项目。
- `.github/workflows/ci.yml`、`scripts/test_seed_e2e_cache.py`：前端 CI 先 build 后启动生产服务器；独立 SQLite 路径、Redis 7 非默认 DB、显式 demo 数据与固定 README 缓存，避免 GitHub/AI 外呼；独立 PostgreSQL 16 + Redis 7 job 运行可抛弃集成测试。

## 已执行验证

- `backend/.venv/Scripts/python.exe -m pytest -q tests/test_config.py tests/test_ranking_calculator.py tests/test_api.py tests/test_task_queue.py tests/test_github_client.py tests/test_infrastructure_integration.py`（`PYTHONPATH=.;..`）：**37 passed, 4 skipped**（集成按设计默认跳过；2 条第三方 deprecation warning）。
- `backend/.venv/Scripts/python.exe -m ruff check tests/conftest.py tests/test_api.py tests/test_config.py tests/test_ranking_calculator.py tests/test_infrastructure_integration.py ../scripts/test_seed_e2e_cache.py`：通过。
- `python -m compileall -q backend/tests scripts/test_seed_e2e_cache.py`：通过。
- `frontend: npx tsc --noEmit --pretty false` 和 `npx eslint tests/ranking.spec.ts playwright.config.ts`：通过。
- `frontend: npx playwright test --list --project=desktop-chromium`：测试发现成功（增加第二个排名契约测试后又通过类型/ESLint；未运行浏览器）。

## 后续必验

Luna 完成并停止写入后，运行完整后端 pytest/Ruff/mypy、生产 build 与 desktop/mobile 浏览器测试；显式开启 PostgreSQL/Redis 集成测试（只用唯一 `repopulse-test-*` 临时容器/服务，不用现有卷）。根据 Redis limiter、可信代理 token、内部 FastAPI lease coordinator 最终 API 增补测试；当下未假定未知函数名。检查 CI 中 demo API、固定 README 缓存在全部浏览器测试期间不触发外部网络。当前未运行全套或 Docker 集成，尚不能声称 CI/浏览器通过。
