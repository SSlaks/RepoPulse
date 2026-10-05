# 参与贡献

感谢你关注 RepoPulse。本指南说明如何搭建环境、验证改动，以及提交 Issue 和 Pull Request 的基本约定。请先阅读 [README](README.md) 了解项目定位。

## 开始之前

RepoPulse 当前正式版本为 **v1.0.0**。`main` 分支可能包含下一版本开发中的变更；体验正式版本请使用发布 Tag，参与贡献请以 `main` 上的最新代码和文档为准。涉及接口或排名口径的变更，请在 Issue 或 Pull Request 中说明兼容性影响。参与贡献前请确认：

- 你运行的是仓库最新代码，而不是线上演示站点的行为。
- 你的改动范围清晰，不顺手重排无关代码。
- 你没有把任何密钥、Token 或私有环境变量写进代码、日志、截图或测试数据。

## 开发环境

完整的本地搭建步骤见 [开发快速上手](docs/getting-started.md)。下面给出两条最短路径。

### 方式一：Docker Compose（含演示数据）

`.env` 可能已经存在，请在缺失时才复制模板，避免覆盖你已有的本地配置：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --build postgres redis api frontend
```

这里只启动 `postgres`、`redis`、`api`、`frontend` 四个核心服务；静态演示不需要 `worker` 和 `beat`，只有开启真实采集时才需要它们。

### 方式二：源码本地开发（两个终端，均在仓库根目录下操作）

需要 Python 3.12+ 与 Node 22。

终端 A（后端，`uvicorn` 会阻塞该终端，请保持运行）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

终端 B（前端，`frontend` 与 `backend` 是仓库根目录下的兄弟目录）：

```powershell
cd frontend
npm ci
npm run dev
```

`npm ci` 会按 `package-lock.json` 做可复现安装；只有在你确实要升级依赖时才用 `npm install`。

站点位于 <http://localhost:3000>，API 文档位于 <http://localhost:8000/docs>。

## 后端验证

以下命令与 CI 的后端检查一致（完整定义见 [.github/workflows/ci.yml](.github/workflows/ci.yml)）。Ruff 分两步：先忽略生产代码的 import 排序（`--ignore I001`），再用 `--select I` 单独检查测试与脚本的 import 排序：

```powershell
cd backend
.\.venv\Scripts\python.exe -m ruff check app ..\worker tests --ignore I001
.\.venv\Scripts\python.exe -m ruff check --select I tests ..\scripts\test_*.py
.\.venv\Scripts\python.exe -m mypy app ..\worker
.\.venv\Scripts\python.exe -m pytest
```

约定：

- 默认 `pytest` 套件会拦截真实网络访问，使用临时 SQLite，不需要 PostgreSQL 或 Redis。
- 标记为 `integration` 的用例默认跳过。要运行它们，需要可用的 disposable PostgreSQL 与 Redis，并设置 `RUN_INTEGRATION_TESTS=1`，参数见 CI 的 `infrastructure-integration` 任务。
- 涉及数据库结构变更时，请同时提交 Alembic 迁移。

## 前端验证

```powershell
cd frontend
npm run lint
npm run typecheck
npm run build
```

### 浏览器端到端测试

`npm test` 运行的是 [Playwright](frontend/playwright.config.ts)，**不是**开箱即用的纯前端测试。`playwright.config.ts` 会以生产模式启动 `npm start`，因此需要先 `npm run build`。当前定义了两个项目：

- `desktop-chromium`（Desktop Chrome）
- `mobile-chromium`（Pixel 7）

运行指定项目：

```powershell
npm test -- --project=desktop-chromium
npm test -- --project=mobile-chromium
```

端到端测试有前置条件，缺少时用例会直接失败或报错：

1. 后端 API 必须在 `API_BASE_URL`（默认 `http://127.0.0.1:8000`）上就绪。
2. 必须设置 `TRUSTED_PROXY_TOKEN`，否则测试辅助工具会抛出 `Browser E2E requires a test TRUSTED_PROXY_TOKEN`。
3. 需要隔离的测试数据库：`ENVIRONMENT=test`、`SEED_DEMO_DATA=true`，且 `SYNC_DATABASE_URL` 指向一个存在的、文件名以 `repopulse-e2e-` 开头的绝对路径 SQLite 文件。
4. 需要 Redis（CI 使用 db 15），并运行 `python scripts/test_seed_e2e_cache.py` 写入 README 固定数据。

CI 的 `frontend` 任务完整演示了上述环境变量与启动顺序，请以 [.github/workflows/ci.yml](.github/workflows/ci.yml) 为准，不要脱离前置条件单独解读 `npm test`。

### 查询与页面可靠性回归

在 `frontend` 目录先运行 `npm run build`，再运行 `npm run test:reliability`。这组测试会启动独立的本地假 API 和生产模式前端，覆盖请求超时与取消、快速切换、筛选选项重试、条件移除与清空、加载区高度、详情返回位置、趋势数据边界、详情页 404/503 及重试恢复，不需要数据库、Redis、GitHub Token 或 AI Key。桌面与移动端均会执行；测试结束后由 Playwright 关闭服务。服务端专用用例仅在此独立配置中运行，CI 会单独执行这组回归。

## 提交改动

- **保持小步、聚焦。** 一个 Pull Request 只解决一件事，便于审查和回滚。大改动请先开 Issue 说明背景。
- **改测试。** 行为变更请补充或更新测试；仅当改动确实无法测试时，在 PR 中说明原因。
- **不要提交密钥。** `.env`、`.env.production` 等文件可能包含 GitHub Token、数据库连接串等敏感信息，已被 `.gitignore` 忽略，禁止提交。模板见 `.env.example` 与 `.env.production.example`。若怀疑密钥泄漏，请按 [SECURITY.md](SECURITY.md) 私下报告。
- **提交前自查。** 至少跑一遍与改动相关的 lint、类型检查和测试。

## 报告问题与提需求

- 缺陷与功能请求请使用仓库的 Issue 表单。
- 安全问题请勿公开提交 Issue，改按 [SECURITY.md](SECURITY.md) 私下报告。
