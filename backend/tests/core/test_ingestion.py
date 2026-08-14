"""Ingestion handler behavior with a stubbed GitHub client (requires a live database)."""

import io
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.adapters.github import (
    GitHubNotFoundError,
    GitHubRateLimitError,
    JobInfo,
    RunInfo,
)
from buildlens.core import ingestion
from buildlens.core.worker import TaskDeferred
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
    jobs: list[JobInfo] | None = None,
    archive: bytes = b"",
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

        async def list_jobs(self, owner: str, name: str, run_id: int) -> list[JobInfo]:
            if error:
                raise error
            return jobs or []

        async def download_run_logs(self, owner: str, name: str, run_id: int) -> bytes:
            if error:
                raise error
            return archive

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


def _job(
    job_id: int = 11, name: str = "build", steps: list[dict[str, Any]] | None = None
) -> JobInfo:
    return JobInfo(
        github_job_id=job_id,
        name=name,
        status="completed",
        conclusion="failure",
        steps=steps
        if steps is not None
        else [
            {"name": "Set up job", "conclusion": "success"},
            {"name": "Run tests", "conclusion": "failure"},
        ],
    )


def _archive(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for entry_name, data in entries.items():
            bundle.writestr(entry_name, data)
    return buffer.getvalue()


class _MemoryStore:
    """Stands in for S3 without touching a disk or a network."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> str:
        self.objects[key] = data
        return f"memory://{key}"

    async def get(self, key: str) -> bytes:
        return self.objects[key]


def _stub_store(monkeypatch: MonkeyPatch) -> _MemoryStore:
    store = _MemoryStore()
    monkeypatch.setattr(ingestion, "make_store", lambda: store)
    return store


async def _pending_run(
    session: AsyncSession, repo: Repository, github_run_id: int = 99
) -> WorkflowRun:
    run = WorkflowRun(
        repository_id=repo.id,
        github_run_id=github_run_id,
        run_attempt=1,
        workflow_name="CI",
        event="push",
        branch="main",
        head_sha="b" * 40,
        conclusion=RunConclusion.FAILURE,
        ingest_state=IngestState.PENDING,
    )
    session.add(run)
    await session.flush()
    return run


def _ingest_task(run: WorkflowRun) -> IngestionTask:
    return IngestionTask(task_type=TaskType.INGEST_RUN, payload={"run_id": str(run.id)})


async def test_backfill_records_runs_and_queues_ingestion(
    session_factory: async_sessionmaker[AsyncSession],
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


async def test_ingest_run_stores_archive_and_records_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    archive = _archive({"1_build.txt": b"a\nb\nc\n", "2_lint.txt": b"x\n"})
    _stub_client(monkeypatch, jobs=[_job(11, "build"), _job(12, "lint")], archive=archive)
    store = _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

        jobs = (
            await session.execute(Job.__table__.select().where(Job.workflow_run_id == run.id))
        ).all()

    assert run.ingest_state is IngestState.READY
    assert run.raw_log_uri == f"memory://{ingestion.archive_key(run)}"
    assert store.objects[ingestion.archive_key(run)] == archive
    assert {job.name: job.log_line_count for job in jobs} == {"build": 3, "lint": 1}
    assert {job.step_name for job in jobs} == {"Run tests"}
    assert {job.status for job in jobs} == {JobStatus.COMPLETED}


async def test_ingest_run_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    """At-least-once delivery means this runs twice in production eventually."""
    _stub_client(monkeypatch, jobs=[_job(11, "build")], archive=_archive({"1_build.txt": b"a\n"}))
    _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

        # A redelivery arrives after the run is already ready.
        run.ingest_state = IngestState.PENDING
        await session.commit()
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

        jobs = (
            await session.execute(Job.__table__.select().where(Job.workflow_run_id == run.id))
        ).all()

    assert len(jobs) == 1


async def test_ingest_run_skips_work_when_already_ready(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    _stub_client(monkeypatch, jobs=[_job()], archive=_archive({"1_build.txt": b"a\n"}))
    store = _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        run.ingest_state = IngestState.READY
        await session.commit()

        await ingestion.ingest_run(session, _ingest_task(run))

    assert store.objects == {}


async def test_ingest_run_updates_a_job_that_changed(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    """Re-ingesting overwrites the job row rather than inserting beside it."""
    _stub_client(monkeypatch, jobs=[_job(11, "build")], archive=_archive({"1_build.txt": b"a\n"}))
    _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

        _stub_client(
            monkeypatch,
            jobs=[_job(11, "build")],
            archive=_archive({"1_build.txt": b"a\nb\nc\nd\n"}),
        )
        run.ingest_state = IngestState.PENDING
        await session.commit()
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

        jobs = (
            await session.execute(Job.__table__.select().where(Job.workflow_run_id == run.id))
        ).all()

    assert len(jobs) == 1
    assert jobs[0].log_line_count == 4


async def test_ingest_run_defers_on_rate_limit_without_stranding_the_run(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    reset = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
    _stub_client(monkeypatch, error=GitHubRateLimitError(reset))
    _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        await session.commit()

        with pytest.raises(TaskDeferred) as excinfo:
            await ingestion.ingest_run(session, _ingest_task(run))

    assert excinfo.value.until == reset

    # The state has to survive the rolled-back transaction, or the run sits in
    # `fetching` forever while the task quietly retries.
    async with session_factory() as session:
        reloaded = await session.get(WorkflowRun, run.id)
        assert reloaded is not None
        assert reloaded.ingest_state is IngestState.PENDING


async def test_ingest_run_marks_expired_logs_as_failed(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    """GitHub drops run logs after 90 days. Retrying will never help."""
    _stub_client(monkeypatch, error=GitHubNotFoundError("gone"))
    _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

    assert run.ingest_state is IngestState.FAILED


async def test_ingest_run_tolerates_a_conclusion_github_invented(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: MonkeyPatch,
) -> None:
    """Unknown vocabulary maps to `other` instead of tripping a CHECK."""
    exotic = JobInfo(
        github_job_id=13, name="build", status="waiting", conclusion="startup_failure", steps=[]
    )
    _stub_client(monkeypatch, jobs=[exotic], archive=_archive({"1_build.txt": b"a\n"}))
    _stub_store(monkeypatch)

    async with session_factory() as session:
        repo = await _repo(session)
        run = await _pending_run(session, repo)
        await ingestion.ingest_run(session, _ingest_task(run))
        await session.commit()

        jobs = (
            await session.execute(Job.__table__.select().where(Job.workflow_run_id == run.id))
        ).all()

    assert jobs[0].conclusion is RunConclusion.OTHER
    assert jobs[0].status is JobStatus.QUEUED
    assert jobs[0].step_name is None
