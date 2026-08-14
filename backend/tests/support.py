"""Test-database guards, kept out of conftest so they can be imported and tested.

`conftest.py` is loaded by pytest as a plugin. Importing it again by name from
a test module produces a second copy of the module and re-runs its top-level
code, so anything a test needs to call directly lives here instead.
"""

from sqlalchemy.engine import make_url

TEST_DATABASE_SUFFIX = "_test"


def derive_test_database_url(url: str) -> str:
    """Return the `_test` sibling of a database URL, idempotently."""
    parsed = make_url(url)
    name = parsed.database or ""
    if name.endswith(TEST_DATABASE_SUFFIX):
        return url
    return parsed.set(database=f"{name}{TEST_DATABASE_SUFFIX}").render_as_string(
        hide_password=False
    )


def require_test_database(url: str) -> None:
    """Raise unless `url` names a database the suite is allowed to truncate.

    A backstop, not the mechanism: the redirect in `conftest.py` is what points
    the suite at the right database. This exists so a future change that
    bypasses or breaks the redirect fails loudly instead of silently deleting
    real data, which is exactly how a full ingest was lost once.
    """
    name = make_url(url).database or ""
    if not name.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            f"Refusing to truncate {name!r}: integration tests only run against a "
            f"database whose name ends in {TEST_DATABASE_SUFFIX!r}."
        )
