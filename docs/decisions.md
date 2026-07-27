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
