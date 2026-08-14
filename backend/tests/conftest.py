"""Shared pytest fixtures for the buildlens test suite.

Tests run against their own database, derived from the configured one by
appending `_test`. The cleanup fixture below truncates every table, so sharing
a database with development means running the suite destroys development data.
Deriving the name here rather than requiring a second environment variable
means there is no way to point the suite at the wrong database by forgetting
to set something.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from buildlens.api.app import create_app
from buildlens.core.config import get_settings
from buildlens.db.base import Base
from buildlens.db.session import create_engine, create_session_factory
from tests.support import derive_test_database_url, require_test_database


async def _create_database_if_missing(url: str) -> None:
    """Create the test database.

    CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT, and it
    is issued from a connection to `postgres` because you cannot create a
    database from a connection to the database you are creating.
    """
    parsed = make_url(url)
    name = parsed.database
    admin = parsed.set(database="postgres").render_as_string(hide_password=False)

    engine = create_async_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if not exists:
                # The name is derived from our own config, never from input,
                # and an identifier cannot be a bound parameter.
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def _redirect_to_test_database() -> None:
    """Point Settings at the test database before anything reads it."""
    configured = get_settings().database_url
    os.environ["BUILDLENS_DATABASE_URL"] = derive_test_database_url(configured)
    get_settings.cache_clear()


# Runs at import, before any fixture or test module touches Settings.
_redirect_to_test_database()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Create and migrate the test database, once, and only if it is needed.

    A collection hook rather than a fixture: it runs synchronously, before any
    event loop exists, so it can call `asyncio.run`. `trylast` matters — the
    `-m` deselection is itself a `pytest_collection_modifyitems` hook, so
    running earlier would still see the integration tests that `make
    test-unit` just filtered out, and would try to connect.

    Migrations rather than `create_all`: the schema under test is then the one
    the migrations actually produce, which is the thing that ships (ADR-007).
    """
    if not any(item.get_closest_marker("integration") for item in items):
        return

    url = get_settings().database_url
    require_test_database(url)
    asyncio.run(_create_database_if_missing(url))

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


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

    Three properties this fixture has to hold, all learned from breaking them:

    1. It refuses to run against a database whose name does not end in
       `_test`. TRUNCATE is unrecoverable, and a fixture that quietly deletes
       real data is worse than one that fails loudly.
    2. The table list comes from the model metadata, never a hand-written
       string. A hand-written list stops covering the schema the moment a
       table is added, and the symptom — a suite that passes only when it runs
       first — points nowhere near the cause.
    3. Unit tests never open a connection. The dependency above is safe to
       request without a database because `create_async_engine` builds a pool
       lazily and connects to nothing; it is the TRUNCATE below that needs a
       server, and that is what the marker gates.
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    require_test_database(get_settings().database_url)

    tables = ", ".join(sorted(table.name for table in Base.metadata.sorted_tables))
    async with session_factory() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()
    yield
