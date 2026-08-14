# Architecture Decision Records — buildlens

Format per entry: Context → Decision → Alternatives considered → Consequences.
ADRs are append-only. A reversed decision gets a new ADR that supersedes the old
one; we never rewrite history.

---

## ADR-001: Monorepo with a modular-monolith backend

**Date:** 2026-07-10 · **Status:** Accepted

**Context.** buildlens has several moving parts (HTTP API, background worker,
frontend, eval harness, infrastructure code) built and operated by one person.
Each part could live in its own repository, and the backend could be split into
services (ingestion service, diagnosis service, etc.).

**Decision.** One repository containing every subsystem as a top-level
directory (`backend/`, `frontend/`, `evals/`, `infra/`, `ingest/`, `docs/`).
The backend is a modular monolith: one Python codebase, one deployable image,
multiple entrypoints (api, worker) — module boundaries enforced by import
rules, not by network boundaries.

**Alternatives considered.**
- *Polyrepo:* rejected — cross-cutting changes (e.g., an API change touching
  backend + frontend + evals) would need coordinated PRs across repos; for a
  team of one this is pure overhead.
- *Microservices:* rejected — network boundaries add serialization, deployment,
  and observability costs and pay off only when independent teams need
  independent deploy cadences. We have one team member. The async boundary we
  genuinely need (API vs. background work) is provided by a task queue, not by
  separate services.

**Consequences.**
- (+) Atomic commits across subsystems; one CI pipeline; one issue tracker.
- (+) Refactoring across module boundaries is an IDE operation, not a
  cross-service migration.
- (−) CI must be configured to avoid running every job on every change
  (path filters) or pipelines get slow.
- (−) Module boundaries are enforced only by discipline and lint rules; a
  monolith lets you cheat in ways separate services physically prevent.
  Mitigation: layering rules (api → core → adapters → db) checked in review.

---

## ADR-002: Development environment — WSL2 (Ubuntu) on Windows

**Date:** 2026-07-10 · **Status:** Accepted

**Context.** Development machine runs Windows. The project's tooling (make,
Docker, shell-based CI, deployment to Linux servers) assumes a Unix
environment. Early commands already failed under PowerShell, and the
repository lived inside a OneDrive-synced folder — a known source of Git
index corruption.

**Decision.** Develop inside WSL2 running Ubuntu. The repository lives in the
Linux filesystem (~/projects/buildlens), cloned fresh from GitHub. VS Code
attaches via the WSL extension. The OneDrive copy is deleted; GitHub is the
sole source of truth. Line endings normalized to LF (core.autocrlf=input).

**Alternatives considered.**
- *Native Windows + PowerShell:* rejected — permanent command-translation tax,
  no native make, worse Docker ergonomics, dev/prod mismatch.
- *Git Bash:* rejected — surface-level fix; no make, no real Linux userland.
- *Full Linux dual-boot/VM:* rejected — heavier than needed; WSL2 gives ~95%
  of the benefit with none of the machine-management cost.

**Consequences.**
- (+) Dev environment matches production Linux (matters at deploy, M5).
- (+) Project tooling (Makefile, Compose, CI scripts) works as written.
- (−) One more system to learn (Linux) on top of the project itself.
- (−) Two-world complexity: files in /mnt/c vs ~; discipline required to keep
  projects on the Linux side.

## ADR-003: Dependency management with uv, a lock file, and the src layout

**Date:** 2026-07-11 · **Status:** Accepted

**Context.** The backend will depend on outside libraries (FastAPI, SQLAlchemy,
etc.). Those libraries must install to the exact same versions in three places:
my laptop, the Docker image, and CI. If versions drift between those places,
we get "works on my machine" bugs. Python's default tool (pip alone, with a
hand-written requirements.txt) does not guarantee this.

**Decision.**
- Use **uv** to create the virtual environment, resolve dependencies, and
  maintain a lock file (`uv.lock`), which is committed to Git.
- Declare only *direct* dependencies in `pyproject.toml`; the lock file
  records the exact version and hash of *everything*, including
  dependencies-of-dependencies.
- Put the package code in `backend/src/buildlens/` (the "src layout") so tests
  and tools always import the installed package, never a same-named folder
  that happens to be nearby.
- Use **hatchling** as the build backend (the standard, boring choice).

**Alternatives considered.**
- *pip + requirements.txt:* rejected — no lock discipline, no hash checking,
  transitive versions drift over time.
- *pip-tools:* workable, but a manual two-step (edit .in, compile .txt) that
  uv does in one.
- *Poetry:* does everything but is heavier and slower, and the ecosystem's
  momentum has moved to uv.

**Consequences.**
- (+) One fast tool handles venv + resolution + locking.
- (+) Dev, Docker, and CI install byte-identical environments from uv.lock.
- (−) uv is newer, so many tutorials online show pip/Poetry commands that
  don't match our workflow.
- (−) Anyone cloning the repo must install uv first.
- Incident note: first sync built an empty package because the source file
  didn't exist yet, and the cache preserved the broken build. Diagnosed by
  inspecting site-packages; fixed with `uv sync --reinstall-package buildlens`.

## ADR-004L Code quality toolchain: ruff and mypy with graded strictness.

**Date:** 2026-07-25 . **Status:** Accepted

**Context.** Implemented ruff and mypy in the project to check formating and
type errors accross the files while fixing them instantly with no complications.

**Decision** — ruff for lint+format (one tool replacing black/flake8/isort/pyupgrade,
config in pyproject), mypy with graded strictness (baseline everywhere, stricter in
core/adapters, relaxed in tests).

**Consequences** —
 - (+) style debates ended, whole classes of bugs caught pre-runtime, refactors safer;
 - (−) annotation ceremony on every new function, occasional fights with library types,
   another gate that can block a commit when you're in a hurry.

## ADR-005 Postgres as the only stateful service.

**Date:** 2026-07-25. **Status:** Accepted

**Context** BuildLens requires relational data storage, vector embeddings search
(for AI/RAG), full-text search, and background job queues. The standard approach
uses four separate specialized services (e.g., PostgreSQL + Pinecone/Qdrant +
Elasticsearch + Redis). However, operating multiple stateful databases in early
development adds unnecessary operational overhead, infrastructure complexity,
and higher hosting costs.

### Decision
We will use PostgreSQL (via the `pgvector/pgvector:pg16` Docker image in development
and AWS RDS in production) as the single stateful database for all storage, vector search,
text retrieval, and queue management.

### Alternatives Considered
* **Dedicated specialized stores (e.g., Redis + Qdrant + Postgres):** Offers slightly
higher performance at scale for each concern, but introduces four separate data pipelines
to manage, back up, and monitor.

### Consequences
* **Positive (+):** Single backup strategy, transactional enqueueing (writing domain data and background jobs in one transaction), and reduced infrastructure setup.
* **Negative (-):** Postgres is "good enough" for vector search and queues rather than best-in-class; we may hit scale ceilings if traffic grows dramatically.

## ADR-006: Async SQLAlchemy for Database Access

**Date:** 2026-07-26
**Status:** Accepted

### Context
BuildLens requires a database access layer to query PostgreSQL safely, efficiently, and with strong Python type safety across both the FastAPI web server and background workers.

Because FastAPI is an asynchronous ASGI application, database queries are I/O operations that must be non-blocking. Synchronous database drivers would block the Python event loop, freezing all concurrent requests while waiting on database network calls.

### Decision
We will use **SQLAlchemy 2.0 (Async)** with the **`asyncpg`** driver for all database communication.

Key implementation choices:
1. **Engine & Connection Pooling:** A long-lived `AsyncEngine` will manage connection pooling globally per application process using `pool_pre_ping=True` to detect stale connections before reuse.
2. **Session Lifecycle:** Short-lived unit-of-work sessions will be created using `async_sessionmaker`.
3. **Explicit Behavior Flags:**
   * `expire_on_commit=False`: Prevents post-commit attributes from triggering hidden, non-awaited lazy queries (a common async footgun).
   * `autoflush=False`: Ensures no implicit database flushes occur prior to explicit commits.

### Alternatives Considered
* **Raw `asyncpg`:** Extremely fast, but lacks an ORM, schema migration tooling (like Alembic), and parameter-binding abstractions.
* **Sync SQLAlchemy:** Simpler to write, but blocks the FastAPI event loop under concurrent load.
* **Lighter ORMs (Tortoise ORM / Piccolo):** Smaller footprint, but less mature migration tooling and a smaller ecosystem compared to SQLAlchemy.

### Consequences
* **Positive (+):**
  * Fully asynchronous non-blocking I/O keeps API endpoints responsive.
  * Native parameter binding provides robust protection against SQL injection.
  * Seamless integration with Alembic for future database schema migrations.
  * Easy fallback to raw SQL queries (`text()`) when needed.
* **Negative (-):**
  * Asynchronous ORM patterns have a steeper learning curve than synchronous code.
  * Requires careful model design to avoid costly or inefficient N+1 query patterns.

## ADR-007: Alembic Migrations with Expand/Contract Discipline

**Date:** 2026-07-28
**Status:** Accepted

### Context
The database schema will change for the life of the project, and the schema lives in two places at once: SQLAlchemy models in Python, and the actual tables in Postgres. Those two must stay in agreement across a laptop, CI, and production, and every change must be reproducible in the same order everywhere.

SQLAlchemy's `create_all()` can create tables that do not exist yet, but it can never alter one that does. It is a bootstrap tool, not a schema-evolution tool.

### Decision
We will use **Alembic** for all schema changes. No table, column, index, or constraint is created or altered by any other means.

Key implementation choices:
1. **Config from Settings:** `alembic/env.py` builds the database URL from the application `Settings` object rather than a hardcoded string in `alembic.ini`, so migrations run against whatever environment invoked them.
2. **Naming convention on the metadata:** `Base.metadata` carries an explicit naming convention (`ix_`, `uq_`, `ck_`, `fk_`, `pk_`), so autogenerate emits stable, predictable constraint names instead of Postgres defaults that cannot be referenced later.
3. **Autogenerate is reviewed, never trusted.** It reliably gets NOT NULL columns and type changes wrong. Every generated file is read and edited before it is committed.
4. **Expand/contract for column changes:** add nullable → backfill → `alter_column` to NOT NULL. A constraint that only applies cleanly to an empty table has not been tested; deduplicate before adding a UNIQUE.
5. **Downgrades are tested immediately,** while the intent is still fresh, not at the moment they are needed in production.
6. **`alembic check` runs in CI** as a drift gate, failing the build when models and migrations disagree.

### Alternatives Considered
* **`Base.metadata.create_all()`:** Rejected — creates but never alters, and keeps no history, so there is no path from an existing production database to a new schema.
* **Hand-written SQL files plus a runner:** Correct and fully explicit, but gives up autogenerate entirely and puts the whole diff-detection burden on a human every time.
* **Atlas / Sqitch:** Capable standalone tools, but they sit outside the Python ecosystem and know nothing about the ORM models, which is precisely what makes autogenerate and `alembic check` useful here.

### Consequences
* **Positive (+):**
  * Schema history is versioned, ordered, and reproducible in every environment.
  * CI catches model/schema drift the moment it appears, not at deploy.
  * Expand/contract makes column changes survivable on a live table.
* **Negative (−):**
  * Autogenerated output requires review on every revision, which is an easy step to skip when in a hurry.
  * `alembic check` proves models and database *agree*; it never proves either one is *correct*. The `jobs` unique constraint was missing for three revisions while every gate stayed green.
  * Revisions form a linear chain, so parallel work needs merge revisions to reconcile.

## ADR-008: Postgres Task Queue with Leases and Backoff

**Date:** 2026-07-30
**Status:** Accepted

### Context
Ingestion and diagnosis are slow, rate-limited, and failure-prone. They cannot run inside an HTTP request: a GitHub log archive download can take a minute, and a rate-limit response means "come back in twenty minutes," not "return a 500."

That requires a durable work queue with at-least-once delivery, retries, and a way to stop retrying. ADR-005 commits us to Postgres as the only stateful service, so introducing a broker needs to clear a high bar.

### Decision
We will implement the queue as an **`ingestion_tasks` table in Postgres**, claimed with `SELECT ... FOR UPDATE SKIP LOCKED`.

Key implementation choices:
1. **Claiming:** One task at a time, in a short transaction that sets a lease and commits. `SKIP LOCKED` means concurrent workers step over each other's rows instead of blocking on them.
2. **Work happens after the claim commits,** in its own transaction, so a slow handler never holds a row lock or a pooled connection.
3. **Leases, not locks:** a claimed task carries an expiry. A sweep returns expired tasks to the pending pool, so a worker that is killed mid-task self-heals rather than orphaning work.
4. **Failure policy:** an attempts counter, exponential backoff with full jitter, and dead-lettering once `max_attempts` is exhausted. Poison tasks stop; they never retry forever.
5. **Deferral is distinct from failure:** a rate limit reschedules the task at the reset time and gives the attempt back. "Not ready yet" must not consume a retry budget meant for "broken."
6. **Partial index** on `(priority, next_run_at) WHERE status = 'pending'` keeps the claim query reading only claimable rows.
7. **Handlers must be idempotent,** because at-least-once delivery guarantees that some task runs twice.

### Alternatives Considered
* **Celery + Redis / RQ:** The default answer, and rejected on ADR-005 grounds — a second stateful service to run, back up, and monitor, in exchange for throughput we do not need at NFR-6's design point.
* **AWS SQS:** Managed and durable, but it cannot participate in a Postgres transaction, so enqueue and the domain write that caused it could no longer succeed or fail together. It also drags a cloud dependency into local development.
* **`LISTEN`/`NOTIFY` alone:** A fine notification mechanism and a bad queue — payloads are not durable, and anything published while a worker is disconnected is simply gone.
* **Cron:** No retries, no leases, no visibility into what is in flight or why it failed.

### Consequences
* **Positive (+):**
  * A task is enqueued in the same transaction as the domain write that created the need for it, so there are no phantom jobs referencing rows that were rolled back, and no lost jobs after a successful write.
  * One service to operate, one backup, one connection pool.
  * Queue state is inspectable with ordinary SQL — `SELECT` against `ingestion_tasks` answers "what is stuck and why."
* **Negative (−):**
  * Workers poll, so an idle system still spends one query per tick. Mitigated with a jittered idle sleep.
  * At-least-once delivery pushes real design work into every handler: idempotency is now a correctness requirement, not a nicety.
  * A table-based queue tops out well below a dedicated broker. Acceptable at this scale, and the ceiling is a documented trade-off rather than a surprise.
  * Dead-lettered tasks need a human, which means each one needs a runbook entry before the alarm that reports it is allowed to exist.

## ADR-009: ObjectStore protocol for raw logs

**Context**
TC-4 requires raw logs stored immutably outside the database, keyed by run. Production is S3 (ADR-005 keeps Postgres as the only stateful service, and object storage is a separate concern). NFR-14 requires tests that touch no network.

**Decision**
Implement an `ObjectStore` protocol in `adapters/`, with a local filesystem backend now and S3 in milestone M5, selected by a factory reading `Settings`.

**Alternatives considered**
*   **S3 from day one via moto or LocalStack:** Provides real semantics in tests, but introduces a heavy dependency and a slower test suite for a milestone that only needs "put bytes, get bytes."
*   **MinIO in Compose:** Provides a real S3 API, but requires a second stateful service to operate — the exact thing ADR-005 exists to avoid.
*   **Bytes in a Postgres `bytea` column:** Transactional, but multi-megabyte blobs bloat the table, break `pg_dump` ergonomics, and fight the vector index for the buffer cache.

**Consequences**
*   The backend that runs in tests is not the backend that runs in production, so M5 owes at least one integration test against a real bucket.
*   `file://` URIs stored in a dev database are machine-local and meaningless elsewhere.
*   `LocalObjectStore` is code that never runs in production.
*   The protocol only guarantees the *shape* matches, not that S3's consistency and size semantics behave like a filesystem.

## ADR-010: Polling scheduler in the worker process

**Context**
FR-3 requires cadence-based polling. Workers scale horizontally, so every replica would run the same sweep. GitHub allows 5,000 requests/hour and ETags make unchanged polls free.

**Decision**
Implement a loop inside the worker process, leader-elected per sweep via `pg_try_advisory_xact_lock`, using a uniform 10-minute cadence with jitter, skipping repos with an unfinished poll task.

**Alternatives considered**
*   **Separate scheduler process:** Another deploy unit and health check for one loop.
*   **Leader-election table with a lease:** Works, but introduces a table to maintain and a lease to expire (the advisory lock does this for free).
*   **Cron or cloud scheduler:** External state, no local dev story, and breaks the one-image model.
*   **Adaptive per-repo intervals:** Optimizes a cost ETags already make near-zero, while adding state and a new failure mode.
*   **Webhooks:** Lower latency, but requires a public endpoint and secret management (an M5+ concern).

**Consequences**
*   Worst-case 10-minute detection latency.
*   The advisory lock key is global to the database, so any future lock must not collide.
*   A persistently failing tick is quiet by design and needs an alert in M5.
*   The scheduler shares a process with the drain loop, so a runaway sweep would compete for the same connection pool.
