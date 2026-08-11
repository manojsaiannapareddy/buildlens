"""Shared pytest fixtures for the buildlens test suite."""

from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.api.app import create_app
from buildlens.core.config import get_settings
from buildlens.db.base import Base
from buildlens.db.session import create_engine, create_session_factory


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_engine(get_settings())
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Isolate tests from each other's cached Settings (get_settings is lru_cached)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def crashing_client() -> TestClient:
    """A client whose app has a test-only route that raises — for 500-path tests."""
    app = create_app()

    @app.get("/test-only/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail: db password is hunter2")

    return TestClient(app, raise_server_exceptions=False)


@pytest_asyncio.fixture(autouse=True)
async def clean_database(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None, None]:
    """Truncate every table before each integration test.

    Two properties this fixture has to hold, both learned from breaking them:

    1. The table list comes from the model metadata, never a hand-written
       string. A hand-written list stops covering the schema the moment a
       table is added, and the symptom — a suite that passes only when it runs
       first — points nowhere near the cause.
    2. Unit tests never open a connection. The dependency above is safe to
       request without a database because `create_async_engine` builds a pool
       lazily and connects to nothing; it is the TRUNCATE below that needs a
       server, and that is what the marker gates. Requesting the factory
       unconditionally (rather than through `getfixturevalue`) matters: that
       helper hands back an un-awaited coroutine for an async fixture.
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    tables = ", ".join(sorted(table.name for table in Base.metadata.sorted_tables))
    async with session_factory() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()
    yield
