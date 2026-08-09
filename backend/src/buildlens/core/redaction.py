"""Secret redaction (FR-6, spec §15.1).

Runs before anything derived is stored or indexed. A secret that reaches
`log_chunks` is a secret in the vector index, in the prompt, and in the
diagnosis — redaction after the fact is not redaction.

Two rules shape this module:

* **Redaction never changes line structure.** Citations are line ranges into
  the raw log (FR-5), so a replacement that swallowed or added a newline would
  silently shift every citation after it. Patterns therefore run per line, and
  PEM blocks — the one secret that legitimately spans lines — are handled by a
  small state machine rather than a multiline regex.
* **Counts are the only telemetry.** Callers get how many of each pattern
  fired, never what matched. A spike in the counter means a repository is
  printing credentials into its build output, which is worth an alarm; the
  value itself is worth nothing and costs everything.

The pattern set is versioned. Changing it changes what derived text looks
like, so `REDACTION_VERSION` is stamped alongside anything produced from it
and a bump means the affected runs need reprocessing.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Final

REDACTION_VERSION: Final = 1

# A candidate has to be long enough and mixed enough to be a credential rather
# than a word. 4.0 bits/char sits above English prose and below base64 noise.
_ENTROPY_MIN_LENGTH: Final = 24
_ENTROPY_THRESHOLD: Final = 4.0


@dataclass(frozen=True, slots=True)
class Pattern:
    name: str
    regex: re.Pattern[str]
    group: int = 0


def _placeholder(name: str) -> str:
    return f"[REDACTED:{name}]"


# Ordered: the specific formats run before the generic entropy sweep, so a
# recognized token is reported as what it is rather than as "high entropy".
PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,255}\b")),
    Pattern("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b")),
    Pattern(
        "aws_access_key_id",
        re.compile(
            r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[0-9A-Z]{16}\b"
        ),
    ),
    Pattern(
        "aws_secret_access_key",
        re.compile(
            r"(?i)\baws_secret_access_key\b\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})",
        ),
        group=1,
    ),
    Pattern("slack_token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}\b")),
    Pattern("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Pattern("private_key_line", re.compile(r"-{5}BEGIN[A-Z ]*PRIVATE KEY-{5}")),
    Pattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    Pattern(
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd|auth)\b"
            r"\s*[:=]\s*[\"']?([^\s\"']{8,})",
        ),
        group=1,
    ),
)

_PEM_BEGIN = re.compile(r"-{5}BEGIN[A-Z ]*PRIVATE KEY-{5}")
_PEM_END = re.compile(r"-{5}END[A-Z ]*PRIVATE KEY-{5}")
_ENTROPY_CANDIDATE = re.compile(rf"[A-Za-z0-9+/_-]{{{_ENTROPY_MIN_LENGTH},}}={{0,2}}")
_ALL_HEX = re.compile(r"(?i)\A[0-9a-f]+\Z")
_ALL_DIGITS = re.compile(r"\A[0-9]+\Z")


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _looks_like_a_digest(value: str) -> bool:
    """Commit SHAs, image digests and checksums are high-entropy and useful.

    Redacting them would strip exactly the identifiers a diagnosis needs to
    point at, so hex-only and digit-only runs are exempt.
    """
    return bool(_ALL_HEX.match(value) or _ALL_DIGITS.match(value))


def _redact_high_entropy(line: str, counts: Counter[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        if _looks_like_a_digest(value) or shannon_entropy(value) < _ENTROPY_THRESHOLD:
            return value
        counts["high_entropy"] += 1
        return _placeholder("high_entropy")

    return _ENTROPY_CANDIDATE.sub(replace, line)


def redact_line(line: str, counts: Counter[str]) -> str:
    """Apply every pattern to one line, tallying into `counts`."""
    for pattern in PATTERNS:

        def replace(
            match: re.Match[str], name: str = pattern.name, group: int = pattern.group
        ) -> str:
            counts[name] += 1
            if group == 0:
                return _placeholder(name)
            # Keep the key, drop the value: `api_key=[REDACTED:...]` stays
            # readable, and the surrounding text is often the useful evidence.
            whole, secret = match.group(0), match.group(group)
            return whole.replace(secret, _placeholder(name))

        line = pattern.regex.sub(replace, line)
    return _redact_high_entropy(line, counts)


def redact_lines(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    """Redact a whole log, returning the same number of lines it was given.

    PEM bodies are tracked across lines because the base64 between the BEGIN
    and END markers carries no signature of its own — without the state
    machine it reads as ordinary high-entropy noise, and only most of it would
    go.
    """
    counts: Counter[str] = Counter()
    output: list[str] = []
    in_private_key = False

    for line in lines:
        if in_private_key:
            if _PEM_END.search(line):
                in_private_key = False
                output.append(line)
            else:
                counts["private_key_body"] += 1
                output.append(_placeholder("private_key_body"))
            continue

        if _PEM_BEGIN.search(line):
            in_private_key = True

        output.append(redact_line(line, counts))

    return output, dict(counts)
