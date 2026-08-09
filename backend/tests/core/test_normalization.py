"""Normalization pipeline (FR-5). Pure functions — no database, no network."""

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from buildlens.core.normalization import (
    NormalizedLine,
    collapse_progress_frames,
    normalize,
    slice_lines,
    split_timestamp,
    strip_ansi,
)

# Bytes a runner can plausibly emit, including the escapes and controls the
# normalizer exists to remove.
_log_text = st.text(
    # codec="utf-8" excludes surrogates, which are not bytes a runner can emit.
    alphabet=st.characters(codec="utf-8", min_codepoint=1, max_codepoint=0x2FFF),
    max_size=400,
)

# --- FR-5: ANSI ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\x1b[31mfailed\x1b[0m", "failed"),
        ("\x1b[1;32;40mok\x1b[m", "ok"),
        ("\x1b[2Kcleared", "cleared"),
        ("\x1b]0;window title\x07shell", "shell"),
        ("\x1b]8;;http://example.com\x1b\\link\x1b]8;;\x1b\\", "link"),
        ("\x1bMreverse index", "reverse index"),
        ("plain text", "plain text"),
        ("keeps\ttabs", "keeps\ttabs"),
    ],
)
def test_fr5_strip_ansi_removes_escapes_and_keeps_content(raw: str, expected: str) -> None:
    assert strip_ansi(raw) == expected


@given(_log_text)
def test_fr5_no_escape_sequence_survives_normalization(text: str) -> None:
    for line in normalize(text).lines:
        assert "\x1b" not in line.text


# --- FR-5: timestamps ------------------------------------------------------


def test_fr5_timestamp_is_lifted_out_of_the_text() -> None:
    stamp, body = split_timestamp("2026-08-03T18:44:31.8445698Z Run pytest")

    assert stamp == datetime(2026, 8, 3, 18, 44, 31, 844569, tzinfo=UTC)
    assert body == "Run pytest"


def test_fr5_dotnet_seven_digit_fraction_is_truncated_not_rejected() -> None:
    """datetime.fromisoformat refuses seven digits; GitHub emits seven."""
    stamp, _ = split_timestamp("2026-08-03T18:44:31.1234567Z x")

    assert stamp is not None
    assert stamp.microsecond == 123456


@pytest.mark.parametrize(
    "line",
    [
        "no timestamp here",
        "2026-08-03 18:44:31 not rfc3339",
        "2026-13-45T99:99:99Z impossible",
        "prefixed 2026-08-03T18:44:31Z mid-line",
    ],
)
def test_fr5_non_timestamps_are_left_in_the_text(line: str) -> None:
    stamp, body = split_timestamp(line)

    assert stamp is None
    assert body == line


# --- FR-5: progress frames -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10%\r50%\r100%", "100%"),
        ("downloading\r", "downloading"),
        ("\rstarted", "started"),
        ("no frames", "no frames"),
        ("\r\r\r", ""),
    ],
)
def test_fr5_only_the_visible_frame_survives(raw: str, expected: str) -> None:
    assert collapse_progress_frames(raw) == expected


def test_fr5_progress_spam_collapses_to_one_line() -> None:
    frames = "\r".join(f"Downloading [{'=' * n}>] {n * 10}%" for n in range(11))
    log = normalize(f"2026-08-03T18:44:31.0000000Z {frames}\nDone\n")

    assert [line.text for line in log.lines] == ["Downloading [==========>] 100%", "Done"]


# --- FR-5: line numbering --------------------------------------------------


def test_fr5_line_numbers_point_back_at_the_raw_log() -> None:
    log = normalize("first\n\n\x1b[31m\x1b[0m\nfourth\n")

    assert [(line.number, line.text) for line in log.lines] == [(1, "first"), (4, "fourth")]
    assert log.raw_line_count == 5


def test_fr5_blank_lines_are_dropped_without_shifting_the_rest() -> None:
    log = normalize("\n".join(["", "", "error: boom", ""]))

    assert [line.number for line in log.lines] == [3]


@given(_log_text)
@settings(max_examples=200)
def test_fr5_line_numbers_are_increasing_and_in_range(text: str) -> None:
    log = normalize(text)
    numbers = [line.number for line in log.lines]

    assert numbers == sorted(set(numbers))
    assert all(1 <= number <= log.raw_line_count for number in numbers)


@given(_log_text)
@settings(max_examples=200)
def test_fr5_every_normalized_line_maps_to_a_real_raw_line(text: str) -> None:
    """The property citation validation rests on."""
    raw_lines = text.replace("\r\n", "\n").split("\n")
    log = normalize(text)

    for line in log.lines:
        assert raw_lines[line.number - 1] != ""


@given(_log_text)
@settings(max_examples=200)
def test_fr5_normalization_is_idempotent(text: str) -> None:
    once = normalize(text)
    twice = normalize(once.to_text())

    assert [line.text for line in twice.lines] == [line.text for line in once.lines]


@given(_log_text)
@settings(max_examples=200)
def test_fr5_output_lines_never_contain_line_breaks(text: str) -> None:
    """A line that grew a newline would split one citation into two."""
    for line in normalize(text).lines:
        assert "\n" not in line.text
        assert "\r" not in line.text


def test_fr5_undecodable_bytes_do_not_stop_ingestion() -> None:
    log = normalize(b"before\n\xff\xfe broken utf8\nafter\n")

    assert log.raw_line_count == 4
    assert log.lines[0].text == "before"
    assert log.lines[-1].text == "after"


def test_fr5_form_feed_does_not_create_a_line() -> None:
    """str.splitlines breaks on \\f; doing so would shift every later number."""
    log = normalize("one\x0ctwo\nthree\n")

    assert log.raw_line_count == 3
    assert log.lines[0] == NormalizedLine(number=1, timestamp=None, text="onetwo")


# --- citation mapping ------------------------------------------------------


def test_slice_lines_returns_the_lines_a_citation_resolves_to() -> None:
    log = normalize("a\n\n\nb\nc\n")

    assert [line.number for line in slice_lines(log.lines, 1, 4)] == [1, 4]
    assert slice_lines(log.lines, 2, 3) == ()
