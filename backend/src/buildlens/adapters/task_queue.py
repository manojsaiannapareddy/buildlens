"""Postgres-backed task queue (ADR-008).

Claims use FOR UPDATE SKIP LOCKED so concurrent workers never take the same
task. Claims are short transactions that set a lease; work happens after the
commit, and expired leases are recovered so crashed workers self-heal.
"""

import random
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from buildlens.db.models import IngestionTask, TaskStatus, TaskType

logger = structlog.get_logger()

LEASE_DURATION = timedelta(minutes=10)
BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 600


def _backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff with full jitter, to avoid synchronized retries."""
    ceiling = min(BACKOFF_BASE_SECONDS * (2**attempts), BACKOFF_CAP_SECONDS)
    return timedelta(seconds=random.uniform(0, ceiling))


async def enqueue(
    session: AsyncSession,
    task_type: TaskType,
    payload: dict[str, Any],
    *,
    priority: int = 100,
    delay: timedelta | None = None,
) -> IngestionTask:
    """Record intent to do work. Caller controls the transaction."""
    task = IngestionTask(
        task_type=task_type,
        payload=payload,
        priority=priority,
        next_run_at=datetime.now(UTC) + (delay or timedelta()),
    )
    session.add(task)
    await session.flush()
    return task


async def claim_next(session: AsyncSession) -> IngestionTask | None:
    """Atomically claim one due task, or return None if the queue is empty."""
    stmt = (
        select(IngestionTask)
        .where(
            IngestionTask.status == TaskStatus.PENDING,
            IngestionTask.next_run_at <= datetime.now(UTC),
        )
        .order_by(IngestionTask.priority.asc(), IngestionTask.next_run_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        return None

    task.status = TaskStatus.LEASED
    task.attempts += 1
    task.lease_expires_at = datetime.now(UTC) + LEASE_DURATION
    await session.flush()
    return task


async def mark_done(session: AsyncSession, task: IngestionTask) -> None:
    task.status = TaskStatus.DONE
    task.lease_expires_at = None
    await session.flush()


async def mark_failed(session: AsyncSession, task: IngestionTask, error: str) -> None:
    """Reschedule with backoff, or dead-letter once attempts are exhausted."""
    task.last_error = error[:2000]
    task.lease_expires_at = None
    if task.attempts >= task.max_attempts:
        task.status = TaskStatus.DEAD
        logger.error(
            "task.dead_lettered",
            task_id=str(task.id),
            task_type=task.task_type,
            attempts=task.attempts,
        )
    else:
        task.status = TaskStatus.PENDING
        task.next_run_at = datetime.now(UTC) + _backoff_delay(task.attempts)
    await session.flush()


async def recover_expired_leases(session: AsyncSession) -> int:
    """Return tasks abandoned by crashed workers to the pending pool."""
    stmt = (
        update(IngestionTask)
        .where(
            IngestionTask.status == TaskStatus.LEASED,
            IngestionTask.lease_expires_at < datetime.now(UTC),
        )
        .values(status=TaskStatus.PENDING, lease_expires_at=None)
    )
    result = await session.execute(stmt)
    recovered = cast(CursorResult[Any], result).rowcount or 0
    if recovered:
        logger.warning("task.leases_recovered", count=recovered)
    return recovered
