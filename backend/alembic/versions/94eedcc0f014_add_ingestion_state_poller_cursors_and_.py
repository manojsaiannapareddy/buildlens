"""add ingestion state, poller cursors, and job steps

Revision ID: 94eedcc0f014
Revises: 012089361fbb
Create Date: 2026-07-31 18:15:14.383384

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "94eedcc0f014"
down_revision: str | Sequence[str] | None = "012089361fbb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Repositories
    op.add_column("repositories", sa.Column("status", sa.String(50), nullable=True))
    op.add_column(
        "repositories", sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "repositories", sa.Column("last_synced_run_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("repositories", sa.Column("runs_etag", sa.String(255), nullable=True))

    op.execute(
        "UPDATE repositories SET status = CASE WHEN is_active THEN 'active' ELSE 'paused' END"
    )
    op.alter_column("repositories", "status", nullable=False)
    op.create_index(op.f("ix_repositories_status"), "repositories", ["status"], unique=False)

    # 2. WorkflowRuns
    op.add_column("workflow_runs", sa.Column("workflow_name", sa.String(255), nullable=True))
    op.add_column("workflow_runs", sa.Column("branch", sa.String(255), nullable=True))
    op.add_column("workflow_runs", sa.Column("ingest_state", sa.String(50), nullable=True))
    op.add_column("workflow_runs", sa.Column("raw_log_uri", sa.String(500), nullable=True))

    op.execute("UPDATE workflow_runs SET workflow_name = 'unknown' WHERE workflow_name IS NULL")
    op.execute("UPDATE workflow_runs SET ingest_state = 'pending' WHERE ingest_state IS NULL")
    op.alter_column("workflow_runs", "workflow_name", nullable=False)
    op.alter_column("workflow_runs", "ingest_state", nullable=False)
    op.create_index(
        op.f("ix_workflow_runs_ingest_state"), "workflow_runs", ["ingest_state"], unique=False
    )

    op.alter_column(
        "workflow_runs", "started_at", existing_type=sa.DateTime(timezone=True), nullable=True
    )
    op.alter_column(
        "workflow_runs", "completed_at", existing_type=sa.DateTime(timezone=True), nullable=True
    )

    # 3. Jobs
    op.add_column(
        "jobs",
        sa.Column("steps", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("jobs", sa.Column("log_line_count", sa.Integer(), nullable=True))

    op.execute("UPDATE jobs SET log_line_count = 0 WHERE log_line_count IS NULL")
    op.alter_column("jobs", "log_line_count", nullable=False)


def downgrade() -> None:
    op.drop_column("jobs", "log_line_count")
    op.drop_column("jobs", "steps")

    op.alter_column(
        "workflow_runs", "completed_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )
    op.alter_column(
        "workflow_runs", "started_at", existing_type=sa.DateTime(timezone=True), nullable=False
    )
    op.drop_index(op.f("ix_workflow_runs_ingest_state"), table_name="workflow_runs")
    op.drop_column("workflow_runs", "raw_log_uri")
    op.drop_column("workflow_runs", "ingest_state")
    op.drop_column("workflow_runs", "branch")
    op.drop_column("workflow_runs", "workflow_name")

    op.drop_index(op.f("ix_repositories_status"), table_name="repositories")
    op.drop_column("repositories", "runs_etag")
    op.drop_column("repositories", "last_synced_run_at")
    op.drop_column("repositories", "last_polled_at")
    op.drop_column("repositories", "status")
