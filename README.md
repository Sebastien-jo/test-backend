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

### Configuration

`make up` copies `.env.example` → `.env`; every setting is read there (see
[`app/core/config.py`](app/core/config.py)). The ones that matter:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` / `REDIS_URL` | Postgres (async) and Redis DSNs |
| `JWT_SECRET`, `JWT_EXPIRES_MINUTES` | access-token signing key + lifetime (HS256) |
| `PARTNER_HMAC_SECRET` | shared secret for the inbound webhook HMAC (out-of-band) |
| `DEV_ENDPOINTS_ENABLED` | exposes `/dev/sign-webhook` — **must be `false` in prod** |
| `MAX_UPLOAD_BYTES` | upload size cap (default 20 MiB) |
| `CELERY_WORKER_CONCURRENCY` | worker thread-pool size |
| `LOG_FORMAT` / `LOG_LEVEL` / `ENVIRONMENT` | `console` locally, `json` in prod ([Observability](#observability)) |

Use real, random secrets outside local dev (e.g. `openssl rand -hex 32`).

## Project structure

```
app/
  api/       # routers (health, auth, documents), deps.py, schemas.py, middleware.py
  core/      # config, async DB session, Redis client, security, logging
  models/    # SQLAlchemy models (organizations, users, documents, steps, webhooks)
  services/  # logic — status.py (state machine), documents.py, storage.py, webhooks.py
  workers/   # Celery pipeline — celery_app, steps (mocks), pipeline, transitions
  events/    # publisher.py — Redis pub/sub for real-time (SSE)
scripts/     # seed.py (runs at startup)
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
  reveal another tenant's resources). Carries `results_available` (true once the
  results endpoint returns 200).
- `GET /documents/{id}/results` — the extracted data, **gated on completion** (the
  brief's "once processing is finished"). Only a `ready` document returns **200**
  with the aggregate: `ocr_text` / `metadata` / `chunks` (the three steps'
  `result`s) plus `partner` (the `result` of the webhook that validated it). A
  non-terminal *or* `failed` document → **409 Conflict** `{status, detail}` — the
  resource exists but its state forbids the read (404 would be a lie; no partial
  results, the brief gates on completion). Missing/other-org → **404**. One query
  for the document + steps, one for the partner event — no N+1.
- `GET /documents` — org's documents, `created_at DESC`, `limit`/`offset`
  (default 20, max 100); each item: filename, uploader email, derived status.
  Uploader joined + steps selectin-loaded (no N+1).

Together these cover the brief's four needs: **upload** (`POST`), **track**
(`GET /{id}` + [SSE](#real-time-tracking-sse)), **retrieve results**
(`GET /{id}/results`), and **list** (`GET /documents`).

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
document reaches `waiting_partner`, then the inbound webhook flips it to `ready`.

**The partner.** `external_call` submits the
extracted `ocr`/`metadata`/`chunks` to an external **compliance provider** that
screens the document (e.g. AML/KYC and sanctions checks) before it can be
published. The provider works **asynchronously**: it returns an opaque `job_id`
immediately (→ `waiting_partner`) and later notifies the verdict via a signed
webhook (→ `ready`, or `failed`) — see [Partner webhook](#partner-webhook).

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
5. `GET /documents/{id}/results` → **200** with `ocr_text` / `metadata` /
   `chunks` / `partner`. (Before step 4 the same call returns **409** with the
   current status.)

`/dev/sign-webhook` is gated by `DEV_ENDPOINTS_ENABLED` (true in compose) and
**must be false in production** — it's a signature oracle.

## Real-time tracking (SSE)

`GET /documents/{id}/events` streams every step/document status change as
Server-Sent Events (auth'd, 404 cross-tenant like the rest).

**Transport choice.** The need is **unidirectional, server→client** push, which is
exactly the shape of SSE — the least machinery that fits (judged on the brief's
four axes: API load, server resources, client complexity, disconnect robustness).

| | Polling | WebSocket | **SSE (chosen)** |
| --- | --- | --- | --- |
| **API load** | 5k viewers × ~1 rps of mostly-unchanged status checks | 1 conn/viewer | 1 conn/viewer, a frame only on real change |
| **Server resources** | a DB hit per poll | duplex conn + per-conn state | a coroutine + a Redis sub — idle-cheap |
| **Client complexity** | trivial but wasteful | upgrade + ping/pong + reconnect protocol, upstream half unused | `EventSource`: auto-reconnect + `Last-Event-ID` **built in** |
| **Disconnect robustness** | stateless by accident | manual | native reconnect + resume (below) |

Polling can't hit ~1s latency at 5k viewers without hammering the API/DB for
nothing; WebSocket's duplex channel is dead weight when nothing flows upstream and
adds infra (sticky sessions or a shared bus) plus a client protocol. SSE is plain
HTTP (traverses proxies/LBs, no upgrade), stateless, and horizontally scalable.

**Architecture.** The two state-change choke-points publish to Redis pub/sub after
their DB commit — the worker (`transitions.py`, sync client) on each step change,
the API (`webhooks.py`, async client) on partner confirmation. The SSE endpoint
subscribes and streams. **One channel per document** (`doc:{id}`): an API instance
subscribes only to the documents its clients are watching, not a global firehose
of every tenant — that's what makes it scale.

**Robustness to disconnects.** On (re)connect the endpoint does, in order:
**(1) subscribe, (2) read the sequence boundary then the DB snapshot, (3) stream**
events with `event_id >` the boundary. Subscribing *before* the snapshot closes
the lost-event window; the snapshot **is** the resync, so no server-side event
history is needed. A per-document `INCR` sequence (not the timestamp) gives a
total order and feeds `Last-Event-ID`. Heartbeats (`: keepalive`) traverse proxies
and detect dead connections. Publishing is **best-effort** — Redis down logs a
warning and never fails the pipeline or webhook; the client resyncs from the DB.

**Scale.** Each SSE connection costs a coroutine + a Redis subscription — cheap;
one async uvicorn process holds thousands, so 5k concurrent viewers fit in a
handful of API replicas.

## Scaling to the target

Target (12 months): **100k docs/day ≈ ~1.2 docs/s average** (higher at peak),
**5k concurrent users**, **p95 pipeline < 2 min**. No single component is near its
limit; the point is that the knobs are known and staged, not that it's tuned now.

- **DB (Postgres).** Writes are small per-step row updates — 100k docs × ~4 steps ×
  a few transitions is low hundreds of writes/s at peak, trivial for Postgres. The
  hot read (listing) rides the `(organization_id, created_at DESC)` index. *Knobs:*
  PgBouncer for connection pooling, read replicas for listing/snapshots,
  time-partition the append-only audit tables (`webhook_events`, `step_attempts`).
- **Async orchestration (Celery).** At ~1.2 docs/s average the broker is idle; the
  thread pool (IO-bound mock steps) holds many concurrent tasks per worker, and
  throughput scales by adding worker replicas. **p95 < 2 min:** the critical path is
  `ocr (≤15s) + max(metadata ≤10s, chunking ≤12s) + external_call (≤5s) ≈ ≤32s` of
  work, comfortably under budget unless retries stack (rare at >99.8%/step).
  *Knobs:* per-step queues, DLQ, autoscale on queue depth.
- **Real-time (SSE).** Per-document channels (no global firehose), stateless async,
  horizontal — see [Real-time tracking](#real-time-tracking-sse). *Knob past this
  scale:* one shared Redis subscriber per process instead of one per client.
- **Tenant isolation.** `organization_id` from the JWT, an indexed filter on every
  query — no cross-tenant scan, scales with the same index as the data grows.

## Observability

Production-grade **structured logging**, added as a **pure side layer**: remove
it and the application behaves identically — no request, task, or SSE stream can
be broken by a log line. (Metrics are the deliberate next step — see below.)

### Structured logging

**structlog**, one JSON line per event on **stdout** (the container convention —
the platform collects stdout, no files). Fields follow Datadog naming
(`service`, `env`, `status`) so the agent ingests them untouched, but nothing
depends on Datadog to run. `LOG_FORMAT=console` (the local default) renders a
colored, human-readable line instead; `LOG_FORMAT=json` is production. The API
and the worker share the same config, and stdlib loggers (uvicorn, celery,
sqlalchemy) are routed through the same pipeline — one format, no mix.

**Correlation is the backbone.** A `request_id` (short UUID, or an inbound
`X-Request-ID`) is bound to the log context per request and echoed back as
`X-Request-ID`. It then rides into Celery via the task headers, so **one grep
follows an upload from the HTTP call to the last task of the DAG**:

```console
$ docker compose logs worker | grep 28c6cc9ad03b
{"event":"task started","step":"ocr","request_id":"28c6cc9ad03b","attempt":1,...}
{"event":"task finished","step":"ocr","outcome":"SUCCESS","duration_ms":7990.18,...}
{"event":"task started","step":"metadata","request_id":"28c6cc9ad03b",...}   # ‖ chunking
{"event":"step retry scheduled","step":"metadata","error_type":"ValueError","retry_in_seconds":1,...}
{"event":"task started","step":"external_call","request_id":"28c6cc9ad03b",...}
```

`document_id`, `task_id`, `step`, and (in the webhook) `job_id` are bound the
same way. Three representative lines:

```json
{"event":"upload accepted","filename":"deed.pdf","size_bytes":48213,"organization_id":"…","request_id":"28c6cc9ad03b","document_id":"1b61ccb9…","service":"docpipe-api","status":"info","timestamp":"2026-07-05T13:38:14Z"}
{"event":"step retry scheduled","step":"ocr","attempt":1,"error_type":"TimeoutError","error":"OCR provider timeout","retry_in_seconds":1,"request_id":"ca66290611fc","service":"docpipe-worker","status":"warning","timestamp":"…"}
{"event":"webhook received","outcome":"processed","signature_valid":true,"job_id":"j_6a80f1b1…","document_id":"1b61ccb9…","service":"docpipe-api","status":"info","timestamp":"…"}
```

**Never logged:** passwords, hashes, JWTs, the HMAC secret, signatures, or file
contents. Webhook payloads already live in the DB audit trail — not duplicated in
logs. A login logs `user_id`/`org` on success and only the `email` on failure; an
invalid webhook signature logs *that it failed*, never the signature.

### Next step: metrics (deliberately deferred)

Logging ships now because it's **fully verifiable without any external
account** — the tests above assert correlation and secret-redaction, and the
JSON stream is inspectable locally. Metrics are the natural next layer, but a
StatsD/DogStatsD (or Prometheus) pipeline can only be validated end-to-end
against a running collector (a Datadog agent/account, a Prometheus scrape).
Shipping an emitter we can't exercise past "the call didn't raise" is a poor
tradeoff, so it's scoped as follow-up rather than added blind.

The intended design: a thin best-effort `increment`/`histogram`/`gauge` module
(one system, not two), emitted from the **same hooks that already log** — so no
new code paths, just an extra sink. Concretely:

- **Pipeline:** step `duration_ms` (tags `step`, `outcome`) and `retries`;
  `failed_permanently` (`step`, `error_type`) — *where* it breaks; document
  `duration_ms` for **upload→waiting_partner vs the < 2 min p95 target**, and a
  `completed` counter (`final_status`) for throughput.
- **API:** request `duration_ms` + `count` (tags `method`, route **template**,
  `status_class`) for latency and error rate.
- **Webhook:** `received` counter by `outcome`; **SSE:** open-connections gauge;
  **uploads:** counter per `org`.

Step durations would reuse the persisted `started_at`/`finished_at` (no parallel
stopwatch). Wiring in production is one setting — point the client at the agent —
and tag cardinality (templated routes, bounded `org`) is the thing to watch.

**Beyond that:** APM / distributed traces spanning the DAG end-to-end, alerting
on `failed_permanently` and p95 breaches, and prebuilt dashboards.

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

## With more time

Consolidated follow-ups (several are also flagged inline where they matter):

- **Delivery correctness.** A **transactional outbox** (or a periodic sweeper) to
  close the enqueue-after-commit gap — today a broker failure after the DB commit
  leaves a `pending` orphan. A **DLQ** + per-step queues for the pipeline. An
  **idempotency key** on `external_call` so a redelivered task can't double-submit
  to the partner.
- **Metrics & tracing.** Ship the deferred metrics layer
  ([Next step: metrics](#next-step-metrics-deliberately-deferred)); add APM /
  distributed traces spanning the DAG; alert on `failed_permanently` and p95
  breaches; build dashboards.
- **Auth.** Short-lived access + rotating **refresh tokens** (httpOnly cookie or
  secure store) and token revocation — today it's access-token only.
- **Storage.** Swap `LocalFileStorage` for **object storage** (S3/GCS) behind the
  existing `FileStorage` Protocol; presigned direct uploads for large files.
- **Webhook hardening.** Request body-size cap, per-partner rate limiting, and
  **replay protection** (reject a stale `occurred_at` / a seen nonce); per-partner
  secrets instead of one shared HMAC key.
- **Ops.** Run migrations as a dedicated job/init-container once per rollout, not on
  every replica start; PgBouncer + read replicas as load grows (see
  [Scaling](#scaling-to-the-target)).
