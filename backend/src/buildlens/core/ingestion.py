"""Ingestion handlers: discover failed workflow runs and record them (FR-3, FR-8).

Handlers are idempotent by construction: runs are upserted with ON CONFLICT
DO NOTHING against the (repo, run, attempt) unique constraint, so a task
delivered twice produces the same state as a task delivered once.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from buildlens.adapters import task_queue
from buildlens.adapters.github import (
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
    JobInfo,
    RunInfo,
)
from buildlens.adapters.object_store import ObjectStore, make_object_store
from buildlens.core.config import get_settings
from buildlens.core.exceptions import TaskDeferred
from buildlens.core.log_archive import job_log_line_counts, slug
from buildlens.db.models import (
    IngestionTask,
    IngestState,
    Job,
    JobStatus,
    Repository,
    RepoStatus,
    RunConclusion,
    TaskType,
    WorkflowRun,
)

logger = structlog.get_logger()

BACKFILL_RUN_LIMIT = 50
POLL_RUN_LIMIT = 20


def make_client() -> GitHubClient:
    """Seam for tests: patch this to inject a fake client."""
    return GitHubClient(get_settings())


def make_store() -> ObjectStore:
    """Seam for tests: patch this to inject an in-memory store."""
    return make_object_store(get_settings())


def _run_conclusion(value: str) -> RunConclusion:
    """Coerce a GitHub conclusion into our enum.

    GitHub's vocabulary is longer than ours and grows without notice
    (`startup_failure`, `action_required`, `neutral`, `stale`). Unknown values
    become `other` rather than tripping the column's CHECK constraint at
    insert time.
    """
    try:
        return RunConclusion(value)
    except ValueError:
        logger.info("github.unknown_conclusion", value=value)
        return RunConclusion.OTHER


def _job_conclusion(value: str | None) -> RunConclusion | None:
    """A job still running has no conclusion yet; that is not an error."""
    return None if value is None else _run_conclusion(value)


def _job_status(value: str) -> JobStatus:
    try:
        return JobStatus(value)
    except ValueError:
        logger.info("github.unknown_job_status", value=value)
        return JobStatus.QUEUED


async def ensure_repository(
    session: AsyncSession, owner: str, name: str
) -> tuple[Repository, bool]:
    """Register a public repo (idempotent), and queue its backfill.

    Returns the repository and whether this call created it, so the caller can
    distinguish "created" from "already existed" without querying again.
    """
    existing = (
        await session.execute(
            select(Repository).where(Repository.owner == owner, Repository.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    async with make_client() as gh:
        info = await gh.get_repo(owner, name)

    repo = Repository(
        github_id=info.github_id,
        owner=info.owner,
        name=info.name,
        default_branch=info.default_branch,
        status=RepoStatus.ACTIVE,
    )
    session.add(repo)
    await session.flush()

    await task_queue.enqueue(
        session, TaskType.BACKFILL_REPO, {"repository_id": str(repo.id)}, priority=50
    )
    logger.info("repository.registered", repository_id=str(repo.id), owner=owner, name=name)
    return repo, True


async def _load_repo(session: AsyncSession, task: IngestionTask) -> Repository:
    repo_id = uuid.UUID(task.payload["repository_id"])
    repo = await session.get(Repository, repo_id)
    if repo is None:
        raise ValueError(f"Repository {repo_id} no longer exists")
    return repo


async def _record_runs(
    session: AsyncSession, repo: Repository, runs: list[RunInfo]
) -> list[uuid.UUID]:
    """Insert runs we haven't seen. Returns the ids of genuinely new rows."""
    if not runs:
        return []

    rows = [
        {
            "id": uuid.uuid4(),
            "repository_id": repo.id,
            "github_run_id": run.github_run_id,
            "run_attempt": run.run_attempt,
            "workflow_name": run.workflow_name,
            "event": run.event,
            "branch": run.branch,
            "head_sha": run.head_sha,
            "conclusion": _run_conclusion(run.conclusion),
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "ingest_state": IngestState.PENDING,
        }
        for run in runs
    ]

    stmt = (
        pg_insert(WorkflowRun)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["repository_id", "github_run_id", "run_attempt"])
        .returning(WorkflowRun.id)
    )
    new_ids = list((await session.execute(stmt)).scalars())

    for run_id in new_ids:
        await task_queue.enqueue(session, TaskType.INGEST_RUN, {"run_id": str(run_id)})

    logger.info("runs.recorded", seen=len(runs), new=len(new_ids), repository_id=str(repo.id))
    return new_ids


async def backfill_repo(session: AsyncSession, task: IngestionTask) -> None:
    """One-time historical sweep of a newly registered repository (FR-3)."""
    repo = await _load_repo(session, task)
    try:
        async with make_client() as gh:
            runs, etag = await gh.list_failed_runs(repo.owner, repo.name, limit=BACKFILL_RUN_LIMIT)
    except GitHubRateLimitError as exc:
        raise TaskDeferred(exc.reset_at) from exc
    except GitHubNotFoundError:
        repo.status = RepoStatus.PAUSED
        logger.warning("repository.unavailable", repository_id=str(repo.id))
        return

    await _record_runs(session, repo, runs)
    repo.runs_etag = etag
    repo.last_polled_at = datetime.now(UTC)
    repo.last_synced_run_at = max(
        (r.completed_at for r in runs if r.completed_at), default=repo.last_synced_run_at
    )


async def poll_repo(session: AsyncSession, task: IngestionTask) -> None:
    """Incremental check for new failures, using the stored ETag (FR-3)."""
    repo = await _load_repo(session, task)
    if repo.status is not RepoStatus.ACTIVE:
        logger.info("poll.skipped_inactive", repository_id=str(repo.id))
        return

    try:
        async with make_client() as gh:
            runs, etag = await gh.list_failed_runs(
                repo.owner, repo.name, limit=POLL_RUN_LIMIT, etag=repo.runs_etag
            )
    except GitHubRateLimitError as exc:
        raise TaskDeferred(exc.reset_at) from exc

    repo.last_polled_at = datetime.now(UTC)
    if not runs:
        logger.info("poll.unchanged", repository_id=str(repo.id))
        return

    await _record_runs(session, repo, runs)
    repo.runs_etag = etag
    repo.last_synced_run_at = max(
        (r.completed_at for r in runs if r.completed_at), default=repo.last_synced_run_at
    )


def _failing_step_name(steps: list[dict[str, Any]]) -> str | None:
    """The first step that failed — the anchor a signature will be cut from."""
    for step in steps:
        if step.get("conclusion") == "failure":
            name = step.get("name")
            return str(name)[:255] if name else None
    return None


def archive_key(run: WorkflowRun) -> str:
    """Storage key for a run's log archive.

    Keyed by our own UUIDs rather than owner/name so a repository rename does
    not orphan its blobs; the database remains the only index that matters.
    """
    return f"repos/{run.repository_id}/runs/{run.id}/logs.zip"


async def _record_jobs(
    session: AsyncSession,
    run: WorkflowRun,
    jobs: list[JobInfo],
    line_counts: dict[str, int],
) -> int:
    """Upsert this run's jobs. Re-ingestion overwrites rather than duplicates."""
    if not jobs:
        return 0

    rows = [
        {
            "id": uuid.uuid4(),
            "workflow_run_id": run.id,
            "github_job_id": job.github_job_id,
            "name": job.name[:255],
            "step_name": _failing_step_name(job.steps),
            "status": _job_status(job.status),
            "conclusion": _job_conclusion(job.conclusion),
            "steps": job.steps,
            "log_line_count": line_counts.get(slug(job.name), 0),
        }
        for job in jobs
    ]

    stmt = pg_insert(Job).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["workflow_run_id", "github_job_id"],
        set_={
            "name": stmt.excluded.name,
            "step_name": stmt.excluded.step_name,
            "status": stmt.excluded.status,
            "conclusion": stmt.excluded.conclusion,
            "steps": stmt.excluded.steps,
            "log_line_count": stmt.excluded.log_line_count,
            # onupdate= does not fire for an INSERT ... ON CONFLICT, so the
            # timestamp is set here or it silently never moves.
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    logger.info("jobs.recorded", run_id=str(run.id), jobs=len(rows))
    return len(rows)


async def ingest_run(session: AsyncSession, task: IngestionTask) -> None:
    """Fetch one failed run's jobs and log archive, and store both (FR-4, FR-8).

    Drives `ingest_state` pending → fetching → ready|failed. The `fetching`
    transition is committed before the network calls so an in-flight run is
    visible to readers and a crash is diagnosable; the handler re-enters from
    any non-ready state, so a run left `fetching` by a dead worker is simply
    redone when the lease expires.
    """
    run_id = uuid.UUID(task.payload["run_id"])
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise ValueError(f"Workflow run {run_id} no longer exists")

    if run.ingest_state is IngestState.READY:
        logger.info("run.ingest_skipped", run_id=str(run.id), reason="already_ready")
        return

    repo = await session.get(Repository, run.repository_id)
    if repo is None:
        raise ValueError(f"Repository {run.repository_id} no longer exists")

    run.ingest_state = IngestState.FETCHING
    await session.commit()

    try:
        async with make_client() as gh:
            jobs = await gh.list_jobs(repo.owner, repo.name, run.github_run_id)
            archive = await gh.download_run_logs(repo.owner, repo.name, run.github_run_id)
    except GitHubRateLimitError as exc:
        # Committed here because raising rolls the working transaction back,
        # which would otherwise strand the run in `fetching`.
        run.ingest_state = IngestState.PENDING
        await session.commit()
        raise TaskDeferred(exc.reset_at) from exc
    except GitHubNotFoundError:
        # GitHub discards run logs after 90 days, and deleted runs 404 too.
        # Neither is retryable, so this is a terminal state, not a failure.
        run.ingest_state = IngestState.FAILED
        logger.warning("run.logs_unavailable", run_id=str(run.id), github_run_id=run.github_run_id)
        return

    run.raw_log_uri = await make_store().put(archive_key(run), archive)
    await _record_jobs(session, run, jobs, job_log_line_counts(archive))
    run.ingest_state = IngestState.READY

    logger.info(
        "run.ingested",
        run_id=str(run.id),
        github_run_id=run.github_run_id,
        jobs=len(jobs),
        archive_bytes=len(archive),
    )
