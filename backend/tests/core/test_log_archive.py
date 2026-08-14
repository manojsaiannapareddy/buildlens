"""Unit tests for reading GitHub's run-log archive. No database, no network."""

import io
import zipfile
from pathlib import PurePosixPath

import pytest

from buildlens.core import log_archive
from buildlens.core.log_archive import (
    LogArchiveError,
    count_lines,
    job_log_line_counts,
    slug,
)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("3_Build (3.12)", "build-3-12"),
        ("build (3.12)", "build-3-12"),
        ("10_lint", "lint"),
        ("Run tests / unit", "run-tests-unit"),
        ("  spaced  ", "spaced"),
        ("", ""),
        (
            "benchmarks walltime (colour_science|static_frame)",
            "benchmarks-walltime-colour-sciencestatic-frame",
        ),
        ('a<b>c:d"e/f\\g|h?i*j', "abcdefghij"),
        ("Run tests / unit", "run-tests-unit"),
    ],
)
def test_slug_collapses_names_to_a_comparable_key(
    name: str,
    expected: str,
) -> None:
    assert slug(name) == expected


def test_slug_matches_a_job_name_to_its_archive_entry() -> None:
    """The property the whole mapping rests on."""
    assert slug("3_Build (3.12).txt".removesuffix(".txt")) == slug("Build (3.12)")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", 0),
        (b"one", 1),
        (b"one\n", 1),
        (b"one\ntwo", 2),
        (b"one\ntwo\n", 2),
        (b"\n\n", 2),
    ],
)
def test_count_lines_handles_the_trailing_newline(data: bytes, expected: int) -> None:
    assert count_lines(data) == expected


def test_count_lines_does_not_decode() -> None:
    """Log archives carry raw bytes; a stray 0x80 must not raise."""
    assert count_lines(b"caf\xe9\nrunner\n") == 2


def test_whole_job_files_are_counted_per_job() -> None:
    archive = _zip(
        {
            "1_build (3.12).txt": b"a\nb\nc\n",
            "2_lint.txt": b"only one line\n",
        }
    )
    assert job_log_line_counts(archive) == {"build-3-12": 3, "lint": 1}


def test_step_files_are_summed_when_no_whole_job_file_exists() -> None:
    archive = _zip(
        {
            "build/1_Set up job.txt": b"a\nb\n",
            "build/2_Run tests.txt": b"c\nd\ne\n",
        }
    )
    assert job_log_line_counts(archive) == {"build": 5}


def test_whole_job_file_wins_over_the_sum_of_its_steps() -> None:
    """The same lines appear in both layouts; summing them would double-count."""
    archive = _zip(
        {
            "1_build.txt": b"a\nb\nc\nd\n",
            "build/1_Set up job.txt": b"a\nb\n",
            "build/2_Run tests.txt": b"c\nd\n",
        }
    )
    assert job_log_line_counts(archive) == {"build": 4}


def test_non_text_entries_are_ignored() -> None:
    archive = _zip({"1_build.txt": b"a\n", "metadata.json": b"{}\n", "notes.md": b"x\n"})
    assert job_log_line_counts(archive) == {"build": 1}


def test_traversal_entries_are_ignored() -> None:
    archive = _zip({"../escape.txt": b"a\nb\n", "1_build.txt": b"a\n"})
    assert job_log_line_counts(archive) == {"build": 1}


def test_empty_archive_yields_no_counts() -> None:
    assert job_log_line_counts(_zip({})) == {}


def test_unreadable_archive_raises_a_typed_error() -> None:
    with pytest.raises(LogArchiveError):
        job_log_line_counts(b"this is not a zip file")


def test_entry_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_archive, "MAX_ENTRIES", 2)
    archive = _zip({f"{i}_job.txt": b"line\n" for i in range(3)})
    with pytest.raises(LogArchiveError, match="entries"):
        job_log_line_counts(archive)


def test_expansion_is_bounded_by_bytes_actually_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zip bomb declares a small size and expands anyway, so the real
    guard has to count what came out, not what the header promised."""
    monkeypatch.setattr(log_archive, "MAX_UNCOMPRESSED_BYTES", 100)
    archive = _zip({"1_build.txt": b"x\n" * 500})
    with pytest.raises(LogArchiveError):
        job_log_line_counts(archive)


@pytest.mark.parametrize(
    ("job_name", "archive_entry"),
    [
        (
            "benchmarks walltime (colour_science|static_frame)",
            "1_benchmarks walltime (colour_sciencestatic_frame).txt",
        ),
        (
            "benchmarks walltime (pydantic|freqtrade|multithreaded|altair)",
            "2_benchmarks walltime (pydanticfreqtrademultithreadedaltair).txt",
        ),
        ("cargo test (linux, release)", "17_cargo test (linux, release).txt"),
        (
            "benchmarks instrumented ty (memory constraint_set)",
            "11_benchmarks instrumented ty (memory constraint_set).txt",
        ),
    ],
)
def test_real_github_names_match_their_archive_entries(job_name: str, archive_entry: str) -> None:
    """Names taken verbatim from an astral-sh/ruff run archive.

    GitHub deletes filename-illegal characters rather than substituting them,
    so a slug that treats them as separators produces a hyphen the entry does
    not have. Every one of these silently counted zero lines before the fix.
    """
    assert slug(job_name) == slug(PurePosixPath(archive_entry).stem)
