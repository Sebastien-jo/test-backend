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
  workers/   # Celery pipeline — celery_app, steps (mocks), pipeline, transitions
  events/    # Redis pub/sub for real-time (later)
scripts/     # seed.py (idempotent demo data, runs at startup)
```

## Data model

```
organizations (id, name, created_at)                     # tenant boundary
users          (id, organization_id→, email·, hashed_password, created_at)
documents      (id, organization_id→, uploaded_by→, filename, storage_path,
                status‡, partner_job_id·, created_at, updated_at)   # (org_id, created_at DESC) index
processing_steps (id, document_id→, name‡, status‡, attempts, result jsonb,
                started_at, finished_at)                 # unique (document_id, name)
step_attempts  (id, step_id→, attempt, error, started_at, finished_at, created_at)  # append-only
webhook_events (id, job_id, payload jsonb, signature_valid, received_at)  # append-only audit
```
`→` FK · `·` unique · `‡` native Postgres enum.

Per-attempt errors live in **`step_attempts`** (append-only, one row per
execution — race-free under at-least-once, unlike a single `error` column). The
step keeps only its summary status/attempts; the API surfaces the full history
and, for a *failed* step, its last error.

The document status is **computed by the state machine** in
[`app/services/status.py`](app/services/status.py) (pure, no DB/HTTP —
exhaustively unit-tested) and **persisted** on the document by the transition
helpers (worker on each step change; webhook on partner confirmation). Endpoints
read that stored value — always current because `status.py` is its only writer.

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

## Pipeline orchestration

Upload enqueues a Celery DAG (the document stays `pending` until it runs):

```
chain( ocr, chord( group(metadata, chunking), external_call ) )
```
`ocr` first; `metadata`/`chunking` in parallel; `external_call` last → the
document reaches `waiting_partner` (the inbound webhook that flips it to `ready`
is the next phase).

- **Why Celery:** native retries + exponential backoff and `chord` for the
  metadata/chunking fan-in — orchestration we'd otherwise hand-roll. It's also
  the team's stack.
- **Thread pool** (`--pool=threads`, concurrency 32): the steps are `time.sleep`
  (IO-bound-like), so threads hold far more concurrent tasks than prefork at
  equal memory.
- **Results in the DB** (`processing_steps.result` JSONB): each task writes its
  output; downstream tasks read upstream results from the DB, not from Celery
  arguments — retries/redelivery make argument passing fragile, and the DB makes
  every task independently replayable. `external_call`'s job_id is also copied to
  `documents.partner_job_id`.
- **Retries:** `autoretry_for` the mock exceptions, `retry_backoff` + jitter,
  `max_retries=5`. With ~1/3 failure per attempt, 6 attempts give
  `(1/3)**6 ≈ 0.14%` failure → **>99.8%** success per step.
- **Chord on failure (verified):** if `metadata` *or* `chunking` fails
  permanently, the chord body does not fire — `external_call` stays `pending` and
  the derived document status is `failed`.
- **Single transition helper** (`app/workers/transitions.py`): every step change
  goes through it (validate → timestamps/result/error → recompute document
  status). It's the one place phase 7 will hook real-time events.
- **Enqueue after commit:** the pipeline is enqueued only once the document is
  committed (the worker must not look up an invisible row). Limitation: if the
  enqueue fails after the commit, the document is a `pending` orphan — prod fix is
  a transactional outbox or a periodic sweeper.

**Idempotency:** `acks_late` + `reject_on_worker_lost` give at-least-once
execution, so a step can be delivered/run more than once (redelivery, or
concurrent duplicate delivery). The transition helper makes this safe: already
terminal steps are skipped, a terminal outcome (`done`/`failed`) is authoritative
from any active state so a success is never dropped, and any remaining illegal
move (e.g. a laggard retry after completion) is a logged no-op rather than a hard
error that would break the chord.

## Partner webhook

`POST /webhooks/partner` receives the partner's async notification and flips the
document from `waiting_partner` to `ready` (or `failed`).

- **Auth = the signature.** No JWT (the caller is the partner, not a user). The
  `X-Partner-Signature` must be `HMAC-SHA256(raw_body, PARTNER_HMAC_SECRET)`,
  verified over the **exact received bytes** (never a re-parsed JSON — any
  whitespace/key-order change breaks it) with a constant-time compare. Missing or
  wrong → **401**, no hint which.
- **Opaque + no enumeration.** Every request is audited in `webhook_events`
  first (valid or not). A valid signature always returns `200 {"status":"received"}`
  — known, unknown, or duplicate job_id (never 404, so outsiders can't probe which
  job_ids exist; an early notification is simply logged for investigation). Valid
  signature but malformed body → **422**.
- **Idempotent.** A partner retry re-POSTs the same job_id; terminal stays
  terminal, so the second delivery is a logged no-op (decided on document state,
  not an event count).

### Test it from Swagger in ~30s

1. Upload a document and poll `GET /documents/{id}` until it's `waiting_partner`.
   Grab its `partner_job_id` (`docker compose exec db psql -U primmo -d primmo -c
   "select partner_job_id from documents"`).
2. `POST /dev/sign-webhook` with the JSON
   `{"job_id":"<partner_job_id>","status":"completed"}` → copy `signature` from the
   response.
3. `POST /webhooks/partner`: send the **same** JSON body, put `signature` in the
   `X-Partner-Signature` header → `200`. (Signed over raw bytes, so the two bodies
   must be byte-identical — copy the same JSON into both; don't re-edit it.)
4. `GET /documents/{id}` → **`ready`**. (Re-POST the same → still `ready`.)

`/dev/sign-webhook` is gated by `DEV_ENDPOINTS_ENABLED` (true in compose) and
**must be false in production** — it's a signature oracle.

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
