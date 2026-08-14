"""Poll scheduler (spec §7.1, FR-3).

A loop inside the worker process, not a third process: one image, one deploy
unit, and nothing new to operate. Every worker runs this loop, so the sweep
takes a transaction-scoped advisory lock — whichever worker arrives first
schedules the round and the others skip immediately rather than blocking.
`pg_try_advisory_xact_lock` releases itself when the transaction ends, so a
worker killed mid-sweep cannot wedge the lock.

Scheduling is deliberately conservative: a repository already holding an
unfinished `poll_repo` task is never given a second one. Without that check a
slow or deferred poll accumulates duplicates, every one of which burns a
GitHub request against the same 5,000/hour budget.
"""

import asyncio
import contextlib
import random
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.adapters import task_queue
from buildlens.db.models import (
    IngestionTask,
    Repository,
    RepoStatus,
    TaskStatus,
    TaskType,
)

logger = structlog.get_logger()

POLL_INTERVAL = timedelta(minutes=10)
POLL_JITTER = timedelta(minutes=2)
TICK_INTERVAL_SECONDS = 30.0

# Arbitrary but fixed: "bldl" as bytes. Any other advisory lock in this system
# must pick a different constant, so they are declared here together.
SCHEDULER_LOCK_KEY = 0x626C646C


async def enqueue_due_polls(session: AsyncSession) -> int:
    """Queue a poll for every active repository that is due for one.

    Returns the number enqueued — zero when another worker holds the lock,
    which is the normal case for all but one worker each round.
    """
    acquired = (
        await session.execute(select(func.pg_try_advisory_xact_lock(SCHEDULER_LOCK_KEY)))
    ).scalar_one()
    if not acquired:
        logger.debug("scheduler.tick_skipped", reason="not_leader")
        return 0

    cutoff = datetime.now(UTC) - POLL_INTERVAL
    open_polls = select(IngestionTask.payload["repository_id"].astext).where(
        IngestionTask.task_type == TaskType.POLL_REPO,
        IngestionTask.status.in_((TaskStatus.PENDING, TaskStatus.LEASED)),
    )
    due = (
        (
            await session.execute(
                select(Repository).where(
                    Repository.status == RepoStatus.ACTIVE,
                    or_(
                        Repository.last_polled_at.is_(None),
                        Repository.last_polled_at < cutoff,
                    ),
                    cast(Repository.id, Text).notin_(open_polls),
                )
            )
        )
        .scalars()
        .all()
    )

    for repo in due:
        await task_queue.enqueue(
            session,
            TaskType.POLL_REPO,
            {"repository_id": str(repo.id)},
            # Jitter spreads the herd: without it every repository registered
            # in the same minute polls in the same minute, forever.
            delay=timedelta(seconds=random.uniform(0, POLL_JITTER.total_seconds())),
        )

    if due:
        logger.info("scheduler.polls_enqueued", count=len(due))
    return len(due)


async def run_scheduler(
    session_factory: async_sessionmaker[AsyncSession], shutdown: asyncio.Event
) -> None:
    """Tick until asked to stop.

    A failing tick is logged and retried on the next round rather than
    propagated: the scheduler falling over must not take ingestion with it.
    """
    logger.info("scheduler.started")

    while not shutdown.is_set():
        try:
            async with session_factory() as session:
                await enqueue_due_polls(session)
                await session.commit()
        except Exception:
            logger.exception("scheduler.tick_failed")

        delay = TICK_INTERVAL_SECONDS * random.uniform(0.8, 1.2)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=delay)

    logger.info("scheduler.stopped")
