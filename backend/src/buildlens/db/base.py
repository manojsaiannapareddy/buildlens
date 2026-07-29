"""Declarative base: the registry every model inherits from.

Base.metadata is the single source of truth about our schema, used by
Alembic's autogenerate to diff models against the live database.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all buildlens ORM models."""
