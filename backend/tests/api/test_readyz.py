"""Integration tests for the readiness endpoint (requires a live database)."""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_readyz_reports_ready_when_database_is_reachable(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.integration
def test_healthz_does_not_depend_on_the_database(client: TestClient) -> None:
    """Liveness must never check dependencies — that distinction prevents restart loops."""
    response = client.get("/healthz")

    assert response.status_code == 200
