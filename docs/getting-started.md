# RepoPulse 快速上手

本指南面向第一次接触 RepoPulse 的贡献者和自建者，给出三条路径：最快看到演示、本地源码开发、真实自建生产概览。每条路径都对应仓库里已经存在的配置，命令可以直接复制。生产部署和备份回滚请以 [docs/operations.md](./operations.md) 为准，本指南不执行任何生产部署。

## 前置条件

- Docker Compose v2，用 `docker compose version` 确认。旧版 `docker-compose` 二进制不在讨论范围内。
- 只有源码开发才额外需要 Python 3.12 或更高版本（backend/pyproject.toml 的 `requires-python = ">=3.12"`），以及 Node 22（frontend/Dockerfile 使用 `node:22-alpine`）。
- 默认开发端口都绑定在本机回环地址：3000 前端、8000 API、5432 PostgreSQL、6379 Redis。被占用时先释放对应进程。

## 1. 最快看到演示（Docker Compose）

这条路径用仓库自带的 `docker-compose.yml` 加 `docker-compose.override.yml`。`docker compose` 会自动叠加 override 文件，override 提供演示数据、`ENVIRONMENT=development`，并把端口绑定到 `127.0.0.1`。

### 准备环境文件

`.env` 可能已经存在，已有安装不要覆盖。只在缺失时复制模板。

PowerShell：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Bash：

```bash
[ -f .env ] || cp .env.example .env
```

`.env.example` 里的 `POSTGRES_PASSWORD` 和 `REDIS_PASSWORD` 是本机占位值，只适合本机演示。把主机共享给他人之前先换成真实密码。

### 启动

```bash
docker compose up -d --build postgres redis api frontend
```

这条命令不包含 `worker` 和 `beat`。静态演示不需要它们，真实采集才需要。`api` 依赖一次性的 `migrate` 服务（执行 `alembic upgrade head`）以及 `postgres`、`redis` 健康检查，Compose 会自动把 `migrate` 带上。

### 验证

- API 就绪探针：<http://localhost:8000/api/v1/ready> 应返回 `200` 和 `status: ok`。该探针会同时检查数据库和 Redis，任一不可达或超过两秒会返回 `503` 和 `status: degraded`。
- 前端站点：<http://localhost:3000>。
- 容器状态：`docker compose ps`。
- API 文档：<http://localhost:8000/docs>。

### 演示数据

演示数据不需要 `GITHUB_TOKEN`。种子由两处共同保证：override 把 `ENVIRONMENT` 设为 `development`，并把 `SEED_DEMO_DATA` 设为 `true`。后端在 `environment == "development"` 或 `seed_demo_data` 为真时建表并写入演示仓库（见 backend/app/main.py 的 lifespan 和 backend/app/seed.py）。已有仓库时不会重复播种，重启是安全的。

### DNS 注意

override 只给 `api` 和 `worker` 固定了 DNS `10.20.55.160` 和 `223.5.5.5`，这是维护者本机网络的假设。演示和构建本身不依赖它，但容器里要访问 GitHub 时取决于你网络可用的解析器。如果与你的网络不符，可以另建一个只在本机使用、不提交的 Compose override（例如 `docker-compose.local.yml`），启动时用 `-f` 显式叠加来覆盖这两个服务的 DNS。不要把带有你本机 DNS 的配置提交进仓库。

### 停止

```bash
docker compose down
```

`-v` 会删除 `repopulse-postgres` 和 `repopulse-avatar-cache` 数据卷，演示数据库和头像缓存都会被清空。只在一个全新的、可丢弃的演示项目里这样做；已有数据或生产环境绝对不要用这条命令，也不要把它当成日常清理手段。普通停止用上面的 `docker compose down`。

```bash
docker compose down -v
```

## 2. 源码本地开发

这条路径不用容器运行应用，方便断点和热重载。需要 Python 3.12+ 和 Node 22。

### 后端

```bash
cd backend
python -m venv .venv
```

激活虚拟环境。

PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

Bash：

```bash
source .venv/bin/activate
```

安装依赖并启动：

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

不提供 `.env` 时，backend/app/config.py 的默认值是 SQLite（`sqlite+aiosqlite:///./repopulse.db` 与 `sqlite:///./repopulse.db`）和 `redis://localhost:6379/0`。默认 `environment` 是 `development`，因此会自动建表并播种演示数据。

一个关键点：设置会从当前工作目录读取 `.env`。请在 `backend/` 目录内启动后端，不要从仓库根目录启动。从根目录启动可能读到根目录那份为 Docker 写的 `.env`，里面的数据库主机名是 `postgres` 和 `redis`，这些是容器网络里的名字，在宿主机上不可达。容器主机名不是 `localhost`，两套环境不要混用。

### 前端

后端终端保持运行。新开一个终端，回到仓库根目录，再进入 `frontend/`。不要在后端所在的终端里操作，也不要在后端的工作目录下直接 `cd frontend`。

```bash
cd frontend
npm ci
npm run dev
```

`npm ci` 按 package-lock.json 安装，复现仓库锁定的依赖版本；只有改动依赖时才用 `npm install`。

frontend/next.config.ts 默认 `API_BASE_URL=http://localhost:8000`，把 `/api/v1/*` 重写到本地后端。CORS 默认允许 `http://localhost:3000` 和 `http://127.0.0.1:3000`，开箱可用。

站点在 <http://localhost:3000>，API 文档在 <http://localhost:8000/docs>。

### 没有 Redis 时能做什么

只靠 SQLite 也能渲染源码演示：backend/app/cache.py 在 `RedisError` 时跳过缓存（`get` 返回 `None`，`set` 只记 debug 日志），页面数据从数据库读取。但不要以为只有 SQLite 就能跑通全部源码功能。

- `/api/v1/ready` 会因 Redis `ping` 失败返回 `503` 和 `status: degraded`，这不代表页面不可用。
- README 预热、真实采集（`worker` 和 `beat`）、以及依赖 Redis 租约的回源与 AI 功能都需要 Redis 和相关服务，只在 SQLite 下不会完整工作。

要验证完整链路，启动 Compose 里的 `redis`，注意它带 `REDIS_PASSWORD`，与本地默认 `redis://localhost:6379/0` 的无密码地址不同；再按需启动 `worker` 和 `beat`。

## 3. 真实采集与自建概览

真实采集需要 `GITHUB_TOKEN`，并且额度要够。多配置几个 Token 不构成独立额度，GitHub core 配额可能与同一账号的其他应用共享，配额保护细节见 [docs/rate-limits.md](./rate-limits.md)。

榜单不做历史回溯。每个周期取的基线是 `as_of - period` 对应的 UTC 日历日快照，而且必须精确命中那一天。刚自建时没有历史快照，榜单可在采集达到发布条件后展示，但缺少基线的条目会显示“历史数据不足”。1 天周期的有效净增长需等次日快照，7、14、30 天周期需积累对应天数的快照。口径细节见 README 的「排名口径」一节。

要跑采集才启动 `worker` 和 `beat`，静态演示不需要。

## 4. 自建生产概览

先读 [docs/operations.md](./operations.md)，它是当前的运维手册。下面只说明自建时必须自己决定的部分，不复制维护者的部署假设。

### 环境文件

从 `.env.production.example` 复制出 `.env.production`，替换所有 `CHANGE_ME`。

- `POSTGRES_PASSWORD`、`REDIS_PASSWORD`：写进连接串时要做 URL 编码，模板里的 `CHANGE_ME_url_encoded_*` 就是提醒这一点。
- `TRUSTED_PROXY_TOKEN` 与 `INTERNAL_SERVICE_TOKEN`：必须互不相同，各为至少 32 个字符的随机值。后端在 `ENVIRONMENT=production` 时会校验这两项。
- `ENVIRONMENT=production`、`SEED_DEMO_DATA=false`。
- `FRONTEND_ORIGINS` 和 `NEXT_PUBLIC_SITE_URL`：填你自己的 HTTPS 域名。生产校验禁止 localhost，也要求 origin 是 HTTPS。
- `GIT_SHA`、`BACKEND_IMAGE`、`FRONTEND_IMAGE`：填同一个实际构建的完整 Git SHA。镜像 tag 必须等于这个 SHA，运维 CLI 会据此校验并查找本地镜像。把示例里的 `CHANGE_ME_git_sha` 换成 `<fullSHA>`：

```dotenv
GIT_SHA=<fullSHA>
BACKEND_IMAGE=repopulse-backend:<fullSHA>
FRONTEND_IMAGE=repopulse-frontend:<fullSHA>
NEXT_PUBLIC_SITE_URL=https://你的域名
```

### 先构建按 SHA 打标签的镜像

必须在启动之前完成构建，并保证 tag 与 `.env.production` 里的 SHA 一致。运维 CLI 默认按 `repopulse-backend:<sha>` 和 `repopulse-frontend:<sha>` 查找本地镜像，也接受显式传入的镜像名。

Bash：

```bash
SHA=$(git rev-parse HEAD)
docker build -f backend/Dockerfile --build-arg VCS_REF=$SHA -t repopulse-backend:$SHA .
docker build -f frontend/Dockerfile --build-arg VCS_REF=$SHA \
  --build-arg API_BASE_URL=http://api:8000 \
  --build-arg NEXT_PUBLIC_SITE_URL=https://你的域名 \
  -t repopulse-frontend:$SHA ./frontend
```

PowerShell：

```powershell
$SHA = (git rev-parse HEAD).Trim()
docker build -f backend/Dockerfile --build-arg VCS_REF=$SHA -t repopulse-backend:$SHA .
docker build -f frontend/Dockerfile --build-arg VCS_REF=$SHA `
  --build-arg API_BASE_URL=http://api:8000 `
  --build-arg NEXT_PUBLIC_SITE_URL=https://你的域名 `
  -t repopulse-frontend:$SHA ./frontend
```

后端构建上下文是仓库根目录（Compose 里 `context: .`，`dockerfile: backend/Dockerfile`），前端上下文是 `./frontend`。`NEXT_PUBLIC_SITE_URL` 是前端构建参数，必须和 `.env.production` 里的值一致。

### 启动

已有运行中的自建环境升级时，用运维 CLI 部署这个 SHA，它会停止 Beat、排空 Worker、执行前置备份和迁移，再重建容器。此流程需要已有可备份的数据库，不用于尚未初始化数据库的首次安装：

```bash
python3 scripts/repopulse_ops.py deploy <fullSHA> \
  --env-file .env.production \
  --project-name repopulse \
  --backend-image repopulse-backend:<fullSHA> \
  --frontend-image repopulse-frontend:<fullSHA> \
  --backup-dir backups/predeploy
```

首次自建应在配置和镜像准备完成后，显式只用主 Compose 初始化数据库并启动服务；此时镜像已按 tag 存在，不需要 `--build`：

```bash
docker compose -f docker-compose.yml --env-file .env.production up -d
```

无论哪种方式都显式只用主 `docker-compose.yml`，避免加载开发 override，否则会被覆盖成 `development`、固定开发 Token 和演示种子。主 `docker-compose.yml` 强制 `ENVIRONMENT=production` 和 `SEED_DEMO_DATA=false`，并且只把 `frontend` 发布到 `127.0.0.1:3000`。`postgres`、`redis`、`api` 不对外暴露，反向代理只需把 443 转发到 `127.0.0.1:3000`。发布、备份和回滚的具体参数见 [docs/operations.md](./operations.md)。

### Nginx 与域名

Nginx 模板是 [deploy/nginx.conf.template](../deploy/nginx.conf.template)。先分清两个变量：

- `NEXT_PUBLIC_SITE_HOST`：只有主机名，没有 `https://`、没有路径、不能是多个域名。
- `NEXT_PUBLIC_SITE_URL`：完整 URL，用于前端构建和站点元数据；它不是 Nginx 模板的渲染输入。

模板只接受两个 `envsubst` 变量：`NEXT_PUBLIC_SITE_HOST` 和 `TRUSTED_PROXY_TOKEN`。渲染前应将主机名和与 `.env.production` 一致的可信代理 Token 导出到当前 shell 环境；Compose 的 `--env-file` 不会替你导出它们。按模板头部注释渲染，不要替换其它变量，否则会破坏 Nginx 的 `$host`、`$request_uri`、`$remote_addr` 等运行时变量：

```bash
envsubst '${NEXT_PUBLIC_SITE_HOST} ${TRUSTED_PROXY_TOKEN}' \
  < deploy/nginx.conf.template > /etc/nginx/conf.d/repopulse.conf
```

前置条件：目标主机名的 DNS 已解析到服务器，并已为它签发证书，使 `/etc/letsencrypt/live/<主机名>/fullchain.pem` 与 `privkey.pem` 存在，模板按该主机名引用这两个证书路径。渲染结果包含 `TRUSTED_PROXY_TOKEN`，权限必须限制为仅 Nginx 服务可读。reload 前先执行 `nginx -t` 确认语法。你自有的、解析到该主机的域名都可以用，不要求使用维护者的 `repopulse.slak7.cn`。

## 测试与验证

- 测试和贡献流程见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
- GitHub 限流与配额见 [docs/rate-limits.md](./rate-limits.md)。
- 生产运维见 [docs/operations.md](./operations.md)。
