# RepoPulse

**简体中文** | [English](README.en.md)

[![CI](https://github.com/SSlaks/RepoPulse/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SSlaks/RepoPulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

RepoPulse 是一个中文 GitHub 开源项目增长榜。它每天按快照记录候选仓库的 Star 数，计算最近 1、7、14、30 天的净增长，并提供筛选、仓库详情与趋势图，帮助你用可解释的数据发现正在增长的项目。

**在线网站：<https://repopulse.slak7.cn>**

当前正式版本为 **v1.0.0**，提供源码与 Docker Compose 自建方式；功能范围和已知限制见 [1.0 发布说明](docs/releases/v1.0.0.md)。`main` 可能包含后续开发中的变更，体验正式版本请使用发布 Tag。

## 界面

![RepoPulse 首页榜单](docs/assets/home.png)

<details>
<summary>更多截图（详情页与移动端）</summary>

![仓库详情与趋势图](docs/assets/detail.png)

![移动端榜单](docs/assets/mobile.png)

</details>

> 截图取自在线网站，展示截图拍摄时的界面与数据；最新榜单以网站当前显示为准。

## 功能

- **发现正在增长的项目**：查看最近 1 / 7 / 14 / 30 天的 Star 净增长与增长率。
- **缩小关注范围**：按语言、主题、Star 数筛选，搜索项目名称或简介。
- **了解项目增长走势**：打开仓库详情，查看历史趋势与项目 README。
- **用中文快速读懂项目**：配置自己的 AI API Key，生成 README「总结翻译」或全文「中文译文」。
- **本地体验或自行部署**：使用演示数据浏览榜单，也可以配置 GitHub Token 开启真实采集。

## 快速开始

前置条件：[Git](https://git-scm.com/downloads)、已启动的 [Docker](https://docs.docker.com/get-docker/) 与 Compose v2（用 `docker compose version` 确认）。以下命令从全新克隆开始，固定使用 `v1.0.0`，只在 `.env` 缺失时复制模板。已有克隆请跳过前两行，在仓库根目录执行其余命令。

**Bash**

```bash
git clone --branch v1.0.0 --depth 1 https://github.com/SSlaks/RepoPulse.git
cd RepoPulse
[ -f .env ] || cp .env.example .env
docker compose up -d --build --wait --wait-timeout 180 postgres redis api frontend
```

**PowerShell**

```powershell
git clone --branch v1.0.0 --depth 1 https://github.com/SSlaks/RepoPulse.git
cd RepoPulse
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
docker compose up -d --build --wait --wait-timeout 180 postgres redis api frontend
```

这是最快的静态演示路径，使用自带的演示数据，**不需要 GitHub Token 或 AI API Key**。命令构建并启动数据库、缓存、API 与前端，等待服务就绪；`api` 会在一次性迁移服务成功后写入演示数据。

打开站点 <http://localhost:3000>，API 文档见 <http://localhost:8000/docs>。

验证容器状态与 API 就绪情况：

```bash
docker compose ps
```

<http://localhost:8000/api/v1/ready> 应返回 HTTP `200` 和 `status: ok`。如果启动失败，用 `docker compose logs --tail 100 api frontend migrate` 查看日志。停止本地服务用 `docker compose down`，会保留数据卷。

浏览演示榜单不需要 `worker` 和 `beat`。真实采集需要先在 `.env` 配置 `GITHUB_TOKEN`，再启动 `docker compose up -d worker beat`；生产自建请按[开发快速上手](docs/getting-started.md)准备生产配置。AI 总结与翻译另需在浏览器里配置自己的 API Key，是否收费取决于所选模型提供方。

## 纯源码开发

不用 Docker 时需要 Python 3.12 及以上与 Node.js 22。请开两个终端，各自从仓库根目录开始，`uvicorn` 会持续占用终端 1。

**终端 1：后端 API**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

**终端 2：前端**

```powershell
cd frontend
npm ci
npm run dev
```

后端默认使用 SQLite 并在启动时写入演示数据。完整步骤见[开发快速上手](docs/getting-started.md)。

## 技术栈

Next.js / React / TypeScript · FastAPI / Pydantic / SQLAlchemy / Alembic · Celery / Redis / PostgreSQL · Pytest / Playwright / Ruff / Mypy

## 排名口径

- 每日按快照记录候选仓库的 Star 数，计算最近 1、7、14、30 天的净增长：`net_delta = end_stars - start_stars`，`growth_rate = net_delta / start_stars`。
- 目标日与基线快照按 UTC 日历日计算，基线取 `as_of - period`。
- 榜单只覆盖 RepoPulse 当前跟踪的候选仓库，不代表全 GitHub 排名，也不复刻 GitHub Trending 的算法。
- 已发布的历史榜单不回溯重算，后续数据更新不会改写既有排名。

## 测试与贡献

后端校验、前端 lint / typecheck / build 与 Playwright 端到端测试的完整前置条件和命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。端到端测试当前包含 `desktop-chromium` 与 `mobile-chromium` 两个项目，需要先构建前端并准备好后端与 Redis：

```powershell
cd frontend
npm test -- --project=desktop-chromium
```

## 文档

- [开发快速上手](docs/getting-started.md)
- [1.0 发布说明](docs/releases/v1.0.0.md)
- [生产部署与运维（维护者手册）](docs/operations.md)
- [访问限流与租约](docs/rate-limits.md)
- [参与贡献](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [问题反馈](https://github.com/SSlaks/RepoPulse/issues)
- [发布记录](https://github.com/SSlaks/RepoPulse/releases)

## 提交安全

`.env` 与 `.env.production` 可能包含 GitHub Token、数据库连接等密钥，已被 `.gitignore` 忽略，禁止提交。模板见 `.env.example` 与 `.env.production.example`。若怀疑密钥泄漏，请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

[MIT](LICENSE)
