# buildlens — Project State & Conventions

**Last updated:** August 14, 2026 · **Current position:** M1 complete, M2 not started
**Tests:** 148 passing (108 unit, 40 integration) · **Head:** `main`, all gates green

---

## 1. What buildlens is

An LLM-powered diagnostic service for CI failures. Point it at a public GitHub
repository; it ingests failed GitHub Actions runs, normalizes and redacts the logs,
fingerprints each failure into a **signature**, and produces streamed, evidence-cited
root-cause diagnoses for genuinely new failures.

**The core insight (leads every version of the pitch):** most red builds are *repeats*.
Fingerprinting recognizes them in milliseconds at zero cost, so the LLM is reserved for
novelty. The analogy: a veteran mechanic who recognizes the rattle versus a rookie who
re-diagnoses from scratch every time.

**The trust mechanism:** the model is constrained to a schema (category, analysis,
suggested fix, **evidence with specific line ranges**) and every citation is
*mechanically validated* against the real log before display. Hallucinated citations
fail validation rather than reaching a user.

**Pipeline:** Ingest → Normalize (strip ANSI, redact secrets, chunk with line mapping)
→ Index (full-text + vector) → Fingerprint (template + hash) → Diagnose (hybrid
retrieval + schema-constrained LLM) → Observe (SSE stream, cost ledger, request tracing).

---

## 2. Architecture

### Layers (imports point one direction only)

```
api/       Transport: routes, dependencies, request/response schemas, middleware, errors, SSE
  ↓
core/      Domain: config, logging, ingestion, normalization, redaction, log archives,
           scheduler, worker, signatures, diagnosis
  ↓
adapters/  External systems: GitHub, object storage, task queue, LLM
  ↓
db/        Persistence: models, migrations, session factory
```

Two prohibitions enforced in review:
- **`api` never touches `db` directly** — bypasses domain rules in `core`. Registering a
  repository is not one insert: it validates against GitHub and enqueues a backfill in
  the same transaction. This rule is why `ensure_repository` returns
  `tuple[Repository, bool]` — the route needs 201-vs-200 and may not query for it.
- **`core` never imports an SDK** — asks for "an LLM" or "somewhere to put bytes"
  through an adapter interface. This is what makes NFR-14 achievable and what lets the
  S3 backend land in M5 without a domain module changing.

`core/exceptions.py` and `core/config.py` are leaf modules: they import nothing from
buildlens, so anything may import them. That is why an adapter importing `Settings`
does not violate the layering.

### Processes
Two entrypoints, one Docker image: **api** (FastAPI/uvicorn) and **worker**. The worker
runs two concurrent loops in an `asyncio.TaskGroup` sharing one shutdown event: the task
drain loop (`core/worker.py`) and the poll scheduler (`core/scheduler.py`).

### Why Postgres does five jobs
Relational store + vector index (pgvector) + full-text search + job queue
(`FOR UPDATE SKIP LOCKED`) + distributed lock (`pg_try_advisory_xact_lock`, for
scheduler leader election). One stateful service is dramatically easier for one person
to operate, and it allows enqueuing work in the same transaction as the domain write
that caused it. Adding a second stateful service requires an ADR proving Postgres can't.

---

## 3. Repository layout

```
buildlens/
├── Makefile                  make dev/check/test/test-unit/migrate/revision/db-reset
├── docker-compose.yml        postgres + api + worker
├── README.md
├── .github/workflows/ci.yml  two jobs: unit (no database) + quality (full gates)
├── .pre-commit-config.yaml   ruff + file hygiene; unit tests on pre-push
├── backend/
│   ├── pyproject.toml        deps + ruff/mypy/pytest config
│   ├── uv.lock               committed
│   ├── Dockerfile            multi-stage, non-root uid 10001, healthcheck
│   ├── alembic/              env.py reads DB URL from Settings
│   ├── src/buildlens/
│   │   ├── api/              app.py, middleware.py, errors.py, deps.py, schemas.py,
│   │   │                     routers/repos.py
│   │   ├── core/             config.py, logging_setup.py, exceptions.py, worker.py,
│   │   │                     scheduler.py, ingestion.py, normalization.py,
│   │   │                     redaction.py, log_archive.py
│   │   ├── adapters/         github.py, task_queue.py, object_store.py
│   │   ├── db/               base.py, models.py, session.py
│   │   └── main_worker.py
│   ├── tests/                conftest.py, support.py, api/, adapters/, core/
│   └── var/raw-logs/         local object store (gitignored)
├── frontend/ evals/ infra/ ingest/    (reserved, M4/M6/M5)
└── docs/                     decisions.md (ADRs), project-state.md, learning-notes.md
```

---

## 4. What is built and working

**M0 — Foundations: COMPLETE**
- FastAPI app via application factory; uvicorn; lifespan-managed engine
- Twelve-factor config (pydantic-settings), `BUILDLENS_` prefix, crash-early on invalid
- Request-ID middleware bound to structlog contextvars (auto-propagates to all logs)
- RFC 9457 problem-details errors; verbose server-side, opaque client-side
- `/healthz` (liveness) + `/readyz` (readiness, bounded DB probe, 503)
- structlog: pretty in dev, JSON to stdout in prod
- Postgres 16 + pgvector in Docker; async SQLAlchemy 2.0 + asyncpg; Alembic
- pytest with unit/integration split by marker; the unit suite needs no database
- ruff + mypy + pre-commit + GitHub Actions CI (incl. `alembic check` drift gate)
- Multi-stage Dockerfile, Compose stack, Makefile

**M1 — Ingestion: COMPLETE**
- `POST /api/v1/repos` — validates against GitHub, registers, and queues the backfill in
  one transaction. 201 on create, 200 if already registered. 404 for a missing repo, 503
  for an exhausted GitHub budget (not 429 — the caller did nothing wrong), opaque 500 for
  a bad token (our misconfiguration)
- `adapters/github.py` — typed errors, ETag conditional requests, rate-limit header
  awareness, log-archive download racing the ~60s signed-URL expiry, Pydantic DTOs
- `adapters/task_queue.py` — `FOR UPDATE SKIP LOCKED` claiming, leases, exponential
  backoff with jitter, dead-lettering, `defer()` distinct from failure
- `adapters/object_store.py` — `ObjectStore` protocol + `LocalObjectStore` with atomic
  writes (temp file, fsync, rename) and traversal-rejecting keys
- `core/worker.py` — graceful SIGTERM shutdown, three short transactions per task,
  `task_id` bound to logging context, handler registry
- `core/scheduler.py` — enqueues `poll_repo` per active repo every 10 minutes with
  jitter; leader-elected by `pg_try_advisory_xact_lock`; skips repos already holding an
  unfinished poll
- `core/ingestion.py` — `ensure_repository`, `backfill_repo`, `poll_repo`, `ingest_run`.
  `ingest_run` drives `pending → fetching → ready|failed`, commits `fetching` before the
  network calls, commits back to `pending` before raising `TaskDeferred`, treats 404 as
  terminal (GitHub discards logs after 90 days)
- `core/log_archive.py` — pure functions over the run-log zip; whole-job files preferred
  over summed step files; bounded on entry count and on bytes actually decompressed
- `core/normalization.py` — decode with replacement → strip ANSI/C0 → lift timestamp →
  collapse `\r` frames → redact. Every line keeps its raw line number
- `core/redaction.py` — versioned pattern library (GitHub, AWS, Slack, Google, JWT, PEM
  bodies, keyed assignments, entropy sweep). Counts reported, values never. Commit SHAs
  exempt

**Verified against production.** `astral-sh/ruff`: 50 failed runs discovered, archives of
~1.4 MB expanding to ~9.9 MB, 34 job logs each, ~40 job rows per run. Only `skipped` jobs
report zero log lines, which is correct.

**Deferred to M5 (was listed under M1):** the S3 backend. The `ObjectStore` protocol
exists; building an S3 client before there is an AWS account, Terraform, or a bucket
means writing untestable code against imagined credentials.

---

## 5. ADRs on record (`docs/decisions.md`)

| # | Decision |
|---|---|
| 001 | Monorepo + modular monolith |
| 002 | WSL2/Ubuntu dev env, GitHub as source of truth, LF endings |
| 003 | uv + committed lock file + src layout + hatchling |
| 004 | ruff + mypy with graded strictness; CI verifies, never mutates |
| 005 | Postgres as the only stateful service |
| 006 | Async SQLAlchemy as the DB access layer |
| 007 | Alembic migrations, expand/contract discipline |
| 008 | Postgres task queue with leases and backoff |
| 009 | Object storage behind a protocol; local backend now, S3 in M5 |
| 010 | Polling scheduler inside the worker, advisory-lock leader election |
| 011 | Dedicated test database with a truncation guard |

Format: **Context → Decision → Alternatives considered → Consequences** (with honest
downsides — an ADR listing no costs is propaganda).

**Owed:** an ADR for the repos API shape (thin routers, explicit response schemas,
idempotent registration, domain-exception-to-status-code mapping). Not yet written.

---

## 6. Conventions

**Git:** Conventional Commits. Subject, **blank line**, body — `git commit -F-` from a
heredoc or `git commit` in an editor; a `-m` with no blank line makes the whole body the
subject. Before every commit: `git status --short` and `git diff --cached --stat`, and
check the file count against the step's file list. `git log origin/main..HEAD` empty
before considering work shipped.

**Logging:** `logger.info("noun.verb_past", key=value)` — dot-namespaced events, data as
kwargs, never prose. UTC always.

**Testing:** unit tests unmarked; integration tests `@pytest.mark.integration`. Unit
tests must never open a database connection — `make test-unit` runs in ~1.5s with
Postgres stopped, and CI enforces it with an unreachable DATABASE_URL. Tests run against
a `_test` sibling database, created and migrated automatically. Test names carry FR IDs
(`test_fr5_…`) so `pytest -k fr6` answers "is this requirement tested?" Every feature
ships with tests in the same commit.

**Migrations:** always review autogenerated output. Autogenerate reliably gets NOT NULL
columns wrong — expand/contract: add nullable → backfill → `alter_column` NOT NULL, and
the same *inside a downgrade* that re-adds a NOT NULL column. A constraint that only
applies cleanly to an empty table has not been tested: dedupe before adding a UNIQUE.
Test every downgrade immediately, **with a row present**.

**Enums:** `native_enum=False` (VARCHAR + CHECK). External vocabularies (GitHub
conclusions, job statuses) are coerced through a helper mapping unknowns to `other`,
because GitHub's vocabulary is wider than ours and grows without notice.

**Config:** required settings have no defaults (crash-early). Credentials use
`SecretStr`. `.env` gitignored; `.env.example` committed. Fine-grained GitHub PATs
expire in 30 days by default — set the expiry past the September deadline.

**Quality gates (`make check`):** ruff check → ruff format --check → mypy →
alembic check → pytest. Same five in CI, plus a separate unit job with no database.

---

## 7. Milestone plan and dates

| Milestone | Content | Original target | Status |
|---|---|---|---|
| M0 Foundations | ✅ complete | — | done |
| M1 Ingestion | ✅ complete | Aug 9 | done Aug 14 |
| M2 Index & signatures | chunking, embeddings, hybrid retrieval (BM25+vector, RRF), fingerprinting | Aug 16 | not started |
| M3 Diagnosis engine | LLM gateway (timeouts, spend caps, ledger), schema-constrained output, citation validation, SSE | Aug 27 | |
| M4 Web UI | React+TS, virtualized log viewer, streaming diagnosis panel, citation jump | Sep 5 | |
| M5 Production deploy | Terraform, AWS ECS/RDS/S3, zero-downtime deploys, monitoring | **Sep 7 — hard gate** | |
| M6 Eval hardening | golden set, judge calibration, quality regression gate in CI | post-applications | |
| M7 Public demo | load test, backup/restore drill, README GIF + diagram | post-applications | |

**Schedule reality check.** M1 ran five days over and M2's original target is two days
away, which is not happening. M2 and M3 are the two genuinely hard milestones, and M4
always takes longer than anyone estimates. **M5 is the gate that matters** — a deployed,
working system beats a feature-complete local one for recruiting. If time compresses,
cut scope inside M2–M4 rather than sliding M5: fewer retrieval strategies, a simpler UI,
fewer endpoints. M6/M7 land after applications go out.

---

## 8. Recurring failure modes to watch for (learned the hard way)

1. **Partial edits** — the single most common bug. A multi-file instruction gets applied
   to some files and not others. Worst case: a whole file is omitted, which produces *no
   error at all* because nothing imports it. The only detector is comparing the resulting
   test count against what the step should have produced.
2. **Partial commits** — the same bug with a delay. `git status --short` and
   `git diff --cached --stat` before committing; check `N files changed` against the
   step's file list.
3. **String keys are mypy's blind spot** — dict literals naming columns are unchecked.
   Same for `ON CONFLICT DO UPDATE`, where `onupdate=` does not fire and `updated_at`
   must be set by hand or it silently never moves.
4. **Green checks answer only their own question** — `alembic check` proves models and DB
   *agree*, never that either is complete. The `jobs` unique constraint was missing for
   three revisions while every gate stayed green.
5. **CI only catches what it runs** — a fixture that broke `make test-unit` for every
   unit test stayed green for a week because CI ran only the full suite. Every gate needs
   a job.
6. **A fast gate's exclusions are its permanent blind spot** — the unit job deselects
   integration tests, so a break there is invisible to it. Local-green plus CI-red points
   at integration first.
7. **Stale artifacts preserve broken state** — a ten-day-old Compose container held port
   8000 and ran an old worker against the live database. `docker compose ps` before
   debugging anything strange; a container's *age* is part of the environment.
8. **Test isolation decays** — a cleanup fixture written for one table silently stops
   covering the schema. Derive the table list from `Base.metadata`.
9. **Tests and development sharing a database is destructive** — `make check` truncated
   a full ingest of real data. Correct code, missing boundary.
10. **Tests pass against the inputs you imagined** — 141 tests passed while 7% of jobs
    recorded zero log lines, because no invented fixture contained a pipe in a matrix job
    name. Run against production data before declaring a milestone done.
11. **Environment vs. code failures** — `ConnectionRefusedError` on 5432 means Postgres
    isn't running. A 401 has at least three causes (wrong secret, mangled secret, wrong
    probe); test a credential in isolation before testing it through the app.

---

## 9. Where to resume

**M2 — Index & signatures.** In order:

1. **Chunker** (`core/chunking.py`) — consume `NormalizedLog.lines`, emit 60–120-line
   chunks carrying `start_line`/`end_line` taken straight from `NormalizedLine.number`.
   Pure functions, no I/O, tested with the property that every chunk's range maps back to
   real raw lines. This is the citation bridge M3 depends on.
2. **`log_chunks` model + migration** — content, line range, job FK, `tsvector` column,
   `vector` column, and the `normalizer_version`/`redaction_version` stamps.
3. **Embeddings adapter** — behind an interface, like the object store. No SDK in `core`.
4. **Hybrid retrieval** — BM25 (`ts_rank`) + vector cosine, fused with RRF.
5. **Fingerprinting** (`core/signatures.py`) — template the terminal error, hash it,
   store and match signatures.

**Develop against the real archives** in `backend/var/raw-logs/` — 50 `astral-sh/ruff`
runs with genuine ANSI noise, matrix job output, and pytest failures. Synthetic fixtures
are how M1's slug bug survived 141 passing tests.

To resume, say: *"Continue buildlens from docs/project-state.md — next step is the M2
chunker."*
