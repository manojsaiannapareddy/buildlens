"""Log normalization (FR-5, spec §7.3).

Pure functions from raw archive bytes to normalized, redacted lines. No I/O,
no database, no clock. That is deliberate: this is the surface most likely to
hide a subtle bug and the cheapest one to test exhaustively, so it is kept
free of anything that would make a test need a fixture.

The invariant everything downstream depends on: **every normalized line
carries the 1-based line number it had in the raw log.** Citations are
validated against the raw view (FR-17), so a normalizer that renumbers lines
turns every citation into a lie. Lines may be dropped; the numbers on the
survivors never move.

Order of operations matters and is not arbitrary:

1. Decode, replacing undecodable bytes rather than failing. A build that
   prints a broken UTF-8 sequence is a build we still have to diagnose.
2. Strip ANSI. Doing this first stops an escape sequence from hiding the
   timestamp prefix from step 3, or from splitting a secret so that step 5
   cannot see it whole.
3. Lift the per-line timestamp GitHub prefixes onto every line into a
   structured field, out of the text the LLM reads.
4. Collapse `\\r` progress frames to the frame a terminal would have left
   showing. A pip download bar is hundreds of frames of the same line.
5. Redact, before any of this is stored or indexed (FR-6).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog

from buildlens.core.redaction import REDACTION_VERSION, redact_lines

logger = structlog.get_logger()

NORMALIZER_VERSION: Final = 1

# CSI (colour, cursor moves), OSC (window titles, hyperlinks), and the
# two-character escapes. Everything a runner emits falls into one of these.
_ANSI = re.compile(
    r"""
    \x1b \[ [0-?]* [ -/]* [@-~]          # CSI ... final byte
    | \x1b \] .*? (?: \x07 | \x1b\\ )    # OSC ... BEL or ST
    | \x1b [@-Z\\-_]                     # two-character escape
    """,
    re.VERBOSE,
)

# Remaining C0 controls, minus tab (meaningful indentation) and the newline
# and carriage return handled by the line splitter above.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# GitHub prefixes every line: 2026-08-03T18:44:31.8445698Z. The fraction is
# .NET-style seven digits, which datetime.fromisoformat rejects, so it is
# parsed here rather than handed off.
_TIMESTAMP = re.compile(r"\A(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z\s?")


@dataclass(frozen=True, slots=True)
class NormalizedLine:
    """One surviving line, still pointing at where it came from."""

    number: int
    timestamp: datetime | None
    text: str


@dataclass(frozen=True, slots=True)
class NormalizedLog:
    lines: tuple[NormalizedLine, ...]
    raw_line_count: int
    redactions: Mapping[str, int]
    normalizer_version: int = NORMALIZER_VERSION
    redaction_version: int = REDACTION_VERSION

    def to_text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def strip_ansi(text: str) -> str:
    """Remove escape sequences and stray control bytes."""
    return _CONTROL.sub("", _ANSI.sub("", text))


def split_timestamp(text: str) -> tuple[datetime | None, str]:
    """Lift a leading RFC 3339 timestamp off a line."""
    match = _TIMESTAMP.match(text)
    if match is None:
        return None, text

    date, clock, fraction = match.groups()
    # Truncate rather than round: the fraction is a log timestamp, and six
    # digits is the most a datetime can hold.
    micros = (fraction or "").ljust(6, "0")[:6]
    try:
        stamp = datetime.fromisoformat(f"{date}T{clock}.{micros}").replace(tzinfo=UTC)
    except ValueError:
        return None, text
    return stamp, text[match.end() :]


def collapse_progress_frames(text: str) -> str:
    """Keep the frame a terminal would have left on screen.

    A carriage return returns the cursor to column zero, so everything before
    the last one was overwritten in place and was never visible. Trailing
    empty frames are ignored so a line ending in `\\r` keeps its content.
    """
    if "\r" not in text:
        return text
    frames = [frame for frame in text.split("\r") if frame]
    return frames[-1] if frames else ""


def _split_lines(text: str) -> list[str]:
    """Split on newlines only.

    `str.splitlines` also breaks on form feed, vertical tab, and a handful of
    Unicode separators, any of which a build can print. Splitting on those
    would shift every line number after them.
    """
    return text.replace("\r\n", "\n").split("\n")


def normalize(raw: bytes | str) -> NormalizedLog:
    """Run the pipeline over one job's raw log."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    raw_lines = _split_lines(text)

    numbers: list[int] = []
    timestamps: list[datetime | None] = []
    bodies: list[str] = []

    for index, raw_line in enumerate(raw_lines, start=1):
        stamp, body = split_timestamp(strip_ansi(raw_line))
        body = collapse_progress_frames(body).rstrip()
        if not body:
            continue
        numbers.append(index)
        timestamps.append(stamp)
        bodies.append(body)

    redacted, counts = redact_lines(bodies)

    lines = tuple(
        NormalizedLine(number=number, timestamp=stamp, text=body)
        for number, stamp, body in zip(numbers, timestamps, redacted, strict=True)
    )

    if counts:
        logger.info("normalization.redacted", **counts)

    return NormalizedLog(
        lines=lines,
        raw_line_count=len(raw_lines),
        redactions=counts,
    )


def line_index(lines: Sequence[NormalizedLine]) -> Mapping[int, str]:
    """Raw line number to normalized text, for citation validation (FR-17)."""
    return {line.number: line.text for line in lines}


def slice_lines(
    lines: Iterable[NormalizedLine], start: int, end: int
) -> tuple[NormalizedLine, ...]:
    """The lines a citation of raw range [start, end] actually resolves to."""
    return tuple(line for line in lines if start <= line.number <= end)
