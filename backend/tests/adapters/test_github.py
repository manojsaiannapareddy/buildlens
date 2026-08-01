"""GitHub adapter behavior, with all HTTP mocked (no network)."""

import httpx
import pytest
import respx
from pydantic import SecretStr

from buildlens.adapters.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from buildlens.core.config import Settings

BASE = "https://api.github.com"


def _settings() -> Settings:
    return Settings(database_url="postgresql+asyncpg://x/y", github_token=SecretStr("fake-token"))


@respx.mock
async def test_list_failed_runs_parses_and_paginates() -> None:
    respx.get(f"{BASE}/repos/o/r/actions/runs").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"etag": 'W/"abc"', "x-ratelimit-remaining": "4999"},
                json={
                    "workflow_runs": [
                        {
                            "id": 1,
                            "run_attempt": 1,
                            "name": "CI",
                            "event": "push",
                            "head_branch": "main",
                            "head_sha": "a" * 40,
                            "conclusion": "failure",
                            "run_started_at": "2026-07-30T12:00:00Z",
                            "updated_at": "2026-07-30T12:05:00Z",
                        }
                    ]
                },
            ),
            httpx.Response(200, json={"workflow_runs": []}),
        ]
    )

    async with GitHubClient(_settings()) as gh:
        runs, etag = await gh.list_failed_runs("o", "r")

    assert len(runs) == 1
    assert runs[0].workflow_name == "CI"
    assert etag == 'W/"abc"'


@respx.mock
async def test_not_modified_returns_no_runs() -> None:
    respx.get(f"{BASE}/repos/o/r/actions/runs").mock(return_value=httpx.Response(304))

    async with GitHubClient(_settings()) as gh:
        runs, etag = await gh.list_failed_runs("o", "r", etag='W/"abc"')

    assert runs == []
    assert etag == 'W/"abc"'


@respx.mock
async def test_rate_limit_raises_typed_error_with_reset_time() -> None:
    respx.get(f"{BASE}/repos/o/r").mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000"}
        )
    )

    async with GitHubClient(_settings()) as gh:
        with pytest.raises(GitHubRateLimitError) as excinfo:
            await gh.get_repo("o", "r")

    assert excinfo.value.reset_at.year == 2026


@respx.mock
async def test_missing_repo_raises_not_found() -> None:
    respx.get(f"{BASE}/repos/o/r").mock(return_value=httpx.Response(404))

    async with GitHubClient(_settings()) as gh:
        with pytest.raises(GitHubNotFoundError):
            await gh.get_repo("o", "r")


async def test_missing_token_fails_fast() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://x/y",
        github_token=None,
    )
    with pytest.raises(GitHubAuthError):
        GitHubClient(settings)
