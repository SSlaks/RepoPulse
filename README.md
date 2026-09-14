# RepoPulse

RepoPulse 是一个中文 GitHub 开源项目增长榜。它按每日快照计算候选仓库最近 1、7、14、30 天的 Star 净增长，并提供筛选、项目详情和趋势图。

榜单中的仓库标题只显示仓库名，详情页保留组织或用户名称以便识别。常见仓库的 GitHub 英文简介会通过前端本地映射显示为中文；未配置本地翻译的实时仓库会回退到 GitHub 原始简介。

## 技术栈

- Next.js、React、TypeScript
- FastAPI、Pydantic、SQLAlchemy、Alembic
- Celery、Redis、PostgreSQL
- Pytest、Playwright、Ruff、Mypy

## 快速启动

### FastAPI

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

开发环境默认使用 SQLite，并自动初始化带有一年快照的演示数据。生产环境设置 `ENVIRONMENT=production` 后不会写入演示数据。

### Next.js

```powershell
cd frontend
npm install
npm run dev
```

网站地址：<http://localhost:3000>  
API 文档：<http://localhost:8000/docs>

Compose 默认将 `SEED_DEMO_DATA` 设为 `true`，首次本地启动会在 PostgreSQL 中写入演示榜单和一年快照，便于直接体验页面。连接真实 GitHub 采集前，将其设为 `false` 并配置 `GITHUB_TOKEN`。

### PostgreSQL 与 Redis

```powershell
docker compose up -d postgres redis
```

然后根据 [.env.example](./.env.example) 设置数据库、Redis 和 GitHub Token。多个 Token 可以用逗号分隔。采集服务需要 Redis：

```powershell
$env:PYTHONPATH="backend;."
backend\.venv\Scripts\celery.exe -A worker.app.celery_app.celery_app worker --loglevel=INFO
backend\.venv\Scripts\celery.exe -A worker.app.celery_app.celery_app beat --loglevel=INFO
```

候选集可以通过 `CANDIDATE_LANGUAGES`、`CANDIDATE_TOPICS`、
`MANUAL_SEED_REPOSITORIES` 和 `EXCLUDED_REPOSITORIES` 调整。生产环境可设置
`SENTRY_DSN` 开启 API 与 Worker 错误追踪。

Celery Beat 每天 `00:15 UTC`（北京时间 `08:15`）发现一次候选仓库；每天
`02:00 UTC`（北京时间 `10:00`）保存快照并生成 1、7、14、30 天榜单。

### 抓取性能与任务恢复

发现阶段只为尚未跟踪的仓库获取详情；已有仓库在每日快照阶段重新请求 GitHub，
不会复用发现阶段的 Star 数据。两个阶段独立调度。

快照默认四并发、每秒最多启动四次请求。网络线程各自复用客户端，数据库由主线程写入；
每 50 条或 5 秒提交一次（先到者为准）。可通过以下环境变量调整，修改后重建 Worker 容器配置：

| 环境变量 | 默认值 | 用途 |
|---|---:|---|
| `SNAPSHOT_CONCURRENCY` | 4 | 网络并发数，设为 1 回退串行 |
| `SNAPSHOT_REQUESTS_PER_SECOND` | 4 | 单个快照任务启动请求的速率上限 |
| `SNAPSHOT_BATCH_SIZE` | 50 | 每次提交的最大快照数 |
| `SNAPSHOT_FLUSH_SECONDS` | 5 | 有在途请求时的提交及进度更新间隔 |

`job_runs.progress` 保存 `total`、`saved`、`failed`、`failures`、`rate_per_second`、
`eta_seconds`、`updated_at`。发生限流后，任务进入 `waiting`，同时保存 `wait_reason`
和 `resume_at`，按 GitHub 响应头延迟续跑；二级限流后，本轮剩余请求降为串行。
网络临时故障重试三次，间隔 1、2、4 秒。永久错误或重试耗尽会保留失败仓库清单，
任务标记为 `failed`，不触发榜单。人工重投同日期任务时，仅抓取尚未保存的快照。

将对应任务的 `cancel_requested` 设为 `true` 可以取消：调度器停止新请求，
等待已发出的请求返回并保存成功结果，最终标记为 `cancelled`。已取消任务和延迟重试
不会自动恢复；取消响应受当前网络请求超时约束。正常快照任务软超时为两小时，
硬超时为两小时一分钟；强制终止可能丢失最后一批未提交的数据。

快照启动必须取得 Redis 锁；Redis 不可用时不降级为无锁并发写入。
同一天已保存的快照保持唯一，重试固定原快照日期，跨日限流等待不会切换日期。
GitHub 配额可能与其他应用或同账号 Token 共享，不能把更多 Token 视为独立额度。

真实 API 性能验证使用临时 SQLite 数据库，生产数据库只读，不会恢复已取消任务或发布榜单：

```powershell
docker compose exec -T worker python -m worker.app.benchmark --limit 200 --timeout 300
docker compose exec -T worker python -m worker.app.benchmark --limit 0 --timeout 2100
```

配额不足时验证返回 `deferred`；验证失败、取消、限流会返回非零退出码。
完整验证的默认超时是 35 分钟，任务结束后输出已保存数量、快照耗时和四个榜单耗时。
临时库验证主要衡量网络吞吐，不替代生产 PostgreSQL 写入性能验证。
按约 3,500 个仓库、每次请求约一秒估算，快照目标为 16–22 分钟，
发现加快照的累计处理时间为 18–27 分钟；不包含两个定时任务之间的空闲时间或限流等待。

### Docker 开发模式

API、Worker 和 Beat 共用同一个后端镜像。`docker-compose.override.yml` 会在本地开发时
自动挂载 `backend` 与 `worker` 源码，API 代码修改后自动重载；Worker 代码修改后只需重启
进程，无需重新构建镜像：

```powershell
# 首次启动，或 pyproject.toml / Dockerfile 发生变化
docker compose up -d --build

# 普通 Worker 代码修改
docker compose restart worker beat

# 新增数据库迁移
docker compose exec -w /app/backend api alembic upgrade head
docker compose restart worker beat
```

部署时不加载开发覆盖配置：

```powershell
docker compose -f docker-compose.yml up -d --build
```

## 排名口径

```text
net_delta   = end_stars - start_stars
growth_rate = net_delta / start_stars
```

- 基线快照必须早于周期边界，且与边界相差不超过 36 小时。
- 缺少有效基线的仓库仍会进入当期榜单，显示为无增长，待历史快照满足周期后切换为真实增量。
- 榜单只代表 RepoPulse 当前跟踪的候选仓库，不表示全 GitHub 排名。
- 只保存仓库聚合数据，不保存 Star 用户身份。

## 验证

### README 的 AI 摘要与中文译文

详情页的「总结翻译」生成简短的中文项目介绍及最多三个核心要点，不会翻译整篇 README。「中文译文」在没有记录时才请求全文翻译；两种生成操作都需要先通过齿轮入口配置模型和 API Key。

成功生成的全文译文保存在当前浏览器的 IndexedDB 中。退出详情页、刷新或重启浏览器后，点击「中文译文」即可查看已有记录，无需再次配置模型或调用模型。只有主动点击「重新翻译」才会替换记录；失败或取消会保留旧译文。原文更新后会显示提示，不会自动翻译。

翻译记录按仓库区分，不包含 API Key，不跨设备同步，也不会自动过期；清理浏览器的网站数据会删除记录。若浏览器禁止存储或空间不足，页面会提示保存失败，本次结果仍可在当前页面阅读。摘要仅保留在当前页面中。

### 检查命令

```powershell
cd backend
.\.venv\Scripts\python.exe -m ruff check app ..\worker tests
.\.venv\Scripts\python.exe -m mypy app ..\worker
.\.venv\Scripts\python.exe -m pytest

cd ..\frontend
npm run lint
npm run typecheck
npm run build
npm test -- --project=chromium
```

## 环境变量与提交安全

复制 `.env.example` 为 `.env` 后再填写本地配置。`.env` 可能包含 GitHub Token、数据库连接和其他敏感值，已被 `.gitignore` 忽略，禁止提交；提交时只保留 `.env.example`。数据库、缓存、测试截图和构建产物同样不会进入版本库。
