"""The guards that keep the suite from truncating a real database."""

import pytest

from tests.support import derive_test_database_url, require_test_database

BASE = "postgresql+asyncpg://u:pw@localhost:5432/buildlens"


def test_derives_the_test_sibling() -> None:
    assert derive_test_database_url(BASE).endswith("/buildlens_test")


def test_derivation_is_idempotent() -> None:
    once = derive_test_database_url(BASE)
    assert derive_test_database_url(once) == once


def test_credentials_survive_derivation() -> None:
    """render_as_string masks the password by default, which would break the connection."""
    assert "pw" in derive_test_database_url(BASE)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:pw@localhost:5432/buildlens",
        "postgresql+asyncpg://u:pw@localhost:5432/postgres",
        "postgresql+asyncpg://u:pw@prod.example.com:5432/buildlens_prod",
    ],
)
def test_refuses_a_database_that_is_not_a_test_database(url: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing to truncate"):
        require_test_database(url)


def test_allows_a_test_database() -> None:
    require_test_database(derive_test_database_url(BASE))
