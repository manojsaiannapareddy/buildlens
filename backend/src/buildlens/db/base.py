"""Declarative base: the registry every model inherits from."""

import uuid
from datetime import datetime
from enum import Enum
from typing import TypeVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

E = TypeVar("E", bound=Enum)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _enum[E: Enum](enum_cls: type[E], name: str) -> SQLEnum:
    """VARCHAR + CHECK constraint rather than native PG enum for easy migrations."""
    return SQLEnum(
        enum_cls,
        name=name,
        native_enum=False,
        values_callable=lambda obj: [e.value for e in obj],
    )


class Base(DeclarativeBase):
    """Base class for all buildlens ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Mixin that adds a UUID primary key to a model."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    """Mixin that adds created_at and updated_at columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
