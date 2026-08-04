import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from buildlens.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, _enum


class RepoStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETING = "deleting"


class IngestState(StrEnum):
    PENDING = "pending"
    FETCHING = "fetching"
    READY = "ready"
    FAILED = "failed"


class RunConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    OTHER = "other"


class JobStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class Repository(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "repositories"

    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    owner: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[RepoStatus] = mapped_column(
        _enum(RepoStatus, "repo_status"), default=RepoStatus.ACTIVE, index=True
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runs_etag: Mapped[str | None] = mapped_column(String(255))

    runs: Mapped[list["WorkflowRun"]] = relationship(
        "WorkflowRun", back_populates="repository", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repositories_owner_name"),)


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    workflow_name: Mapped[str] = mapped_column(String(255), default="unknown")
    branch: Mapped[str | None] = mapped_column(String(255))
    ingest_state: Mapped[IngestState] = mapped_column(
        _enum(IngestState, "ingest_state"), default=IngestState.PENDING, index=True
    )
    raw_log_uri: Mapped[str | None] = mapped_column(String(500))

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    github_run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    run_attempt: Mapped[int] = mapped_column(Integer, default=1)
    event: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(40))
    conclusion: Mapped[RunConclusion] = mapped_column(
        _enum(RunConclusion, "run_conclusion"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped["Repository"] = relationship("Repository", back_populates="runs")
    jobs: Mapped[list["Job"]] = relationship(
        "Job", back_populates="workflow_run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "github_run_id",
            "run_attempt",
            name="uq_workflow_runs_repo_run_attempt",
        ),
    )


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "jobs"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    github_job_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(255))
    step_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus, "job_status"))
    conclusion: Mapped[RunConclusion | None] = mapped_column(_enum(RunConclusion, "job_conclusion"))
    steps: Mapped[dict | list | None] = mapped_column(JSONB)
    log_line_count: Mapped[int] = mapped_column(Integer, default=0)
    workflow_run: Mapped["WorkflowRun"] = relationship("WorkflowRun", back_populates="jobs")


class TaskType(StrEnum):
    BACKFILL_REPO = "backfill_repo"
    POLL_REPO = "poll_repo"
    INGEST_RUN = "ingest_run"
    DIAGNOSE_RUN = "diagnose_run"
    GC_REPO = "gc_repo"


class TaskStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class IngestionTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable work queue (spec §5.2). Claimed via FOR UPDATE SKIP LOCKED."""

    __tablename__ = "ingestion_tasks"
    __table_args__ = (
        Index(
            "ix_ingestion_tasks_claimable",
            "priority",
            "next_run_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    task_type: Mapped[TaskType] = mapped_column(_enum(TaskType, "task_type"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"), default=TaskStatus.PENDING, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
