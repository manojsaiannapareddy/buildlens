"""GitHub REST client (spec §7.3).

Translates GitHub's API into buildlens DTOs and typed errors. Rate-limit
aware, supports conditional requests via ETag, and downloads log archives
immediately because their signed URLs expire in ~60 seconds.
"""

from datetime import UTC, datetime
from typing import Any, Self

import httpx
import structlog
from pydantic import BaseModel

from buildlens.core.config import Settings

logger = structlog.get_logger()

REQUEST_TIMEOUT_SECONDS = 30.0
LOG_DOWNLOAD_TIMEOUT_SECONDS = 120.0
RATE_LIMIT_WARN_THRESHOLD = 500


class GitHubError(Exception):
    """Base class for GitHub adapter failures."""


class GitHubNotFoundError(GitHubError):
    """The requested resource does not exist or is not public."""


class GitHubAuthError(GitHubError):
    """Missing or invalid credentials."""


class GitHubRateLimitError(GitHubError):
    def __init__(self, reset_at: datetime) -> None:
        super().__init__(f"GitHub rate limit exhausted until {reset_at.isoformat()}")
        self.reset_at = reset_at


class RepoInfo(BaseModel):
    github_id: int
    owner: str
    name: str
    default_branch: str
    private: bool


class RunInfo(BaseModel):
    github_run_id: int
    run_attempt: int
    workflow_name: str
    event: str
    branch: str | None
    head_sha: str
    conclusion: str
    started_at: datetime | None
    completed_at: datetime | None


class JobInfo(BaseModel):
    github_job_id: int
    name: str
    status: str
    conclusion: str | None
    steps: list[dict[str, Any]]


class GitHubClient:
    """Thin, typed wrapper over the GitHub REST API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if settings.github_token is None:
            raise GitHubAuthError("BUILDLENS_GITHUB_TOKEN is not configured")
        self._client = client or httpx.AsyncClient(
            base_url=settings.github_api_base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "buildlens",
            },
            follow_redirects=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    def _check(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None and int(remaining) < RATE_LIMIT_WARN_THRESHOLD:
            logger.warning("github.rate_limit_low", remaining=int(remaining))

        if response.status_code == 403 and remaining == "0":
            reset = int(response.headers.get("x-ratelimit-reset", "0"))
            raise GitHubRateLimitError(datetime.fromtimestamp(reset, tz=UTC))
        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the credentials")
        if response.status_code == 404:
            raise GitHubNotFoundError(str(response.url))
        if response.status_code >= 400:
            raise GitHubError(f"GitHub returned {response.status_code} for {response.url}")

    async def get_repo(self, owner: str, name: str) -> RepoInfo:
        response = await self._client.get(f"/repos/{owner}/{name}")
        self._check(response)
        data = response.json()
        return RepoInfo(
            github_id=data["id"],
            owner=data["owner"]["login"],
            name=data["name"],
            default_branch=data["default_branch"],
            private=data["private"],
        )

    async def list_failed_runs(
        self, owner: str, name: str, *, limit: int = 50, etag: str | None = None
    ) -> tuple[list[RunInfo], str | None]:
        """Return failed runs and the response ETag. Empty list on 304."""
        headers = {"If-None-Match": etag} if etag else None
        runs: list[RunInfo] = []
        new_etag: str | None = None
        page = 1

        while len(runs) < limit:
            response = await self._client.get(
                f"/repos/{owner}/{name}/actions/runs",
                params={"status": "failure", "per_page": 100, "page": page},
                headers=headers,
            )
            if response.status_code == 304:
                return [], etag
            self._check(response)
            new_etag = new_etag or response.headers.get("etag")

            batch = response.json().get("workflow_runs", [])
            if not batch:
                break
            runs.extend(
                RunInfo(
                    github_run_id=item["id"],
                    run_attempt=item.get("run_attempt", 1),
                    workflow_name=item.get("name") or "unknown",
                    event=item["event"],
                    branch=item.get("head_branch"),
                    head_sha=item["head_sha"],
                    conclusion=item.get("conclusion") or "failure",
                    started_at=item.get("run_started_at"),
                    completed_at=item.get("updated_at"),
                )
                for item in batch
            )
            page += 1
            headers = None  # ETag applies to the first page only

        return runs[:limit], new_etag

    async def list_jobs(self, owner: str, name: str, run_id: int) -> list[JobInfo]:
        response = await self._client.get(
            f"/repos/{owner}/{name}/actions/runs/{run_id}/jobs", params={"per_page": 100}
        )
        self._check(response)
        return [
            JobInfo(
                github_job_id=item["id"],
                name=item["name"],
                status=item["status"],
                conclusion=item.get("conclusion"),
                steps=item.get("steps", []),
            )
            for item in response.json().get("jobs", [])
        ]

    async def download_run_logs(self, owner: str, name: str, run_id: int) -> bytes:
        """Fetch the log archive. The redirect target expires in ~60s, so we
        follow it immediately and never persist the signed URL."""
        response = await self._client.get(
            f"/repos/{owner}/{name}/actions/runs/{run_id}/logs",
            timeout=LOG_DOWNLOAD_TIMEOUT_SECONDS,
        )
        self._check(response)
        logger.info("github.logs_downloaded", run_id=run_id, bytes=len(response.content))
        return response.content
