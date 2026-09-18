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

不加载根目录 `.env.example` 时，开发环境默认使用 SQLite，并自动初始化带有一年快照的演示数据。该模板专用于 Docker Compose；生产配置使用单独的 `.env.production`。

### Next.js

```powershell
cd frontend
npm install
npm run dev
```

网站地址：<http://localhost:3000>  
API 文档：<http://localhost:8000/docs>

### Docker Compose

开发 Compose 使用 `.env.example` 中的 PostgreSQL/Redis 认证串，且 `docker-compose.override.yml` 才会打开本机开发端口并启用演示数据：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

开发 Compose 的前端虽然使用生产构建，但会显式设置 `ENVIRONMENT=development`。本机直连
`localhost:3000` 没有反向代理注入可信客户端身份，因此前端会以受信的 `internal` 身份调用
共享 Redis 限流协调器；`INTERNAL_SERVICE_TOKEN` 只存在于服务端容器环境变量中，不会发送给浏览器。
生产主配置固定使用 `ENVIRONMENT=production`，缺少可信代理身份或内部服务 token 时会直接返回
`503`，不会降级为本地内存限流。单独运行 `npm run dev` 时保留 Next.js 开发环境的本地回退语义。

生产部署必须复制并填写 `.env.production.example`，不要加载开发 override；生产主配置会固定关闭演示数据，并拒绝 SQLite、空密码和 localhost Origin：

```powershell
Copy-Item .env.production.example .env.production
# 编辑 .env.production，使用随机密码并对连接串中的密码进行 URL 编码
docker compose --env-file .env.production -f docker-compose.yml up -d --build
```

PostgreSQL、Redis 和 FastAPI 只加入内部网络，不绑定宿主机公网端口。前端通过同源 `/api/v1/*` 访问，由 Next.js 在容器网络内转发到 `http://api:8000`；生产 HTTPS 应由 Caddy、Nginx 或云负载均衡终止，前端入口仅绑定宿主机回环地址。

Redis 必须使用带密码的 `REDIS_URL`；可用下面的命令确认未认证连接被拒绝（应返回 `NOAUTH`）：

```powershell
docker compose --env-file .env.production -f docker-compose.yml exec redis redis-cli ping
```

Backend 和 Frontend 容器使用非 root 用户、只读根文件系统、`no-new-privileges`、`cap_drop: ALL` 和资源限制。生产响应头包含请求级 CSP nonce、HSTS、`X-Content-Type-Options`、`Referrer-Policy` 和 `Permissions-Policy`。

### PostgreSQL 与 Redis（单独运行）

```powershell
docker compose up -d postgres redis
```

然后根据环境实际设置数据库、Redis 和 GitHub Token。多个 Token 可以用逗号分隔。采集服务需要 Redis：

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

### README 持久化与后台预热

公开、已收录仓库的 README 原文写入 PostgreSQL 的 `repository_readmes` 表，表中同时保存
中文 README 根目录选择、根目录 ETag、文件 ETag、成功/检查时间、下一次刷新时间和退避错误。
数据库是正文真源；Redis 只使用 `readme:v2:*` 的短 TTL 响应缓存和短时队列去重标记，不能用
Redis 数据恢复正文，也不会改动全局 eviction、持久化策略或清空共享队列。README 不使用头像
文件缓存。刷新失败不会删除已保存正文；若 GitHub 元数据确认仓库变为 private，API 会立即隐藏
旧正文并使 v2 缓存失效。

API 只接受当前榜单中已收录的仓库。冷缺 README 只入队后台刷新并返回 `503` 与
`Retry-After`，请求本身不会同步访问 GitHub；已有正文即使 Redis 不可用也会从数据库返回，
陈旧正文同时触发带 Redis NX 去重的后台任务。Next.js README 请求使用 `no-store`，避免把
private 状态或后台更新延迟到页面缓存过期后才生效。

README Worker 默认每 10 分钟运行一批，单批最多 100 个仓库，按无正文、到期时间和 Star 数
排序，目标每 24 小时重新验证。它单并发低速请求，并在每次 GitHub 请求前检查真实 core
配额和 `README_QUOTA_RESERVE`（默认 1000）；配额不足、快照/发现任务活跃、Redis 锁不可证明
或任务超过时间限制时会保存 `waiting`/`resume_at` 后退出，不会无限睡眠。GitHub Token 与其他
应用共享配额，配置多个 Token 也不应视为独立额度。

可在 Worker 容器中查看覆盖率和任务状态、请求取消或预热：

```powershell
docker compose exec -T worker python -m worker.app.readme_admin status
docker compose exec -T worker python -m worker.app.readme_admin warmup --limit 100
docker compose exec -T worker python -m worker.app.readme_admin cancel --job-key <job-key>
```

主要配置如下；调度、批次和退避值会同时传给 API、Worker 和 Beat：

| 环境变量 | 默认值 | 用途 |
|---|---:|---|
| `README_BATCH_SIZE` | 100 | 周期任务单批上限 |
| `README_REFRESH_INTERVAL_SECONDS` | 600 | Beat 周期（10 分钟） |
| `README_REFRESH_HOURS` | 24 | 正文成功后再次检查的间隔 |
| `README_REQUESTS_PER_SECOND` | 0.5 | README 单并发请求速率 |
| `README_QUOTA_RESERVE` | 1000 | 每次请求保留的 GitHub core 配额 |
| `README_JOB_TIMEOUT_SECONDS` | 1800 | 单批软时间限制 |
| `README_FAILURE_BACKOFF_SECONDS` | 300 | 失败退避起点，指数增长并封顶 |
| `README_CACHE_TTL_SECONDS` | 300 | Redis v2 响应缓存 TTL |

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

## 排名口径

```text
net_delta   = end_stars - start_stars
growth_rate = net_delta / start_stars
```

- 目标日与快照统一按 UTC 计算；基线必须位于 `as_of - period` 的 UTC 日历日，且时间不晚于该周期边界。
- 不使用 36 小时回退。缺少严格基线的仓库仍会进入当期榜单，显示为历史数据不足，待后续快照满足周期后切换为真实增量。
- 已发布的历史榜单不回溯重算；新口径从后续发布开始生效。
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

复制 `.env.example` 为 `.env` 仅用于 Docker 开发，生产则复制 `.env.production.example` 为 `.env.production` 并填写随机密码。`.env` 和 `.env.production` 可能包含 GitHub Token、数据库连接和其他敏感值，已被 `.gitignore` 忽略，禁止提交；提交时只保留两个模板。数据库、缓存、测试截图和构建产物同样不会进入版本库。

AI Key 仍按 `repopulse-ai-v1` 格式长期保存在当前浏览器的 `localStorage`，删除配置会完整移除该键。Key 不会写入服务端数据库或日志，但同源页面脚本能够读取它；因此公共设备不应保存 Key，且一旦同源脚本被 XSS 攻破，长期 Key 仍需要轮换。
