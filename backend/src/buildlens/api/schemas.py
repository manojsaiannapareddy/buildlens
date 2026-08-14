"""Request and response models for the HTTP API (spec §6).

Deliberately separate from the ORM models. A response schema is a contract:
adding a column to `Repository` must not silently widen what the API returns,
and internal fields like `runs_etag` must not leak because nobody remembered
to exclude them.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from buildlens.db.models import RepoStatus

# GitHub's own limits: owners are alphanumeric plus internal hyphens, at most
# 39 characters; repository names also allow dots and underscores, up to 100.
# These values become URL path segments in the adapter, so they are validated
# at the boundary rather than trusted to 404.
OWNER_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$"
REPO_NAME_PATTERN = r"^[A-Za-z0-9._-]+$"


class RepositoryCreate(BaseModel):
    owner: str = Field(min_length=1, max_length=39, pattern=OWNER_PATTERN)
    name: str = Field(min_length=1, max_length=100, pattern=REPO_NAME_PATTERN)


class RepositoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner: str
    name: str
    default_branch: str
    status: RepoStatus
    last_polled_at: datetime | None
    created_at: datetime
