"""Ingestion handlers: discover failed workflow runs and record them (FR-3, FR-8).

Handlers are idempotent by construction: runs are upserted with ON CONFLICT
DO NOTHING against the (repo, run, attempt) unique constraint, so a task
delivered twice produces the same state as a task delivered once.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from buildlens.adapters import task_queue
from buildlens.adapters.github import (
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
    RunInfo,
)
from buildlens.core.config import get_settings
from buildlens.core.exceptions import TaskDeferred
from buildlens.db.models import (
    IngestionTask,
    IngestState,
    Repository,
    RepoStatus,
    TaskType,
    WorkflowRun,
)

logger = structlog.get_logger()

BACKFILL_RUN_LIMIT = 50
POLL_RUN_LIMIT = 20


def make_client() -> GitHubClient:
    """Seam for tests: patch this to inject a fake client."""
    return GitHubClient(get_settings())


async def ensure_repository(session: AsyncSession, owner: str, name: str) -> Repository:
    """Register a public repo (idempotent), and queue its backfill."""
    existing = (
        await session.execute(
            select(Repository).where(Repository.owner == owner, Repository.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

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
    return repo


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
            "conclusion": run.conclusion,
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
