"""Repository registration endpoint (FR-1). Requires a live database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from buildlens.adapters.github import GitHubNotFoundError, GitHubRateLimitError, RepoInfo
from buildlens.core import ingestion

pytestmark = pytest.mark.integration


def _stub_github(monkeypatch: MonkeyPatch, *, error: Exception | None = None) -> None:
    class Stub:
        async def get_repo(self, owner: str, name: str) -> RepoInfo:
            if error:
                raise error
            return RepoInfo(
                github_id=4242, owner=owner, name=name, default_branch="main", private=False
            )

    @asynccontextmanager
    async def factory() -> AsyncIterator[Stub]:
        yield Stub()

    monkeypatch.setattr(ingestion, "make_client", factory)


def test_registering_a_repository_returns_201(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    _stub_github(monkeypatch)

    response = client.post("/api/v1/repos", json={"owner": "octocat", "name": "hello-world"})

    assert response.status_code == 201
    body = response.json()
    assert body["owner"] == "octocat"
    assert body["name"] == "hello-world"
    assert body["status"] == "active"
    assert body["last_polled_at"] is None


def test_registering_twice_returns_200_not_a_conflict(
    client: TestClient, monkeypatch: MonkeyPatch
) -> None:
    """A client retrying after a timeout should not be punished for succeeding."""
    _stub_github(monkeypatch)
    first = client.post("/api/v1/repos", json={"owner": "octocat", "name": "hello-world"})
    second = client.post("/api/v1/repos", json={"owner": "octocat", "name": "hello-world"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_internal_fields_are_not_exposed(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    """The response is a contract, not a dump of the row."""
    _stub_github(monkeypatch)

    body = client.post("/api/v1/repos", json={"owner": "octocat", "name": "hello-world"}).json()

    assert set(body) == {
        "id",
        "owner",
        "name",
        "default_branch",
        "status",
        "last_polled_at",
        "created_at",
    }


def test_unknown_repository_returns_404(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    _stub_github(monkeypatch, error=GitHubNotFoundError("nope"))

    response = client.post("/api/v1/repos", json={"owner": "octocat", "name": "ghost"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_upstream_rate_limit_returns_503(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    """Our budget, not the caller's behaviour — so 503, never 429."""
    reset = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
    _stub_github(monkeypatch, error=GitHubRateLimitError(reset))

    response = client.post("/api/v1/repos", json={"owner": "octocat", "name": "hello-world"})

    assert response.status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"owner": "", "name": "r"},
        {"owner": "-leading", "name": "r"},
        {"owner": "trailing-", "name": "r"},
        {"owner": "../etc", "name": "r"},
        {"owner": "o", "name": "has spaces"},
        {"owner": "o", "name": "../../escape"},
        {"owner": "o"},
    ],
)
def test_malformed_identifiers_are_rejected_before_any_network_call(
    client: TestClient, monkeypatch: MonkeyPatch, body: dict[str, str]
) -> None:
    """These become URL path segments downstream, so they never leave the boundary."""

    @asynccontextmanager
    async def exploding() -> AsyncIterator[None]:
        raise AssertionError("the adapter must not be reached")
        yield

    monkeypatch.setattr(ingestion, "make_client", exploding)

    assert client.post("/api/v1/repos", json=body).status_code == 422
