"""Repository registration and listing (FR-1, spec §6.2)."""

import structlog
from fastapi import APIRouter, Response, status

from buildlens.adapters.github import GitHubNotFoundError, GitHubRateLimitError
from buildlens.api.deps import SessionDep
from buildlens.api.errors import problem_response
from buildlens.api.schemas import RepositoryCreate, RepositoryRead
from buildlens.core import ingestion

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["repos"])


@router.post(
    "/repos",
    response_model=RepositoryRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RepositoryRead, "description": "Already registered"}},
)
async def register_repository(
    body: RepositoryCreate, session: SessionDep, response: Response
) -> RepositoryRead | Response:
    """Register a public repository and queue its backfill.

    Idempotent: registering twice returns the same repository with 200 rather
    than a conflict, because a client retrying after a timeout should not be
    punished for the first attempt having succeeded.
    """
    try:
        repo, created = await ingestion.ensure_repository(session, body.owner, body.name)
    except GitHubNotFoundError:
        return problem_response(
            status=status.HTTP_404_NOT_FOUND,
            title="Repository not found",
            detail=f"{body.owner}/{body.name} does not exist or is not public.",
        )
    except GitHubRateLimitError as exc:
        # Ours to own, not the caller's fault: 503 with a hint about when to
        # come back, rather than a 429 that implies they sent too much.
        return problem_response(
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Upstream rate limit reached",
            detail=f"GitHub rate limit resets at {exc.reset_at.isoformat()}.",
        )

    await session.commit()

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    logger.info(
        "repos.registered",
        repository_id=str(repo.id),
        owner=repo.owner,
        name=repo.name,
        created=created,
    )
    return RepositoryRead.model_validate(repo)
