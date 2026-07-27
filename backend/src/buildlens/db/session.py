"""Database engine and session factory (async SQLAlchemy).

The engine (and its connection pool) is created once per application;
sessions are short-lived, one unit of work each.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from buildlens.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the application's connection pool."""
    return create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the factory that produces per-unit-of-work sessions."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
