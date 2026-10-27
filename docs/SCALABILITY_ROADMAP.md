# Convoy Backend — Scalability Roadmap (10k+ Concurrent Users)

Audit date: 2026-10-06. Scope: core trucking marketplace APIs + the video/1:1-call
feature. Context assumed: "10k+ concurrent" means ~10k active app sessions
(mostly idle, bursty real request rate), and horizontal scaling (multiple app
instances + a bigger/replicated DB) is on the table, not just single-box tuning.

Findings are grounded in the actual code (file/line references below), not
guesses — each one was verified by reading the relevant source before being
listed here.

## How to read this

- **P0** — fix these regardless of scaling plans. Several are correctness bugs
  that bite at moderate concurrency *today*, not just at 10k+.
- **P1** — needed specifically once you add more than one app instance.
- **P2** — hardening/observability so you can see problems at scale instead of
  guessing.

---

## P0 — Fix before anything else

### 1. Blocking synchronous HTTP calls stall the entire server — ✅ FIXED (2026-10-13)

**Files:** `services/google_places.py`, `services/sms.py`, `services/kyc.py`,
`services/ulip.py` — all use `requests.get/post` (synchronous) directly inside
`async def` route/service functions.

**Why it matters:** Python's asyncio event loop is single-threaded. One
blocking `requests` call freezes *all* concurrent request processing on that
process for its full duration — hundreds of ms to several seconds when
Cashfree/ULIP/Veup are slow or under their own load. This is the single
biggest responsiveness bottleneck in the codebase, and it hurts well before
10k concurrent users — a handful of simultaneous OTP sends or location
searches already serializes behind each other and stalls unrelated requests
(e.g. someone browsing search results waits behind someone else's slow SMS
call).

**Fix:** wrap each call in `await asyncio.to_thread(requests.post, ...)` as a
minimal diff, or migrate these four files to `httpx.AsyncClient` (already a
dependency, added for Bunny) for a cleaner long-term fix.

**Done:** chose the `asyncio.to_thread` approach, applied at every router call
site into these 4 service files (not inside the service files themselves —
see below). All 12 call sites across `routers/locations.py`, `routers/auth.py`,
`routers/kyc.py` (4 sites), `routers/verification.py` (4 sites, including the
loop in `batch_verify`), and `routers/vehicles.py` now wrap the public
entry-point call in `await asyncio.to_thread(...)`. The internal call graphs
inside `services/kyc.py`/`services/ulip.py` (private helpers calling each
other, `ulip.py`'s `threading.Lock`-guarded token cache) were deliberately
left untouched — wrapping only at the call site runs the *entire* synchronous
call graph in one worker thread per request, which is simpler, lower-risk,
and actually correct: `ulip.py`'s existing `threading.Lock` around its token
cache was already written to guard concurrent *thread* access, which is
exactly the execution model this introduces. Verified with an isolated
before/after test showing a concurrent "cheap" coroutine's ticks are delayed
by the full blocking-call duration before the fix and run on schedule after;
confirmed the app still imports/boots cleanly and all edited endpoints keep
their correct auth gating.

### 2. Firebase push-notification sends block the event loop too

**Files:** `notifications/sender/firebase_sender.py` (`messaging.send`,
`messaging.send_each`), called from `services/notifications.py` and
`services/campaigns/executor.py`.

**Why it matters:** firebase-admin's SDK calls are synchronous. A campaign
"Send Now" or the scheduler tick sending to a large audience runs multiple
chunked `send_each` calls synchronously — each one freezes the whole server.
The bigger your user base gets, the bigger campaign audiences get, the longer
each campaign send blocks everything else.

**Fix:** same as #1 — wrap in `asyncio.to_thread`, or better, move all
notification/campaign sends off the request/scheduler path entirely onto a
background worker (see #3's option (c)).

### 3. Horizontal scaling will silently duplicate side effects today

**Files:** `app.py` (three `asyncio.create_task(...)` loops: post-expiry,
device-cleanup, scratch-card-expiry), `notifications/scheduler/__init__.py`
(in-process `AsyncIOScheduler`, no persistent/shared job store),
`services/campaigns/scheduler_tick.py::run_due_campaigns` (no locking at all).

**Why it matters:** every loop/scheduler runs independently per process with
zero coordination. The moment a 2nd app instance is added (implied by "open to
horizontal scaling"), **both instances' schedulers will independently see the
same due campaign and both send it** — a 5,000-recipient campaign becomes 2
(or N) full sends, duplicate notifications to everyone. The expiry/cleanup
loops are idempotent `UPDATE ... WHERE` sweeps, so duplicate runs there are
just wasted DB load, not wrong — but the campaign tick is a genuine
correctness bug waiting to happen the day you scale out.

**Fix**, before adding a 2nd instance, pick one:
- (a) designate one instance as "the scheduler" via an env flag,
- (b) add a real distributed lock around `run_due_campaigns()` — a Redis
  `SET NX` with a TTL (Redis is already in the stack from the video feature)
  or a Postgres advisory lock (`pg_try_advisory_lock`),
- (c) **(recommended long-term)** extract all cron-ish work into one
  always-single-instance worker process — the same pattern already used for
  the video prefetch worker (`worker.py`) — and remove these loops from the
  API process entirely.

### 4. Single uvicorn process, no multi-worker, no multi-instance

**File:** `deploy/convoy-api.service` —
`ExecStart=... uvicorn server:app --host 0.0.0.0 --port 5050` (no `--workers`).

**Why it matters:** this is the entire production footprint today — one
Python process, one event loop. All CPU-bound work and any blocking I/O (see
#1/#2) fully serializes through it.

**Fix (cheap, same VM):** run multiple workers —
`uvicorn ... --workers 4` or `gunicorn -k uvicorn.workers.UvicornWorker -w 4`.
Rule of thumb: `workers = 2 × vCPUs + 1`.
**Fix (what you said you're open to):** a load balancer (Azure Load Balancer /
Application Gateway, or an nginx box) in front of 2+ VMs, each running a
multi-worker API process. **Do #3 first**, or this multiplies the
duplicate-send bug instead of just concurrency.

### 5. DB connection pool is unconfigured

**File:** `db/session.py` —
`engine = create_async_engine(DATABASE_URL, echo=False)`, no `pool_size`/
`max_overflow` set.

**Why it matters:** SQLAlchemy's async engine defaults to `pool_size=5` +
`max_overflow=10` = 15 connections per process. With multiple workers per
instance and multiple instances, nobody is coordinating total connections
against Postgres's `max_connections` ceiling (Azure Postgres Flexible Server
tiers cap this low on small SKUs) — you'll either exhaust it outright, or your
own pool is too small for one process's real concurrency and requests queue
waiting for a connection.

**Fix:** explicitly set `pool_size`/`max_overflow`/`pool_timeout`/
`pool_recycle` sized as `(Postgres max_connections − headroom) ÷ (worker
processes × instances)`, and put **PgBouncer** (transaction-pooling mode) in
front of Postgres once you have multiple app instances — the standard way to
let many app-side connections share far fewer real Postgres backend
connections.

---

## P1 — Needed specifically to scale past one box

### 6. No caching layer for read-heavy/slow-changing data

Candidates: Google Places autocomplete (same queries from many users), the
truck-type list, admin dashboard aggregates, hot search pages. You already
have Redis in the stack (added for video prefetch) — reuse it as a
general-purpose cache (a small `services/cache.py` wrapper, same shape as
`services/redis_cache.py`), with short TTLs for anything that doesn't need to
be instantly fresh.

### 7. No reverse proxy in front of uvicorn

TLS termination, gzip, connection/keep-alive tuning, and slow-client buffering
all currently fall on uvicorn directly or aren't happening at all. An nginx
box or Azure Application Gateway also gives you a clean place to add rate
limiting (#8) and makes rolling deploys across instances far less disruptive
than a hard `systemctl restart`.

### 8. No rate limiting anywhere

`POST /api/auth/send-otp` costs real money per SMS and has zero throttle — a
bad actor or a buggy client retry-loop can run up your Veup bill or degrade
service for everyone. No endpoint in the codebase has any limiter today. Add
one (`slowapi`, or a small Redis-token-bucket middleware) at minimum on
auth/OTP and KYC endpoints.

### 9. No graceful shutdown / zero-downtime deploy story

The deploy workflow does `sudo systemctl restart convoy-api` — in-flight
requests get dropped at that instant. Fine for one low-traffic VM; not once
you have real concurrent users, and especially not with multiple instances
you'd want to roll one at a time (drain, wait for in-flight requests, restart,
repeat) instead of restarting everything simultaneously.

---

## P2 — Hardening / observability

### 10. No APM/metrics/tracing

Structured access logs exist (`middleware/logging.py`) but there's no p50/p95/
p99 latency visibility, no error-rate alerting, no query-performance insight.
At 10k+ users you need to know *where* it's slow, not find out from user
complaints. Start cheap: Postgres's own `pg_stat_statements` extension
(answers "which queries are slow/frequent" directly) plus a hosted APM
(Sentry for errors, or an OpenTelemetry exporter) rather than building this
from scratch.

### 11. Synchronous `RotatingFileHandler` logging on every request

Minor next to #1/#2, but every request does blocking disk I/O via Python's
stdlib logging handlers inside the request path (`logging_config.py`). Fine at
today's traffic; worth moving to `logging.handlers.QueueHandler` + a listener
thread once request volume is high enough to show up in profiling — not worth
prioritizing until the bigger items are fixed and this is actually measured.

### 12. Index audit — mostly already solid, verify with real data

The schema is already fairly disciplined about composite indexes on hot query
patterns (`wallet_transactions(user_id, created_at)`,
`scratch_cards(user_id, earned_at)`, GiST indexes on every spatial column,
etc.) — no obvious glaring gap found by reading the schema. Once there's real
production query volume, run `EXPLAIN ANALYZE` against whatever
`pg_stat_statements` (#10) flags as slow/frequent, rather than guessing from a
static read — that's the reliable way to find real gaps.

---

## Video/calls feature — separate scaling profile

In scope per your answer, but it scales differently from the rest of the API:

- **Bunny Stream and Agora are managed external services.** "Scaling to 10k
  users" here is mostly a billing/capacity conversation with those vendors
  (concurrent Agora minutes, Bunny bandwidth), not a code change on your side.
  Check both vendors' plan limits before video/calls see heavy use.
- **The RabbitMQ prefetch worker (`worker.py`) is a single consumer
  process** — fine today, since it's a pure performance optimization (cache
  warming) and not on the critical path: everything still works, just
  slightly less snappy, if it falls behind. If it becomes a bottleneck, run
  multiple copies of `worker.py` consuming the same queue — RabbitMQ handles
  competing consumers natively, no code change needed.
- **Agora token minting (`services/agora_client.py`) is CPU-bound but
  fast** (HMAC-based) — negligible at 10k scale.
- Everything else here is covered by the general P0/P1 items above; the
  Redis/RabbitMQ clients already degrade gracefully if either is unavailable
  (built that way during the video/calls feature work).

--- 

## Suggested sequencing

1. **#1 + #2** (blocking I/O) — biggest, cheapest win; fixes real-world
   responsiveness today regardless of scaling plans.
2. **#5** (DB pool) + **#4** (multi-worker on the current VM) — cheap,
   immediate concurrency headroom, zero new infra.
3. **#3** (fix duplicate-side-effect risk) — must happen *before* a 2nd
   instance goes live, not after.
4. Add the 2nd+ instance + load balancer, plus **#7** (reverse proxy) and
   **#9** (graceful deploys).
5. **#6** (caching) + **#8** (rate limiting) as real traffic grows.
6. **P2 observability** — ideally stood up *before* step 4, so you can
   actually measure the impact of each change as you make it.
