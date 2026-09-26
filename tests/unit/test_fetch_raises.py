"""
Regression tests for B-003: fetch_one/fetch_all must raise on failure.

Both methods used to catch every exception, roll back, and return None / []
— a mid-query failure was indistinguishable from "no rows". Now None and []
mean strictly "the query succeeded and matched nothing"; errors propagate.
The rollback still runs before the raise, so a failure does not poison the
connection (the B-002 guarantee).

The SQLite tests run without a server and share the exact fetch_one /
fetch_all code paths with the production PostgreSQL backend.
"""

import sqlite3

import pytest

from nexusql import DatabaseManager


@pytest.fixture
def sqlite_manager(temp_db_path):
    """Connected DatabaseManager on a fresh SQLite file with one row."""
    url = f"sqlite:///{temp_db_path}"
    db = DatabaseManager(url)
    assert db.connect(), "SQLite connect failed"
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute(
        "INSERT INTO items (id, name) VALUES (:id, :name)", {"id": 1, "name": "n1"}
    )
    yield db
    try:
        db.disconnect()
    except Exception:
        pass


class TestFetchErrorsRaise:
    def test_fetch_one_missing_table_raises(self, sqlite_manager):
        with pytest.raises(sqlite3.OperationalError):
            sqlite_manager.fetch_one(
                "SELECT * FROM b003_missing WHERE id = :id", {"id": 1}
            )

    def test_fetch_all_missing_table_raises(self, sqlite_manager):
        with pytest.raises(sqlite3.OperationalError):
            sqlite_manager.execute(
                "SELECT * FROM b003_missing WHERE id = :id", {"id": 1}
            )

    def test_fetch_one_syntax_error_raises(self, sqlite_manager):
        with pytest.raises(sqlite3.OperationalError):
            sqlite_manager.fetch_one("THIS IS NOT SQL")

    def test_fetch_all_syntax_error_raises(self, sqlite_manager):
        with pytest.raises(sqlite3.OperationalError):
            sqlite_manager.execute("THIS IS NOT SQL")


class TestNoRowsStillMeansNoneAndEmpty:
    def test_fetch_one_no_match_returns_none(self, sqlite_manager):
        result = sqlite_manager.fetch_one(
            "SELECT * FROM items WHERE id = :id", {"id": 999}
        )
        assert result is None

    def test_fetch_all_no_match_returns_empty_list(self, sqlite_manager):
        result = sqlite_manager.execute(
            "SELECT * FROM items WHERE id = :id", {"id": 999}
        )
        assert result == []


class TestConnectionUsableAfterRaisedError:
    def test_connection_usable_after_fetch_one_error(self, sqlite_manager):
        with pytest.raises(sqlite3.OperationalError):
            sqlite_manager.fetch_one("SELECT * FROM b003_missing")
        assert sqlite_manager.fetch_one("SELECT * FROM items") == {
            "id": 1,
            "name": "n1",
        }

    def test_connection_usable_after_fetch_all_error(self, sqlite_manager):
        with pytest.raises(sqlite3.OperationalError):
            sqlite_manager.execute("SELECT * FROM b003_missing")
        assert sqlite_manager.execute("SELECT * FROM items") == [
            {"id": 1, "name": "n1"}
        ]
