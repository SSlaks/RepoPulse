# RepoPulse

[简体中文](README.md) | **English**

[![CI](https://github.com/SSlaks/RepoPulse/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SSlaks/RepoPulse/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

RepoPulse is a Chinese-language growth board for open source GitHub projects. Each day it snapshots the Star counts of tracked candidates and computes net growth over the last 1, 7, 14, and 30 days, with filters, repository details, and trend charts. It is built with Next.js, React, and TypeScript on the frontend, and FastAPI, SQLAlchemy, PostgreSQL, Celery, and Redis on the backend.

**Live website: <https://repopulse.slak7.cn>**

## Screenshots

![RepoPulse home ranking](docs/assets/home.png)

<details>
<summary>More screenshots (detail and mobile)</summary>

![Repository detail with trend chart](docs/assets/detail.png)

![Mobile ranking](docs/assets/mobile.png)

</details>

> Screenshots were captured from the live website and reflect the interface and data at capture time; refer to the website for current rankings.

## Features

- Daily 1 / 7 / 14 / 30 day Star net-growth rankings
- Ranking filters, repository details, and trend charts
- Bring-your-own-API-key AI "summary translation" and full "Chinese translation"
- Background README warmup with content persisted in PostgreSQL

## Quick start

Requires [Docker](https://docs.docker.com/get-docker/) with Compose v2 (check with `docker compose version`). Run from the repository root. Copy the env template once after a fresh clone; if `.env` already exists, keep it and do not overwrite it.

**Bash**

```bash
cp .env.example .env
docker compose up -d --build postgres redis api frontend
```

**PowerShell**

```powershell
Copy-Item .env.example .env
docker compose up -d --build postgres redis api frontend
```

This is the fastest static demo path. Only the database, cache, API, and frontend start; `api` waits for the one-shot `migrate` service to succeed, then seeds demo data on startup. `migrate` only runs Alembic migrations. Browsing rankings, details, and trends needs no `worker` or `beat`. Run `docker compose up -d worker beat` only when you need live GitHub collection or README warmup.

Open <http://localhost:3000>, API docs at <http://localhost:8000/docs>.

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
