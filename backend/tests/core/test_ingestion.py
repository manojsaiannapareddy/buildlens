"""Ingestion handler behavior with a stubbed GitHub client (requires a live database)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.adapters.github import GitHubRateLimitError, RunInfo
from buildlens.core import ingestion
from buildlens.core.worker import TaskDeferred
from buildlens.db.models import IngestionTask, Repository, RepoStatus, TaskType, WorkflowRun

pytestmark = pytest.mark.integration


def _run(run_id: int) -> RunInfo:
    return RunInfo(
        github_run_id=run_id,
        run_attempt=1,
        workflow_name="CI",
        event="push",
        branch="main",
        head_sha="a" * 40,
        conclusion="failure",
        started_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
    )


def _stub_client(
    monkeypatch: MonkeyPatch,
    *,
    runs: list[RunInfo] | None = None,
    etag: str = 'W/"e1"',
    error: Exception | None = None,
) -> None:
    class Stub:
        async def list_failed_runs(
            self,
            owner: str,
            name: str,
            *,
            limit: int = 50,
            etag: str | None = None,
        ) -> tuple[list[RunInfo], str]:
            if error:
                raise error
            return runs or [], etag or 'W/"e1"'

    @asynccontextmanager
    async def factory() -> AsyncIterator[Stub]:
        yield Stub()

    monkeypatch.setattr(ingestion, "make_client", factory)


async def _repo(session: AsyncSession) -> Repository:
    repo = Repository(
        github_id=1, owner="o", name="r", default_branch="main", status=RepoStatus.ACTIVE
    )
    session.add(repo)
    await session.flush()
    return repo


async def test_backfill_records_runs_and_queues_ingestion(
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: Any,
    monkeypatch: MonkeyPatch,
) -> None:
    _stub_client(monkeypatch, runs=[_run(1), _run(2)])
    async with session_factory() as session:
        repo = await _repo(session)
        task = IngestionTask(
            task_type=TaskType.BACKFILL_REPO, payload={"repository_id": str(repo.id)}
        )
        await ingestion.backfill_repo(session, task)
        await session.commit()

        runs = (
            await session.execute(
                WorkflowRun.__table__.select().where(WorkflowRun.repository_id == repo.id)
            )
        ).all()
        tasks = (
            await session.execute(
                IngestionTask.__table__.select().where(
                    IngestionTask.task_type == TaskType.INGEST_RUN
                )
            )
        ).all()

        assert len(runs) == 2
        assert len(tasks) == 2


async def test_backfill_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: Any,
    monkeypatch: MonkeyPatch,
) -> None:
    """The unique constraint, not application logic, guarantees this."""
    _stub_client(monkeypatch, runs=[_run(1)])
    async with session_factory() as session:
        repo = await _repo(session)
        task = IngestionTask(
            task_type=TaskType.BACKFILL_REPO, payload={"repository_id": str(repo.id)}
        )
        await ingestion.backfill_repo(session, task)
        await ingestion.backfill_repo(session, task)
        await session.commit()

        runs = (
            await session.execute(
                WorkflowRun.__table__.select().where(WorkflowRun.repository_id == repo.id)
            )
        ).all()

        assert len(runs) == 1


async def test_rate_limit_defers_instead_of_failing(
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: Any,
    monkeypatch: MonkeyPatch,
) -> None:
    reset = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
    _stub_client(monkeypatch, error=GitHubRateLimitError(reset))
    async with session_factory() as session:
        repo = await _repo(session)
        task = IngestionTask(task_type=TaskType.POLL_REPO, payload={"repository_id": str(repo.id)})
        with pytest.raises(TaskDeferred) as excinfo:
            await ingestion.poll_repo(session, task)

        assert excinfo.value.until == reset
