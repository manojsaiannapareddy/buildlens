"""Blob storage for immutable raw artifacts (FR-4, TC-4).

`core` asks for "somewhere to put bytes" through the ObjectStore protocol, so
the S3 backend can land in M5 without touching a domain module. The local
backend is not a stand-in for the real thing: it is what makes NFR-14 real for
ingestion, since no test needs credentials or a network to exercise the
storage path.

Raw archives are immutable (TC-4). Writes are atomic — temp file plus rename —
so a crash mid-write can never leave a truncated object under a live key.
"""

import asyncio
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol

import structlog

from buildlens.core.config import Settings

logger = structlog.get_logger()


class ObjectStoreError(Exception):
    """Storage could not satisfy the request."""


class ObjectNotFoundError(ObjectStoreError):
    """No object exists at the requested key."""


class ObjectStore(Protocol):
    """The storage contract `core` depends on. Backends implement this."""

    async def put(self, key: str, data: bytes) -> str:
        """Store bytes under key and return a durable URI for them."""
        ...

    async def get(self, key: str) -> bytes:
        """Read back the bytes stored under key."""
        ...


def _validate_key(key: str) -> None:
    """Reject keys that would escape the store root.

    Keys are built from our own identifiers today, but this is the boundary
    where a future user-supplied component would arrive, and a traversal check
    is cheaper than the incident.
    """
    if not key or key.endswith("/"):
        raise ObjectStoreError(f"Invalid object key: {key!r}")
    parts = PurePosixPath(key).parts
    if key.startswith("/") or any(part in {"..", "."} for part in parts):
        raise ObjectStoreError(f"Invalid object key: {key!r}")


class LocalObjectStore:
    """Filesystem-backed store for dev and tests.

    File I/O runs on a worker thread: the event loop stays free while a
    multi-megabyte archive lands on disk.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        _validate_key(key)
        return self._root / key

    async def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        await asyncio.to_thread(self._write_atomic, path, data)
        logger.info("object_store.stored", key=key, bytes=len(data))
        return path.resolve().as_uri()

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".part")
        try:
            with os.fdopen(handle, "wb") as tmp:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc


def make_object_store(settings: Settings) -> ObjectStore:
    """Build the configured backend. S3 joins this switch in M5."""
    return LocalObjectStore(settings.object_store_root)
