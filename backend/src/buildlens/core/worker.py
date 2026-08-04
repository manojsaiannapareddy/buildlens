"""Background worker loop (spec §7.1).

Claims tasks one at a time, each in its own short transaction, and runs the
registered handler outside that transaction so long work never holds a
connection. Crashed workers self-heal: leases expire and tasks return to the
pending pool.
"""

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.adapters import task_queue
from buildlens.core import ingestion
from buildlens.core.exceptions import TaskDeferred
from buildlens.db.models import IngestionTask, TaskType

logger = structlog.get_logger()

IDLE_SLEEP_SECONDS = 3.0
LEASE_SWEEP_INTERVAL_SECONDS = 60.0

Handler = Callable[[AsyncSession, IngestionTask], Awaitable[None]]


async def _not_implemented(session: AsyncSession, task: IngestionTask) -> None:
    raise NotImplementedError(f"No handler implemented for {task.task_type}")


HANDLERS: dict[TaskType, Handler] = {
    TaskType.BACKFILL_REPO: ingestion.backfill_repo,
    TaskType.POLL_REPO: ingestion.poll_repo,
    TaskType.INGEST_RUN: _not_implemented,
    TaskType.DIAGNOSE_RUN: _not_implemented,
    TaskType.GC_REPO: _not_implemented,
}


async def process_one(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    """Claim and run a single task. Returns False when the queue is empty."""
    async with session_factory() as session:
        task = await task_queue.claim_next(session)
        if task is None:
            return False
        await session.commit()
        task_id, task_type = str(task.id), task.task_type

    structlog.contextvars.bind_contextvars(task_id=task_id, task_type=task_type)
    logger.info("task.started")

    try:
        async with session_factory() as work_session:
            handler = HANDLERS.get(TaskType(task_type), _not_implemented)
            claimed = await work_session.get(IngestionTask, task.id)
            assert claimed is not None
            await handler(work_session, claimed)
            await task_queue.mark_done(work_session, claimed)
            await work_session.commit()
        logger.info("task.completed")
    except TaskDeferred as deferral:
        logger.info("task.deferred_by_handler")
        async with session_factory() as defer_session:
            deferred = await defer_session.get(IngestionTask, task.id)
            if deferred is not None:
                await task_queue.defer(defer_session, deferred, deferral.until)
                await defer_session.commit()
    except Exception as exc:
        logger.exception("task.failed")
        async with session_factory() as fail_session:
            failed = await fail_session.get(IngestionTask, task.id)
            if failed is not None:
                await task_queue.mark_failed(fail_session, failed, repr(exc))
                await fail_session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("task_id", "task_type")

    return True


async def run_worker(
    session_factory: async_sessionmaker[AsyncSession], shutdown: asyncio.Event
) -> None:
    """Drain the queue until asked to stop."""
    logger.info("worker.started")
    last_sweep = 0.0

    while not shutdown.is_set():
        now = asyncio.get_running_loop().time()
        if now - last_sweep > LEASE_SWEEP_INTERVAL_SECONDS:
            async with session_factory() as session:
                await task_queue.recover_expired_leases(session)
                await session.commit()
            last_sweep = now

        did_work = await process_one(session_factory)
        if not did_work:
            delay = IDLE_SLEEP_SECONDS * random.uniform(0.8, 1.2)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=delay)

    logger.info("worker.stopped")
