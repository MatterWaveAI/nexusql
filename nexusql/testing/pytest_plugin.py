"""
pytest fixtures for suites built on NexusQL.

Loaded through the ``pytest11`` entry point when nexusql is installed, so
the fixtures are available without a conftest line. From a source checkout
that is not installed, load it with ``-p nexusql.testing.pytest_plugin`` or
``pytest_plugins = ["nexusql.testing.pytest_plugin"]`` in the root conftest
(one of the two, since pytest registers a plugin module once).

Fixtures:

- ``nexusql_sqlite``: a connected :class:`DatabaseManager` on a fresh SQLite
  file under ``tmp_path``.
- ``nexusql_db``: a connected manager on the URL from ``--nexusql-url``,
  else the ``NEXUSQL_TEST_URL`` environment variable, else a SQLite file
  under ``tmp_path``. A server URL that does not answer skips the test.
- ``nexusql_tx``: ``nexusql_db`` with the whole test inside one transaction
  that is rolled back afterwards.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

from ..interfaces import DatabaseType
from ..manager import DatabaseManager

URL_OPTION = "--nexusql-url"
URL_ENV_VAR = "NEXUSQL_TEST_URL"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("nexusql").addoption(
        URL_OPTION, action="store", default=None, dest="nexusql_url",
        metavar="URL",
        help=f"Database URL for the nexusql_db fixture (default: ${URL_ENV_VAR}, "
             "else a SQLite file in tmp_path)")


def _connect(url: str) -> DatabaseManager:
    db = DatabaseManager(url)
    if db.connect():
        return db
    if db.config.database_type is DatabaseType.SQLITE:
        # A local file that cannot be opened is a broken test setup, not an
        # absent service, so it fails rather than skips.
        pytest.fail(f"could not open SQLite database {url}")
    pytest.skip(f"{db.config.database_type.value} not reachable at {url.rsplit('@', 1)[-1]}")


@pytest.fixture
def nexusql_sqlite(tmp_path) -> Iterator[DatabaseManager]:
    """Connected DatabaseManager on a SQLite file that lives for one test."""
    db = _connect(f"sqlite:///{tmp_path / 'nexusql.db'}")
    yield db
    db.disconnect()


@pytest.fixture
def nexusql_url(request: pytest.FixtureRequest, tmp_path) -> str:
    """URL the nexusql_db fixture connects to: option, environment, then SQLite."""
    url = request.config.getoption("nexusql_url") or os.environ.get(URL_ENV_VAR)
    return url or f"sqlite:///{tmp_path / 'nexusql.db'}"


@pytest.fixture
def nexusql_db(nexusql_url: str) -> Iterator[DatabaseManager]:
    """Connected DatabaseManager on ``nexusql_url``; skips if the server is down."""
    db = _connect(nexusql_url)
    yield db
    db.disconnect()


class _RollBack(Exception):
    """Raised inside the fixture's transaction block so it rolls back."""


@pytest.fixture
def nexusql_tx(nexusql_db: DatabaseManager) -> Iterator[DatabaseManager]:
    """
    ``nexusql_db`` with the test run inside one transaction, rolled back at
    teardown, so rows the test writes never reach other tests.

    Only statements the backend treats as transactional are undone.
    PostgreSQL and SQL Server roll DDL back; MySQL commits implicitly on
    any DDL or TRUNCATE, and Python's sqlite3 only opens a transaction at
    the first INSERT/UPDATE/DELETE, so a CREATE TABLE issued before any
    write on SQLite stays. Create schema in a fixture that uses
    ``nexusql_db`` and drop it there, and keep the test itself to DML.
    """
    try:
        with nexusql_db.transaction():
            yield nexusql_db
            raise _RollBack
    except _RollBack:
        pass
