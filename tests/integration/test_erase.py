"""
DatabaseManager.erase() against live backends.

The same call must blank the named columns, leave every other column and
every other row intact, and report the matched row count on SQLite,
PostgreSQL, MySQL and SQL Server. Identifier quoting differs per backend
(backticks on MySQL), which is why this runs everywhere rather than only on
SQLite. Backends that are not reachable are skipped at collection.
"""

import uuid

import pytest

from nexusql import DatabaseManager

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


def table_name() -> str:
    return "erase_" + uuid.uuid4().hex[:10]


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} ("
        f"id SERIAL PRIMARY KEY, org INTEGER NOT NULL, "
        f"email VARCHAR(100), phone VARCHAR(30), notes VARCHAR(100) NOT NULL)"
    )
    for i in range(1, 5):
        db_manager.execute(
            f"INSERT INTO {name} (org, email, phone, notes) VALUES (:org, :email, :phone, :notes)",
            {"org": 1 if i <= 2 else 2, "email": f"p{i}@x.io", "phone": f"555-000{i}", "notes": f"n{i}"},
        )
    yield name
    try:
        db_manager.execute(f"DROP TABLE {name}")
    except Exception:
        pass


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestErase:
    def test_nulls_matching_rows_only(self, db_manager, table):
        count = db_manager.erase(table, "org = :org", {"org": 1}, columns=["email", "phone"])
        assert count == 2
        rows = db_manager.execute(f"SELECT id, email, phone, notes FROM {table} ORDER BY id")
        assert [(r["email"], r["phone"], r["notes"]) for r in rows] == [
            (None, None, "n1"),
            (None, None, "n2"),
            ("p3@x.io", "555-0003", "n3"),
            ("p4@x.io", "555-0004", "n4"),
        ]

    def test_tombstone_on_not_null_column(self, db_manager, table):
        count = db_manager.erase(table, "id = :id", {"id": 3}, columns=["notes", "email"], tombstone="[erased]")
        assert count == 1
        row = db_manager.fetch_one(f"SELECT email, phone, notes FROM {table} WHERE id = :id", {"id": 3})
        assert row == {"email": "[erased]", "phone": "555-0003", "notes": "[erased]"}

    def test_no_match_is_zero(self, db_manager, table):
        assert db_manager.erase(table, "id = :id", {"id": 999}, columns=["email"]) == 0

    def test_committed_for_other_connections(self, db_manager, table):
        assert db_manager.erase(table, "org = :org", {"org": 2}, columns=["email"]) == 2
        reader = DatabaseManager(db_manager.database_url)
        assert reader.connect()
        try:
            rows = reader.execute(f"SELECT email FROM {table} WHERE org = :org", {"org": 2})
        finally:
            reader.disconnect()
        assert [r["email"] for r in rows] == [None, None]

    def test_rolls_back_with_outer_transaction(self, db_manager, table):
        class Abort(Exception):
            pass

        with pytest.raises(Abort):
            with db_manager.transaction():
                assert db_manager.erase(table, "org = :org", {"org": 1}, columns=["email"]) == 2
                raise Abort
        row = db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table} WHERE email IS NULL")
        assert row["n"] == 0

    def test_bad_identifier_touches_nothing(self, db_manager, table):
        with pytest.raises(ValueError):
            db_manager.erase(f"{table}; DROP TABLE {table}", "id = :id", {"id": 1}, columns=["email"])
        assert db_manager.table_exists(table)
