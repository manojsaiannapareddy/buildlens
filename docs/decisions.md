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