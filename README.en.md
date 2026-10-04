# RepoPulse

[简体中文](README.md) | **English**

[![CI](https://github.com/SSlaks/RepoPulse/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SSlaks/RepoPulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

RepoPulse is a Chinese-language growth board for open source GitHub projects. Each day it snapshots the Star counts of tracked candidates and computes net growth over the last 1, 7, 14, and 30 days, with filters, repository details, and trend charts. It is built with Next.js, React, and TypeScript on the frontend, and FastAPI, SQLAlchemy, PostgreSQL, Celery, and Redis on the backend.

**Live website: <https://repopulse.slak7.cn>**

The current stable release is **v1.0.0**, distributed as source code with Docker Compose for self-hosting. See the [1.0 release notes](docs/releases/v1.0.0.md) for scope and known limitations. `main` may contain work for future releases; use a release tag to run the stable version.

## Screenshots

![RepoPulse home ranking](docs/assets/home.png)

<details>
<summary>More screenshots (detail and mobile)</summary>

![Repository detail with trend chart](docs/assets/detail.png)

![Mobile ranking](docs/assets/mobile.png)

</details>

> Screenshots were captured from the live website and reflect the interface and data at capture time; refer to the website for current rankings.

## Features

- **Discover growing projects**: compare Star net growth and growth rates over the last 1, 7, 14, and 30 days.
- **Focus on what interests you**: filter by language, topic, and Star count, or search project names and descriptions.
- **Understand a project's growth**: open repository details to explore historical trends and read its README.
- **Read projects in Chinese**: configure your own AI API key to generate a README summary or full Chinese translation.
- **Try locally or self-host**: browse seeded demo data, or configure a GitHub token to collect real data.

## Quick start

Requires [Git](https://git-scm.com/downloads), a running [Docker](https://docs.docker.com/get-docker/) engine, and Compose v2 (check with `docker compose version`). These commands start from a fresh clone, pin `v1.0.0`, and copy the env template only if `.env` is missing. For an existing clone, skip the first two lines and run the remaining commands from its root.

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

This is the fastest static demo path, using included seed data. **No GitHub token or AI API key is needed.** The command builds and starts the database, cache, API, and frontend, then waits for them to be ready. The API seeds demo data after the one-shot migration service succeeds.

Open <http://localhost:3000>, API docs at <http://localhost:8000/docs>.

Check container status:

```bash
docker compose ps
```

<http://localhost:8000/api/v1/ready> should return HTTP `200` and `status: ok`. If startup fails, inspect `docker compose logs --tail 100 api frontend migrate`. Stop local services with `docker compose down`, which preserves data volumes.

Browsing demo rankings needs no `worker` or `beat`. To collect real GitHub data, first configure `GITHUB_TOKEN` in `.env`, then run `docker compose up -d worker beat`. For production self-hosting, follow the production configuration in the [getting started guide](docs/getting-started.md). AI summaries and translations separately require your own API key in the browser; charges depend on the selected model provider.

## Source-only development

Without Docker you need Python 3.12 or newer and Node.js 22. Open two terminals, each starting from the repository root; `uvicorn` keeps terminal 1 busy.

**Terminal 1: backend API**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

**Terminal 2: frontend**

```powershell
cd frontend
npm ci
npm run dev
```

The backend defaults to SQLite and seeds demo data on startup. Full steps are in the [getting started guide](docs/getting-started.md).

## Ranking semantics

- Each day records candidate snapshots and computes net growth: `net_delta = end_stars - start_stars`, `growth_rate = net_delta / start_stars`.
- Target and baseline snapshots use UTC calendar days, with the baseline at `as_of - period`.
- A ranking covers only the candidates RepoPulse currently tracks. It is not an all-of-GitHub ranking and does not reimplement the GitHub Trending algorithm.
- Published historical rankings are not recomputed; later data does not rewrite existing results.

## Tests and contributing

Full prerequisites and commands for backend checks, frontend lint / typecheck / build, and Playwright end-to-end tests are in [CONTRIBUTING.md](CONTRIBUTING.md). The end-to-end suite has `desktop-chromium` and `mobile-chromium` projects, and needs a built frontend plus backend and Redis.

## Documentation

- [Getting started](docs/getting-started.md)
- [1.0 release notes](docs/releases/v1.0.0.md)
- [Deployment and operations (maintainer runbook)](docs/operations.md)
- [Rate limits and leases](docs/rate-limits.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Issues](https://github.com/SSlaks/RepoPulse/issues)
- [Releases](https://github.com/SSlaks/RepoPulse/releases)

## Commit safety

`.env` and `.env.production` may contain secrets such as GitHub tokens and database URLs. They are ignored by `.gitignore` and must never be committed. Templates are `.env.example` and `.env.production.example`. If you suspect a leak, report it privately per [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
