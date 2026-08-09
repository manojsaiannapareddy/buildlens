"""Secret redaction (FR-6). Pure functions — no database, no network."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlens.core.normalization import normalize
from buildlens.core.redaction import redact_lines, shannon_entropy

SECRETS = {
    "github_token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "github_pat": "github_pat_11ABCDEFG0abcdefghijkl_" + "Z" * 40,
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "slack_token": "xoxb-000000000000-0000000000000-" + "x" * 24,
    "google_api_key": "AIza" + "SyD-1234567890abcdefghijklmnopqrstu",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
}


@pytest.mark.parametrize(("name", "secret"), sorted(SECRETS.items()))
def test_fr6_known_token_formats_are_redacted(name: str, secret: str) -> None:
    lines, counts = redact_lines([f"exporting token {secret} now"])

    assert secret not in lines[0]
    assert counts.get(name, 0) >= 1


def test_fr6_the_value_is_replaced_but_the_context_survives() -> None:
    """The surrounding text is often the evidence a diagnosis needs."""
    lines, _ = redact_lines(["fatal: could not read Password for 'https://github.com'"])

    assert "could not read" in lines[0]


def test_fr6_keyed_assignments_keep_the_key_and_lose_the_value() -> None:
    lines, counts = redact_lines(["API_KEY=hunter2hunter2hunter2"])

    assert lines[0].startswith("API_KEY=")
    assert "hunter2" not in lines[0]
    assert counts["credential_assignment"] == 1


def test_fr6_private_key_bodies_are_redacted_line_by_line() -> None:
    pem = [
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEAx7Zq3vN1p8Qw2FvKcYd0aB",
        "9sLmNpQrStUvWxYz0123456789abcdefghijkl",
        "-----END RSA PRIVATE KEY-----",
        "done",
    ]
    lines, counts = redact_lines(pem)

    assert counts["private_key_body"] == 2
    assert "MIIEow" not in "\n".join(lines)
    assert lines[-1] == "done"


def test_fr6_line_count_is_never_changed_by_redaction() -> None:
    """Citations are line ranges; a redaction that moved a line would
    invalidate every citation after it."""
    lines = [f"line {index} token ghp_{'x' * 36}" for index in range(50)]
    redacted, _ = redact_lines(lines)

    assert len(redacted) == len(lines)


def test_fr6_commit_shas_and_digests_survive() -> None:
    """High entropy, but they are exactly what a diagnosis points at."""
    sha = "9f2c1ab4de7856bc0139ac4d6f2e8b1a3c5d7e90"
    lines, counts = redact_lines([f"HEAD is now at {sha} fix flaky test"])

    assert sha in lines[0]
    assert counts == {}


def test_fr6_generic_high_entropy_strings_are_caught() -> None:
    lines, counts = redact_lines(["DEPLOY=k7Qm2Xz9PbR4tYw8Fj3LnVc6Hs1Ad5Ge0Ui"])

    assert counts
    assert "k7Qm2Xz9" not in lines[0]


def test_fr6_ordinary_log_output_is_left_alone() -> None:
    lines, counts = redact_lines(
        [
            "Running 42 tests with pytest",
            "FAILED tests/test_worker.py::test_lease_expiry - AssertionError",
            "  assert 3 == 4",
            "Installing collected packages: sqlalchemy, structlog",
        ]
    )

    assert counts == {}
    assert lines == [
        "Running 42 tests with pytest",
        "FAILED tests/test_worker.py::test_lease_expiry - AssertionError",
        "  assert 3 == 4",
        "Installing collected packages: sqlalchemy, structlog",
    ]


def test_fr6_counts_are_reported_and_values_are_not() -> None:
    secret = SECRETS["github_token"]
    _, counts = redact_lines([secret, secret])

    assert counts["github_token"] == 2
    assert secret not in str(counts)


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=200))
def test_fr6_prose_never_trips_the_entropy_sweep(text: str) -> None:
    assert shannon_entropy(text) < 5.0


@given(
    st.lists(
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=0x2FFF), max_size=120),
        max_size=40,
    )
)
def test_fr6_redaction_preserves_the_number_of_lines(lines: list[str]) -> None:
    redacted, _ = redact_lines(lines)

    assert len(redacted) == len(lines)


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_fr6_secrets_do_not_survive_the_full_pipeline(name: str) -> None:
    """Redaction runs inside normalize, not as an optional extra step."""
    secret = SECRETS[name]
    log = normalize(f"2026-08-03T18:44:31.0000000Z \x1b[31mtoken={secret}\x1b[0m\n")

    assert secret not in log.to_text()
    assert log.redactions
