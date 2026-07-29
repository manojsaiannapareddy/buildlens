# buildlens

[![CI](https://github.com/manojsaiannapareddy/buildlens/actions/workflows/ci.yml/badge.svg)](https://github.com/manojsaiannapareddy/buildlens/actions/workflows/ci.yml)

**AI-powered root-cause diagnosis for CI failures** — reads your GitHub Actions
logs, recognizes repeat failures instantly, and explains new ones with citations
to the exact log lines.

> **Status: in active development.** Foundations (M0) are complete and the
> service runs. Ingestion (M1) is in progress. See
> [Current status](#current-status) for exactly what works today.

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

| Stage | What happens |
|---|---|
| **Ingest** | Poll GitHub for failed workflow runs; download logs before their URLs expire |
| **Normalize** | Strip ANSI codes and timestamps, redact secrets, chunk logs while preserving original line numbers |
| **Index** | Store chunks with both full-text (`tsvector`) and vector (`pgvector`) representations |
| **Fingerprint** | Template the terminal error — replacing timestamps, IDs, paths, and durations with placeholders — then hash it into a **failure signature** |
| **Diagnose** | For unrecognized failures only: hybrid retrieval over the repo's own history, then a schema-constrained LLM call whose citations are mechanically validated |
| **Observe** | Stream results over SSE; meter every token, dollar, and millisecond; trace everything by request ID |

Two design choices carry the product:

**Fingerprint before you think.** Normalization turns
`Test failed at 14:32:07 in /tmp/build-a8f3/test_auth.py:41 after 2.3s` into
`Test failed at <TIME> in <PATH>:<NUM> after <DURATION>`, which hashes
identically across runs. "Have I seen this?" becomes a hash lookup: milliseconds,
zero cost. The LLM is reserved for novelty.

**Constrain and verify, don't trust.** The model must return a fixed schema —
category, analysis, suggested fix, and **evidence with specific line ranges** —
and every citation is checked against the real log before the diagnosis is
shown. A hallucinated citation fails validation rather than reaching a user.

---

## Current status

**M0 — Foundations: complete.** The service runs, is containerized, and is
verified by CI on every push.

| Capability | Detail |
|---|---|
| HTTP service | FastAPI behind uvicorn, built via an application factory |
| Configuration | Twelve-factor: env vars parsed and validated by Pydantic at startup; the process **refuses to boot** on invalid config |
| Request tracing | Every request gets a UUID (or honors inbound `X-Request-ID`), bound to the logging context and echoed in responses and errors |
| Structured logging | structlog; human-readable in dev, JSON-per-line on stdout in prod — same events, environment-selected renderer |
| Error contract | RFC 9457 problem details on every error path; internals logged in full, never leaked to clients |
| Health endpoints | `/healthz` (liveness — checks nothing external) and `/readyz` (readiness — bounded database probe, 503 when unreachable) |
| Persistence | PostgreSQL 16 + pgvector, async SQLAlchemy 2.0, Alembic migrations with deterministic constraint naming |
| Testing | pytest with fixtures, unit/integration split by marker, in-process ASGI client |
| Quality gates | ruff (lint + format), mypy (graded strictness), pytest — enforced by pre-commit hooks locally and CI on every push |
| Packaging | Multi-stage Docker image, non-root, healthchecked; Compose stack for local development |

**Not yet implemented:** GitHub ingestion, log normalization and redaction,
failure signatures, retrieval, LLM diagnosis, the web UI, and deployment.

---

## Architecture

### Layers

```
api/       Transport: HTTP routes, middleware, error handling, SSE
  ↓
core/      Domain: configuration, logging, ingestion, signatures, diagnosis
  ↓
adapters/  External systems: GitHub, LLM, object storage, database access
  ↓
db/        Persistence: SQLAlchemy models, migrations, session factory
```

Imports point one direction only. Two rules are enforced in review:

- **`api` never touches `db` directly** — a route reaching into the database
  bypasses every domain rule in `core`.
- **`core` never imports an SDK** — domain logic asks for "an LLM" through an
  adapter interface, which is what makes it testable without network calls.

### Anatomy of a request

1. Middleware assigns a request ID and binds it to the logging context —
   every subsequent log line carries it automatically, with no plumbing.
2. The route handler runs; domain logic executes in `core`.
3. On success, FastAPI serializes the response; the request ID is echoed in
   the headers.
4. On failure, an exception handler converts the error into an RFC 9457
   problem document carrying the same ID — so a screenshot from a user is a
   grep key into the server logs.

### Why Postgres does four jobs

buildlens uses PostgreSQL as its relational store, vector index (pgvector),
full-text search engine, and job queue (`FOR UPDATE SKIP LOCKED`). Dedicated
tools would each be better at their one job; one database is dramatically
easier to operate, back up, and reason about — and it allows enqueuing work in
the same transaction as the domain write that caused it. Adding a second
stateful service requires an ADR proving Postgres cannot do the job.

---

## Tech stack

**Backend** Python 3.12 · FastAPI · uvicorn · SQLAlchemy 2.0 (async) · asyncpg ·
Alembic · Pydantic · structlog
**Data** PostgreSQL 16 · pgvector
**Quality** pytest · ruff · mypy · pre-commit · GitHub Actions
**Packaging** Docker (multi-stage) · Docker Compose · uv
**Planned** Anthropic API · React + TypeScript · Terraform · AWS (ECS, RDS, S3)

---

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/manojsaiannapareddy/buildlens.git
cd buildlens
cp backend/.env.example backend/.env
make dev          # builds the image and starts the stack
make migrate      # applies database migrations

curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

Interactive API docs: <http://127.0.0.1:8000/docs>

### Development

```bash
make check        # everything CI runs: lint, types, tests
make test-unit    # fast loop, no database required
make fmt          # auto-format
make revision m="description"   # generate a migration
make db-reset     # destroy and rebuild the database
make down         # stop the stack (data preserved)
```

---

## Engineering decisions

Every non-obvious choice is recorded as an ADR in
[`docs/decisions.md`](docs/decisions.md), with context, alternatives rejected,
and honest consequences — including the downsides.

| ADR | Decision |
|---|---|
| 001 | Monorepo with a modular-monolith backend |
| 002 | WSL2/Ubuntu development environment; GitHub as source of truth |
| 003 | uv with a committed lock file; `src/` layout |
| 004 | ruff + mypy with graded strictness |
| 005 | Postgres as the only stateful service |
| 006 | Async SQLAlchemy as the database access layer |
| 007 | Alembic for schema migrations, expand/contract discipline |

---

## Roadmap

- [x] **M0 — Foundations:** service skeleton, config, logging, errors, health, database, CI, containers
- [ ] **M1 — Ingestion:** GitHub client, task queue, log normalization and secret redaction, backfill and polling
- [ ] **M2 — Index & signatures:** chunking, embeddings, hybrid retrieval, failure fingerprinting
- [ ] **M3 — Diagnosis engine:** LLM gateway with spend caps, schema-constrained output, citation validation, SSE streaming
- [ ] **M4 — Web UI:** React + TypeScript, virtualized log viewer, streaming diagnosis panel with citation jumping
- [ ] **M5 — Production deploy:** Terraform, AWS ECS/RDS, zero-downtime deploys, monitoring and alarms
- [ ] **M6 — Eval hardening:** golden dataset, judge calibration, quality regression gates in CI
- [ ] **M7 — Public demo:** load testing, backup/restore drill, documentation

## Project structure

```
backend/     FastAPI service, worker, models, migrations, tests
frontend/    React + TypeScript UI (M4)
evals/       Golden dataset and evaluation harness (M3/M6)
infra/       Terraform modules (M5)
ingest/      Recorded GitHub fixtures for offline testing (M1)
docs/        ADR log, architecture notes, runbook
```

## License

MIT
