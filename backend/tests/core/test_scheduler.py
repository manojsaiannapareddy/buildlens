"""Scheduler behavior against a live database."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.adapters import task_queue
from buildlens.core import scheduler
from buildlens.db.models import IngestionTask, Repository, RepoStatus, TaskStatus, TaskType

pytestmark = pytest.mark.integration


async def _repo(
    session: AsyncSession,
    *,
    github_id: int = 1,
    name: str = "r",
    status: RepoStatus = RepoStatus.ACTIVE,
    last_polled_at: datetime | None = None,
) -> Repository:
    repo = Repository(
        github_id=github_id,
        owner="o",
        name=name,
        default_branch="main",
        status=status,
        last_polled_at=last_polled_at,
    )
    session.add(repo)
    await session.flush()
    return repo


async def _poll_tasks(session: AsyncSession) -> list[Any]:
    return list(
        (
            await session.execute(
                IngestionTask.__table__.select().where(
                    IngestionTask.task_type == TaskType.POLL_REPO
                )
            )
        ).all()
    )


async def test_never_polled_repo_is_scheduled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _repo(session)
        assert await scheduler.enqueue_due_polls(session) == 1
        await session.commit()

        assert len(await _poll_tasks(session)) == 1


async def test_recently_polled_repo_is_left_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _repo(session, last_polled_at=datetime.now(UTC) - timedelta(minutes=1))
        assert await scheduler.enqueue_due_polls(session) == 0


async def test_stale_repo_is_scheduled_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _repo(session, last_polled_at=datetime.now(UTC) - scheduler.POLL_INTERVAL * 2)
        assert await scheduler.enqueue_due_polls(session) == 1


@pytest.mark.parametrize("status", [RepoStatus.PAUSED, RepoStatus.DELETING])
async def test_inactive_repos_are_skipped(
    session_factory: async_sessionmaker[AsyncSession], status: RepoStatus
) -> None:
    async with session_factory() as session:
        await _repo(session, status=status)
        assert await scheduler.enqueue_due_polls(session) == 0


async def test_repo_with_an_unfinished_poll_is_not_queued_twice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicates would burn the GitHub budget on the same work."""
    async with session_factory() as session:
        repo = await _repo(session)
        await task_queue.enqueue(session, TaskType.POLL_REPO, {"repository_id": str(repo.id)})
        await session.commit()

        assert await scheduler.enqueue_due_polls(session) == 0


async def test_a_finished_poll_does_not_block_the_next_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = await _repo(session)
        task = await task_queue.enqueue(
            session, TaskType.POLL_REPO, {"repository_id": str(repo.id)}
        )
        task.status = TaskStatus.DONE
        await session.commit()

        assert await scheduler.enqueue_due_polls(session) == 1


async def test_scheduling_is_jittered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Enough repos, enough spread — otherwise the whole fleet polls at once."""
    async with session_factory() as session:
        for index in range(20):
            await _repo(session, github_id=index + 1, name=f"r{index}")
        await scheduler.enqueue_due_polls(session)
        await session.commit()

        scheduled_at = {row.next_run_at for row in await _poll_tasks(session)}

    assert len(scheduled_at) > 1
    assert max(scheduled_at) - min(scheduled_at) < scheduler.POLL_JITTER * 2


async def test_only_one_worker_schedules_a_round(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The advisory lock is what stops two workers double-queueing."""
    async with session_factory() as holder, session_factory() as contender:
        await _repo(session=contender)
        await contender.commit()

        # Held for the life of the holder's transaction, which never commits.
        acquired = (
            await holder.execute(
                select(func.pg_try_advisory_xact_lock(scheduler.SCHEDULER_LOCK_KEY))
            )
        ).scalar_one()
        assert acquired is True

        assert await scheduler.enqueue_due_polls(contender) == 0
