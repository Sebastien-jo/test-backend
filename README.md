# Primmo — Document Pipeline Backend

[![CI](https://github.com/Sebastien-jo/test-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/Sebastien-jo/test-backend/actions/workflows/ci.yml)

Backend for the Primmo document-processing technical test. The original
assignment brief is preserved in [`docs/ASSIGNMENT.md`](docs/ASSIGNMENT.md).

## Getting started

Requires Docker + Docker Compose.

```bash
make up          # copies .env.example -> .env, builds images, starts the stack
```

Then:

- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/health

`GET /health` actively probes Postgres (`SELECT 1`) and Redis (`PING`) and
returns 503 if either is down.

Other targets: `make down`, `make logs`, `make test`, `make lint`, `make format`.

## Project structure

```
app/
  api/       # FastAPI routers, dependencies, Pydantic schemas (health only for now)
  core/      # config, async DB session, Redis client
  models/    # SQLAlchemy models          (later)
  services/  # application logic          (later)
  workers/   # async tasks / pipeline      (later)
  events/    # Redis pub/sub for real-time (later)
```

## Testing & CI

```bash
make test          # run the suite (ruff-clean, no services required)
make lint          # ruff check
make format        # ruff format
```

Tests are **unit + functional only** and never touch Postgres or Redis:
`tests/conftest.py` provides throwaway settings so the app imports without a
real environment. Live-connectivity checks (`/health` against the actual
services) are verified locally via `make up`, not in CI.

CI runs on every push and pull request ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))
as two parallel jobs — **lint** (`ruff check` + `ruff format --check`) and
**test** (`pytest`) — with the uv dependency cache. No service containers are
needed.

## Stack notes

- **Python 3.14** — pinned across the Dockerfile, CI, and `requires-python`.
  Compatibility with the planned async stack (Redis, Celery + `billiard`) was
  verified on 3.14 before pinning.
- **FastAPI + async SQLAlchemy 2.x (asyncpg)** — async from the start; the DB
  session is injected via a FastAPI dependency (`app.core.db.get_db`).
- **PyJWT**.
- **uv** for dependency management, **ruff** for lint + format, **pytest**
  (+ pytest-asyncio, httpx) for tests.
