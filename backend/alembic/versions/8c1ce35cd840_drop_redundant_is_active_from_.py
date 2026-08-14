"""drop redundant is_active from repositories

Revision ID: 8c1ce35cd840
Revises: a431325399a1
Create Date: 2026-08-13 21:11:58.656954

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c1ce35cd840"
down_revision: str | Sequence[str] | None = "a431325399a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # `status` has carried this meaning since PAUSED and DELETING were added;
    # `is_active` had no readers or writers anywhere in the codebase, only a
    # NOT NULL obligation on every insert. Contract is safe here because the
    # readers were already gone — with live readers this would need to be two
    # deploys, stop reading then drop.
    op.drop_column("repositories", "is_active")


def downgrade() -> None:
    """Downgrade schema."""
    # Expand/contract in reverse: a NOT NULL column cannot be added to a table
    # with rows in one step. Backfill from status, which is authoritative.
    op.add_column("repositories", sa.Column("is_active", sa.Boolean(), nullable=True))
    op.execute("UPDATE repositories SET is_active = (status = 'active')")
    op.alter_column("repositories", "is_active", nullable=False)
