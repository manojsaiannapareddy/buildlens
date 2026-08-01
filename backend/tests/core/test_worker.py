"""Worker loop behavior (requires a live database)."""

from typing import Any

import pytest

from buildlens.adapters import task_queue
from buildlens.core import worker
from buildlens.db.models import IngestionTask, TaskStatus, TaskType

pytestmark = pytest.mark.integration


async def test_process_one_returns_false_on_empty_queue(
    session_factory: Any, clean_db: Any
) -> None:
    assert await worker.process_one(session_factory) is False


async def test_successful_task_is_marked_done(
    session_factory: Any, clean_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(session: Any, task: Any) -> None:
        return None

    monkeypatch.setitem(worker.HANDLERS, TaskType.GC_REPO, ok)

    async with session_factory() as session:
        task = await task_queue.enqueue(session, TaskType.GC_REPO, {})
        await session.commit()
        task_id = task.id

    assert await worker.process_one(session_factory) is True

    async with session_factory() as session:
        result = await session.get(IngestionTask, task_id)
        assert result is not None
        assert result.status == TaskStatus.DONE


async def test_failing_task_is_rescheduled_with_error(
    session_factory: Any, clean_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(session: Any, task: Any) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(worker.HANDLERS, TaskType.GC_REPO, boom)

    async with session_factory() as session:
        task = await task_queue.enqueue(session, TaskType.GC_REPO, {})
        await session.commit()
        task_id = task.id

    await worker.process_one(session_factory)

    async with session_factory() as session:
        result = await session.get(IngestionTask, task_id)
        assert result is not None
        assert result.status == TaskStatus.PENDING
        assert result.attempts == 1
        assert "handler exploded" in (result.last_error or "")
