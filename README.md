# buildlens

[![CI](https://github.com/manojsaiannapareddy/buildlens/actions/workflows/ci.yml/badge.svg)](https://github.com/manojsaiannapareddy/buildlens/actions/workflows/ci.yml)

**AI-powered root-cause diagnosis for CI failures** — reads your GitHub Actions
logs, recognizes repeat failures instantly, and explains new ones with citations
to the exact log lines.

> **Status: in active development.** Foundations (M0) and ingestion (M1) are
> complete: point it at a public repository over HTTP and it will discover
> failed workflow runs, download and store their logs, and record every job.
> Signatures and diagnosis (M2/M3) are next. See
> [Current status](#current-status) for exactly what runs today.

---

## The problem

A red build gives you a check-engine light and ten thousand lines of log output.
Was it a flaky test? A real regression? An expired token? Infrastructure?
Answering takes minutes of scrolling, and most of the time the answer is
something the team has already seen before — but that knowledge lives in
people's heads, not anywhere queryable.

### The insight

Think of two mechanics. The rookie hears a rattle, opens the hood, and
investigates from scratch — every time, even for a fault he fixed last week.
The veteran listens for four seconds and says *"alternator belt, fourth one this
month, here's the fix."* She isn't smarter. She has **memory of past failures**
and a **fingerprint for recognizing repeats**.

An LLM on its own is the rookie with a photographic memory of the internet and
no memory of your car. buildlens is built to be the veteran: it fingerprints
failures first and reserves expensive reasoning for genuine novelty.

## How it works

| Stage | What happens | Status |
|---|---|---|
| **Ingest** | Poll GitHub for failed workflow runs; download log archives before their signed URLs expire | ✅ |
| **Normalize** | Strip ANSI codes and timestamps, collapse progress spam, redact secrets — all while preserving original line numbers | ✅ |
| **Index** | Store chunks with both full-text (`tsvector`) and vector (`pgvector`) representations | M2 |
| **Fingerprint** | Template the terminal error — replacing timestamps, IDs, paths, and durations with placeholders — then hash it into a **failure signature** | M2 |
| **Diagnose** | For unrecognized failures only: hybrid retrieval over the repo's own history, then a schema-constrained LLM call whose citations are mechanically validated | M3 |
| **Observe** | Stream results over SSE; meter every token, dollar, and millisecond; trace everything by request ID | M3 |

Three design choices carry the product:

**Fingerprint before you think.** Normalization turns
`Test failed at 14:32:07 in /tmp/build-a8f3/test_auth.py:41 after 2.3s` into
`Test failed at <TIME> in <PATH>:<NUM> after <DURATION>`, which hashes
identically across runs. "Have I seen this?" becomes a hash lookup: milliseconds,
zero cost. The LLM is reserved for novelty.

**Constrain and verify, don't trust.** The model must return a fixed schema —
category, analysis, suggested fix, and **evidence with specific line ranges** —
and every citation is checked against the real log before the diagnosis is
shown. A hallucinated citation fails validation rather than reaching a user.

**Line numbers are load-bearing.** Because citations are ranges into the raw
log, normalization may *drop* lines but never *renumber* them. That single
invariant shapes the whole pipeline: redaction runs per line and never touches
line structure, PEM key bodies get a state machine instead of a multiline
regex, and the line splitter avoids `str.splitlines()` — which also breaks on
form feed, and would shift every number after one.

---

## Current status

**141 tests passing.** Four quality gates — ruff, mypy, `alembic check`, pytest
— run locally via pre-commit and in CI on every push.

### M0 — Foundations: complete

| Capability | Detail |
|---|---|
| HTTP service | FastAPI behind uvicorn, built via an application factory |
| Configuration | Twelve-factor: env vars parsed and validated by Pydantic at startup; the process **refuses to boot** on invalid config |
| Request tracing | Every request gets a UUID (or honors inbound `X-Request-ID`), bound to the logging context and echoed in responses and errors |
| Structured logging | structlog; human-readable in dev, JSON-per-line on stdout in prod — same events, environment-selected renderer |
| Error contract | RFC 9457 problem details on every error path; internals logged in full, never leaked to clients |
| Health endpoints | `/healthz` (liveness — checks nothing external) and `/readyz` (readiness — bounded database probe, 503 when unreachable) |
| Persistence | PostgreSQL 16 + pgvector, async SQLAlchemy 2.0, Alembic migrations with deterministic constraint naming |
| Testing | pytest with fixtures, unit/integration split by marker; the unit suite runs with no database at all |
| Packaging | Multi-stage Docker image, non-root, healthchecked; Compose stack for local development |

### M1 — Ingestion: complete

| Capability | Detail |
|---|---|
| Registration API | `POST /api/v1/repos` validates the repository against GitHub, registers it, and queues its backfill **in one transaction** — a registered repo that nothing ingests is not a reachable state |
| GitHub adapter | Typed errors (not-found / auth / rate-limit), ETag conditional requests, rate-limit header awareness, and a log-archive download that races the ~60-second signed-URL expiry |
| Task queue | Postgres-backed, claimed with `FOR UPDATE SKIP LOCKED`; leases, exponential backoff with jitter, dead-lettering, and deferral distinct from failure |
| Worker | Graceful SIGTERM shutdown, three short transactions per task, `task_id` bound to the logging context |
| Scheduler | An in-worker loop that enqueues polls on a cadence; leader-elected across replicas by a transaction-scoped Postgres advisory lock, so a worker killed mid-sweep cannot wedge it |
| Run ingestion | Drives `pending → fetching → ready \| failed`; idempotent under at-least-once delivery; a 404 is terminal because GitHub discards run logs after 90 days and no amount of backoff brings them back |
| Object storage | An `ObjectStore` protocol with a local filesystem backend; writes are atomic (temp file, fsync, rename) because the worker is killed mid-download on every deploy |
| Archive reading | Parses GitHub's run-log zip, which contains both whole-job and per-step logs for the same job; bounded on entry count and on bytes **actually decompressed**, because the sizes declared in the zip header are written by whoever built the archive |
| Normalization | ANSI and control stripping, timestamp extraction into a structured field, `\r` progress-frame collapse — verified by property-based tests (Hypothesis) for idempotence and line-number preservation |
| Secret redaction | Versioned pattern library for GitHub, AWS, Slack, and Google credentials, JWTs, PEM key bodies, keyed assignments, and generic high-entropy strings — applied **before** anything derived is stored. Counts are reported; values never are. Commit SHAs and digests are exempt, because they are exactly what a diagnosis needs to point at |

**Verified against production.** Registering `astral-sh/ruff` discovered 50
failed runs and ingested them end to end: 1.39 MB archives expanding to 9.94 MB,
34 job logs each, ~40 job rows per run, no rate limiting.

That run also found a bug 141 passing tests had missed. GitHub *deletes*
characters that are illegal in a Windows filename when naming archive entries
rather than substituting them, so the job `benchmarks walltime
(colour_science|static_frame)` becomes `1_benchmarks walltime
(colour_sciencestatic_frame).txt`. Matching on a slug that treated the pipe as
a separator silently recorded zero log lines for 7% of jobs. The fix is now
tested against filenames taken verbatim from a real archive.

**Not yet implemented:** failure signatures, retrieval, LLM diagnosis, the web
UI, and deployment.

---

## Architecture

### Layers

```
api/       Transport: routes, dependencies, request/response schemas, errors, SSE
  ↓
core/      Domain: config, logging, ingestion, normalization, redaction,
           log archives, scheduler, worker, signatures, diagnosis
  ↓
adapters/  External systems: GitHub, object storage, task queue, LLM
  ↓
db/        Persistence: SQLAlchemy models, migrations, session factory
```

Imports point one direction only. Two rules are enforced in review:

- **`api` never touches `db` directly** — a route reaching into the database
  bypasses every domain rule in `core`. Registering a repository is not one
  insert; it validates against GitHub and enqueues a backfill in the same
  transaction, and a route doing its own `INSERT` would create a repository
  nobody ever ingests.
- **`core` never imports an SDK** — domain logic asks for "an LLM" or "somewhere
  to put bytes" through an adapter interface. This is what lets the S3 backend
  land in M5 without a domain module changing, and what keeps the entire test
  suite free of network calls.

### From a repository URL to stored evidence

1. `POST /api/v1/repos` validates the repository against GitHub, inserts it,
   and enqueues a `backfill_repo` task — one transaction, so both or neither.
   Registering twice returns `200` with the same repository rather than a
   conflict; a client retrying after a timeout shouldn't be punished for the
   first attempt having succeeded.
2. The worker claims the backfill, lists recent failed runs, records the ones
   it hasn't seen, and enqueues one `ingest_run` per new run.
3. `ingest_run` moves the run to `fetching` **and commits before the network
   calls**, so an in-flight run is visible and a crash is diagnosable. It then
   downloads the log archive, stores the raw bytes immutably, records every job
   with its failing step and line count, and marks the run `ready`.
4. The scheduler enqueues a poll for every active repository on a cadence, so
   new failures arrive without anyone asking.

Every step is idempotent, because at-least-once delivery makes double execution
a certainty rather than a risk.

### Why Postgres does five jobs

buildlens uses PostgreSQL as its relational store, vector index (pgvector),
full-text search engine, job queue (`FOR UPDATE SKIP LOCKED`), and distributed
lock (`pg_try_advisory_xact_lock`, for scheduler leader election). Dedicated
tools would each be better at their one job; one database is dramatically
easier to operate, back up, and reason about — and it allows enqueuing work in
the same transaction as the domain write that caused it. Adding a second
stateful service requires an ADR proving Postgres cannot do the job.

---

## API

Base path `/api/v1`. JSON bodies, `snake_case` fields, RFC 9457 problem details
on every error. Interactive docs at `/docs`.

| Endpoint | Description |
|---|---|
| `POST /api/v1/repos` | Register a public repository and queue its backfill. `201` on create, `200` if already registered |
| `GET /healthz` | Liveness — checks nothing external |
| `GET /readyz` | Readiness — bounded database probe, `503` when unreachable |

Domain errors are translated at the transport boundary: a repository that
doesn't exist is `404`, an exhausted GitHub budget is `503` rather than `429`
(the caller did nothing wrong), and a bad token falls through to an opaque
`500` — that's our misconfiguration, and the details belong in the server log,
not the response.

---

## Tech stack

**Backend** Python 3.12 · FastAPI · uvicorn · SQLAlchemy 2.0 (async) · asyncpg ·
Alembic · Pydantic · structlog
**Data** PostgreSQL 16 · pgvector
**Quality** pytest · Hypothesis · ruff · mypy · pre-commit · GitHub Actions
**Packaging** Docker (multi-stage) · Docker Compose · uv
**Planned** Anthropic API · React + TypeScript · Terraform · AWS (ECS, RDS, S3)

---

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/manojsaiannapareddy/buildlens.git
cd buildlens
cp backend/.env.example backend/.env
# add a GitHub token to backend/.env — a fine-grained PAT with
# read-only access to public repositories and no permissions is enough
make dev          # builds the image and starts the stack
make migrate      # applies database migrations
```

Register a repository and watch it ingest:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/repos \
  -H 'content-type: application/json' \
  -d '{"owner":"astral-sh","name":"ruff"}'
```

The worker picks up the backfill within seconds. Logs stream the whole pipeline:

```
task.started            task_type=backfill_repo
runs.recorded           seen=50 new=50
github.logs_downloaded  bytes=1386554
object_store.stored     key=repos/562c.../runs/a49b.../logs.zip
log_archive.read        jobs=34 bytes=9939193
run.ingested            jobs=40 archive_bytes=1386554
```

Interactive API docs: <http://127.0.0.1:8000/docs>

### Development

```bash
make check        # everything CI runs: lint, format, types, migrations, tests
make test-unit    # fast loop — no database required, ~1s
make fmt          # auto-format
make revision m="description"   # generate a migration
make db-reset     # destroy and rebuild the database
make down         # stop the stack (data preserved)
```

`make test-unit` runs the entire pure-function suite — normalization,
redaction, archive parsing, object storage — with no Postgres and no network.
CI runs it as a separate job with a deliberately unreachable database URL, so
it passes only if nothing tries to connect.

---

## Engineering decisions

Every non-obvious choice is recorded as an ADR in
[`docs/decisions.md`](docs/decisions.md), with context, alternatives rejected,
and honest consequences — including the downsides. An ADR that lists no costs
is propaganda.

| ADR | Decision |
|---|---|
| 001 | Monorepo with a modular-monolith backend |
| 002 | WSL2/Ubuntu development environment; GitHub as source of truth |
| 003 | uv with a committed lock file; `src/` layout |
| 004 | ruff + mypy with graded strictness |
| 005 | Postgres as the only stateful service |
| 006 | Async SQLAlchemy as the database access layer |
| 007 | Alembic for schema migrations, expand/contract discipline |
| 008 | Postgres task queue with leases and backoff |
| 009 | Object storage behind a protocol; local backend now, S3 in M5 |
| 010 | Log normalization and secret redaction before any derived write |

---

## Roadmap

- [x] **M0 — Foundations:** service skeleton, config, logging, errors, health, database, CI, containers
- [x] **M1 — Ingestion:** registration API, GitHub client, task queue, worker, scheduler, log normalization and secret redaction, object storage
- [ ] **M2 — Index & signatures:** chunking, embeddings, hybrid retrieval (BM25 + vector, RRF), failure fingerprinting
- [ ] **M3 — Diagnosis engine:** LLM gateway with spend caps, schema-constrained output, citation validation, SSE streaming
- [ ] **M4 — Web UI:** React + TypeScript, virtualized log viewer, streaming diagnosis panel with citation jumping
- [ ] **M5 — Production deploy:** Terraform, AWS ECS/RDS/S3, zero-downtime deploys, monitoring and alarms
- [ ] **M6 — Eval hardening:** golden dataset, judge calibration, quality regression gates in CI
- [ ] **M7 — Public demo:** load testing, backup/restore drill, documentation

## Project structure

```
backend/     FastAPI service, worker, models, migrations, tests
frontend/    React + TypeScript UI (M4)
evals/       Golden dataset and evaluation harness (M3/M6)
infra/       Terraform modules (M5)
ingest/      Recorded GitHub fixtures for offline testing
docs/        ADR log, architecture notes, runbook
```

## License

MIT
