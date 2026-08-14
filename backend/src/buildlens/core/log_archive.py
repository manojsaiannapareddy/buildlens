"""Reading GitHub's run-log archive (spec §7.3: pure, no I/O, no DB).

GitHub returns one zip per run. Its top level holds a whole-job log per job
(`3_build (3.12).txt`) and, beside those, a directory per job holding one file
per step (`build (3.12)/2_Run tests.txt`). Neither layout is documented as
stable, so we read both and prefer the whole-job file when present.

Job names do not survive the round trip byte-for-byte — spaces, slashes and
parentheses are rewritten — so entries are matched to jobs by slug rather than
by equality.

Everything here is a pure function over bytes, which is the point: this is the
layer most likely to hide a subtle bug and the cheapest one to test
exhaustively.
"""

import io
import re
import zipfile
from pathlib import PurePosixPath

import structlog

logger = structlog.get_logger()

# An archive from a legitimate run has one file per job plus a handful per
# step. Anything past these bounds is malformed or hostile, and we are about
# to hold the decompressed result in memory (§15).
MAX_ENTRIES = 5_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

_LEADING_INDEX = re.compile(r"^\d+_")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ILLEGAL_IN_FILENAME = re.compile(r'[<>:"/\\|?*]')


class LogArchiveError(Exception):
    """The archive could not be read or exceeded its safety bounds."""


def slug(name: str) -> str:
    """Collapse a job or file name to a comparable key.

    `3_Build (3.12).txt` and `Build (3.12)` both reduce to `build-3-12`.

    Illegal filename characters are removed before the collapse, not after:
    GitHub deletes them, so treating them as separators would put a hyphen
    where the archive entry has nothing and the two would never match.
    """
    stem = _LEADING_INDEX.sub("", name.strip())
    stem = _ILLEGAL_IN_FILENAME.sub("", stem)
    return _NON_ALNUM.sub("-", stem.lower()).strip("-")


def count_lines(data: bytes) -> int:
    """Count lines without decoding — log archives carry arbitrary bytes."""
    if not data:
        return 0
    newlines = data.count(b"\n")
    return newlines if data.endswith(b"\n") else newlines + 1


def job_log_line_counts(archive: bytes) -> dict[str, int]:
    """Map each job's slug to the number of log lines it produced.

    Whole-job files win over the sum of their step files: the same lines
    appear in both, and the whole-job file also carries output emitted
    between steps.
    """
    whole_job: dict[str, int] = {}
    by_step: dict[str, int] = {}
    decompressed = 0

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES:
                raise LogArchiveError(f"Archive has {len(entries)} entries; refusing to read")
            declared = sum(entry.file_size for entry in entries)
            if declared > MAX_UNCOMPRESSED_BYTES:
                raise LogArchiveError(f"Archive declares {declared} bytes; refusing to read")

            for entry in entries:
                if entry.is_dir():
                    continue
                path = PurePosixPath(entry.filename)
                if path.suffix.lower() != ".txt" or ".." in path.parts:
                    continue

                # The declared sizes above are self-reported and can lie, so
                # the real bound is enforced against what we actually read.
                data = bundle.read(entry)
                decompressed += len(data)
                if decompressed > MAX_UNCOMPRESSED_BYTES:
                    raise LogArchiveError("Archive expanded past the size limit")

                lines = count_lines(data)
                if len(path.parts) == 1:
                    whole_job[slug(path.stem)] = lines
                else:
                    key = slug(path.parts[0])
                    by_step[key] = by_step.get(key, 0) + lines
    except zipfile.BadZipFile as exc:
        raise LogArchiveError("Run log archive is not a readable zip") from exc

    counts = by_step | whole_job
    logger.info("log_archive.read", jobs=len(counts), bytes=decompressed)
    return counts
