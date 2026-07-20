"""Tests for platform endpoints and the error framework (the manual checklist, mechanized)."""

from fastapi.testclient import TestClient

from buildlens.api.app import create_app


def test_healthz_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "x-request-id" in response.headers


def test_unknown_route_returns_problem_details() -> None:
    client = TestClient(create_app())

    response = client.get("/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["request_id"] is not None
    assert response.headers["x-request-id"] == body["request_id"]


def test_inbound_request_id_is_honored() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz", headers={"X-Request-ID": "my-trace-123"})

    assert response.headers["x-request-id"] == "my-trace-123"