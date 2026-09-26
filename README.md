# RepoPulse

用可解释的数据，发现正在增长的开源项目。RepoPulse 是一个中文 GitHub 开源项目增长榜：每日按快照计算候选仓库最近 1、7、14、30 天的 Star 净增长，并提供筛选、详情与趋势图。

**在线站点：<https://repopulse.slak7.cn>**

## 功能

- 每日 1 / 7 / 14 / 30 天 Star 净增长榜
- 榜单筛选、仓库详情与趋势图
- 自带 API Key 的 AI「总结翻译」与全文「中文译文」
- 冷缺 README 后台预热，正文持久化于 PostgreSQL

## 技术栈

Next.js / React / TypeScript · FastAPI / Pydantic / SQLAlchemy / Alembic · Celery / Redis / PostgreSQL · Pytest / Playwright / Ruff / Mypy

## 快速开始

```powershell
# Docker Compose 开发（含演示数据）
Copy-Item .env.example .env
docker compose up -d --build
```

纯本地运行（不加载 `.env.example` 时默认 SQLite + 演示数据）：

```powershell
cd backend; python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"; uvicorn app.main:app --reload --port 8000

cd frontend; npm install; npm run dev
```

站点 <http://localhost:3000>，API 文档 <http://localhost:8000/docs>。

## 排名口径

- `net_delta = end_stars - start_stars`，`growth_rate = net_delta / start_stars`。
- 目标日与快照按 UTC 计算，基线取自 `as_of - period` 的 UTC 日历日。
- 榜单只代表 RepoPulse 当前跟踪的候选仓库，不表示全 GitHub 排名。
- 已发布的历史榜单不回溯重算。

## 文档

- [生产部署与运维](docs/operations.md)
- [GitHub 限流说明](docs/rate-limits.md)
- [Windows 运维演练](docs/operations-windows-drill.md)

## 验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m ruff check app ..\worker tests
.\.venv\Scripts\python.exe -m mypy app ..\worker
.\.venv\Scripts\python.exe -m pytest

cd ..\frontend
npm run lint; npm run typecheck; npm run build
npm test -- --project=chromium
```

## 提交安全

`.env` 与 `.env.production` 可能含 GitHub Token、数据库连接等密钥，已被 `.gitignore` 忽略，禁止提交；模板见 `.env.example` 与 `.env.production.example`。
