"""Unit tests for the local object store. No database, no network."""

from pathlib import Path

import pytest

from buildlens.adapters.object_store import (
    LocalObjectStore,
    ObjectNotFoundError,
    ObjectStoreError,
)


async def test_put_then_get_round_trips(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    await store.put("repos/r/runs/1/logs.zip", b"\x50\x4b\x03\x04payload")

    assert await store.get("repos/r/runs/1/logs.zip") == b"\x50\x4b\x03\x04payload"


async def test_put_returns_a_resolvable_uri(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    uri = await store.put("a/b.zip", b"data")

    assert uri.startswith("file://")
    assert uri.endswith("a/b.zip")


async def test_nested_keys_create_their_directories(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    await store.put("deeply/nested/key.zip", b"data")

    assert (tmp_path / "deeply" / "nested" / "key.zip").read_bytes() == b"data"


async def test_rewriting_a_key_leaves_no_partial_file(tmp_path: Path) -> None:
    """Re-ingestion writes the same key again; the result is the new object
    and nothing else — no stray .part files from the atomic rename."""
    store = LocalObjectStore(tmp_path)
    await store.put("k.zip", b"first")
    await store.put("k.zip", b"second-and-longer")

    assert await store.get("k.zip") == b"second-and-longer"
    assert [p.name for p in tmp_path.iterdir()] == ["k.zip"]


async def test_missing_key_raises_a_typed_error(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ObjectNotFoundError):
        await store.get("never/written.zip")


@pytest.mark.parametrize("key", ["", "/absolute.zip", "../escape.zip", "a/../../escape.zip", "a/"])
async def test_keys_that_escape_the_root_are_rejected(tmp_path: Path, key: str) -> None:
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ObjectStoreError):
        await store.put(key, b"data")
