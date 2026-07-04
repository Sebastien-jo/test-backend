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

### Log in via Swagger (30 seconds)

The stack auto-seeds two organizations with one user each:

| Organization | Email             | Password      |
| ------------ | ----------------- | ------------- |
| Acme         | `alice@acme.test` | `password123` |
| Globex       | `bob@globex.test` | `password123` |

In [Swagger](http://localhost:8000/docs): click **Authorize**, enter one of the
emails as *username* + its password, **Authorize** → protected endpoints (e.g.
`GET /auth/me`) now carry the token automatically.

Other targets: `make down`, `make logs`, `make test`, `make lint`, `make format`,
`make migrate`, `make seed`.

## Project structure

```
app/
  api/       # routers (health, auth, documents), deps.py, schemas.py
  core/      # config, async DB session, Redis client, security (hashing + JWT)
  models/    # SQLAlchemy models (organizations, users, documents, steps, webhooks)
  services/  # logic — status.py (state machine), documents.py, storage.py
  workers/   # async tasks / pipeline      (later)
  events/    # Redis pub/sub for real-time (later)
scripts/     # seed.py (idempotent demo data, runs at startup)
```

## Data model

```
organizations (id, name, created_at)                     # tenant boundary
users          (id, organization_id→, email·, hashed_password, created_at)
documents      (id, organization_id→, uploaded_by→, filename, storage_path,
                status‡, partner_job_id·, created_at, updated_at)   # (org_id, created_at DESC) index
processing_steps (id, document_id→, name‡, status‡, attempts, error,
                started_at, finished_at, …)              # unique (document_id, name)
webhook_events (id, job_id, payload jsonb, signature_valid, received_at)  # append-only audit
```
`→` FK · `·` unique · `‡` native Postgres enum.

The document status is **derived**, never stored as truth by the ORM: the state
machine in [`app/services/status.py`](app/services/status.py) (pure, no DB/HTTP —
exhaustively unit-tested) is the single source of the pipeline rules.

**Webhook dedup:** `webhook_events` is an append-only audit trail, so `job_id`
is indexed but **not unique** — a partner may legitimately re-POST the same
`job_id` (retries, or a status progression). Idempotency is enforced in the
handler; the strong "one partner job per document" guarantee lives on the unique
`documents.partner_job_id`.

**Migrations** (Alembic, async) run at API container startup (`alembic upgrade
head` before uvicorn). Fine here; in production this should be a dedicated
migration job run once before rollout, not on every replica start.

## Authentication & multi-tenancy

- **Flow:** `POST /auth/login` (email + password) returns a **JWT** (HS256,
  60 min, configurable via `JWT_EXPIRES_MINUTES`) with claims `sub` (user id) and
  `org` (organization id). Protected endpoints depend on `get_current_user`
  ([`app/api/deps.py`](app/api/deps.py)), which verifies the token and re-checks
  the user still exists.
- **Passwords:** hashed with **Argon2id** (`app/core/security.py`) — current
  OWASP recommendation, no bcrypt 72-byte limit.
- **Tenant invariant:** the `organization_id` **always** comes from the signed
  token, never from a client parameter. This is what isolates one organization's
  data from another; every future query scopes on it.
- **No user enumeration:** login returns the same 401 whether the email is
  unknown or the password is wrong (timing equalized).
- **Seed credentials:** see [Getting started](#log-in-via-swagger-30-seconds).
- **Prod note:** access-token only, no refresh token (out of scope). In
  production we'd add short-lived access + rotating refresh tokens (httpOnly
  cookie or secure store) and token revocation.

## Documents API

All endpoints require a bearer token and are scoped to the caller's org.

- `POST /documents` — multipart upload; creates the document in `pending` with its
  4 pipeline steps (no processing triggered yet). **PDF only**, validated on the
  file's magic bytes (`%PDF-`), not the spoofable Content-Type → 415 otherwise.
  Empty files → 400, files over `MAX_UPLOAD_BYTES` → 413. The allowlist would
  widen as more formats are ingested.
- `GET /documents/{id}` — detail with steps; status is **derived** via
  `status.py`. Missing *or* another org's document → **404** (never 403 — don't
  reveal another tenant's resources).
- `GET /documents` — org's documents, `created_at DESC`, `limit`/`offset`
  (default 20, max 100); each item: filename, uploader email, derived status.
  Uploader joined + steps selectin-loaded (no N+1).

**Tenant isolation:** `organization_id` comes from the token; every query filters
on it, and storage keys are `{org_id}/{doc_id}/{filename}`.

**Storage** is a 3-method `FileStorage` Protocol (`save`/`open`/`delete`) over
opaque keys — `LocalFileStorage` (path-traversal-guarded) for now.

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
