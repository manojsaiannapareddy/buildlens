"""Integration tests for queue semantics (requires a live database)."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from buildlens.adapters import task_queue
from buildlens.db.models import TaskStatus, TaskType

pytestmark = pytest.mark.integration

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def test_claim_returns_highest_priority_task(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        await task_queue.enqueue(session, TaskType.POLL_REPO, {"n": 1}, priority=200)
        urgent = await task_queue.enqueue(session, TaskType.DIAGNOSE_RUN, {"n": 2}, priority=10)
        await session.commit()

        claimed = await task_queue.claim_next(session)
        assert claimed is not None
        assert claimed.id == urgent.id
        assert claimed.status == TaskStatus.LEASED
        assert claimed.attempts == 1
        await session.rollback()


async def test_concurrent_claims_do_not_collide(
    session_factory: SessionFactory,
) -> None:
    """SKIP LOCKED: a second worker takes a different task, it does not block."""
    async with session_factory() as setup:
        await task_queue.enqueue(setup, TaskType.INGEST_RUN, {"n": 1})
        await task_queue.enqueue(setup, TaskType.INGEST_RUN, {"n": 2})
        await setup.commit()

    async with session_factory() as worker_a, session_factory() as worker_b:
        first = await task_queue.claim_next(worker_a)
        second = await task_queue.claim_next(worker_b)

        assert first is not None and second is not None
        assert first.id != second.id


async def test_failure_dead_letters_after_max_attempts(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        task = await task_queue.enqueue(session, TaskType.GC_REPO, {})
        task.attempts = task.max_attempts
        await task_queue.mark_failed(session, task, "boom")

        assert task.status == TaskStatus.DEAD
        assert task.last_error == "boom"
        await session.rollback()
