"""Shared pytest fixtures for the buildlens test suite."""

from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buildlens.api.app import create_app
from buildlens.core.config import get_settings
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


@pytest.fixture(autouse=True)
async def clean_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Truncate tables between integration tests to prevent state leakage."""
    async with session_factory() as session:
        await session.execute(text("TRUNCATE TABLE ingestion_tasks CASCADE;"))
        await session.commit()
