"""Shared pytest fixtures for the buildlens test suite."""

import pytest
from fastapi.testclient import TestClient

from buildlens.api.app import create_app
from buildlens.core.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Isolate tests from each other's cached Settings (get_settings is lru_cached)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    """A test client for a freshly built app."""
    return TestClient(create_app())


@pytest.fixture
def crashing_client() -> TestClient:
    """A client whose app has a test-only route that raises — for 500-path tests."""
    app = create_app()

    @app.get("/test-only/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail: db password is hunter2")

    return TestClient(app, raise_server_exceptions=False)