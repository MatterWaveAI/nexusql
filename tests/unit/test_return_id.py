"""
execute(..., return_id=True): one call that returns the generated key.

The SQLite tests exercise the real driver path without a server. The MySQL
and SQL Server branches read a driver-specific value (pymysql's lastrowid,
pyodbc's SCOPE_IDENTITY() in the same batch), so they are checked here
against fake connections that behave like those drivers; the live backends
are covered by tests/integration/test_return_id.py when reachable.
"""

import sqlite3

import pytest

from nexusql import ConnectionConfig, DatabaseManager
from nexusql.manager import (
    first_value,
    insert_target_table,
    is_insert_statement,
    single_primary_key,
    strip_trailing_semicolon,
    with_returning,
)


@pytest.fixture
def db(temp_db_path):
    url = f"sqlite:///{temp_db_path}"
    manager = DatabaseManager(url)
    assert manager.connect(), "SQLite connect failed"
    manager.execute(
        "CREATE TABLE items (id SERIAL PRIMARY KEY, name TEXT UNIQUE)"
    )
    yield manager, url
    try:
        manager.disconnect()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SQLite: the real driver path
# ---------------------------------------------------------------------------

class TestSqlite:
    def test_returns_generated_id(self, db):
        manager, _ = db
        first = manager.execute(
            "INSERT INTO items (name) VALUES (:name)", {"name": "a"}, return_id=True
        )
        second = manager.execute(
            "INSERT INTO items (name) VALUES (:name)", {"name": "b"}, return_id=True
        )
        assert first == 1
        assert second == 2
        assert manager.fetch_one("SELECT name FROM items WHERE id = :id", {"id": 2}) == {
            "name": "b"
        }

    def test_insert_is_committed(self, db):
        manager, url = db
        new_id = manager.execute(
            "INSERT INTO items (name) VALUES (:name)", {"name": "durable"}, return_id=True
        )
        reader = DatabaseManager(url)
        assert reader.connect()
        try:
            rows = reader.execute("SELECT id, name FROM items")
        finally:
            reader.disconnect()
        assert rows == [{"id": new_id, "name": "durable"}]

    def test_explicit_returning_wins(self, db):
        """A caller-supplied RETURNING names the column; its value is returned."""
        manager, _ = db
        value = manager.execute(
            "INSERT INTO items (name) VALUES (:name) RETURNING name",
            {"name": "returned"},
            return_id=True,
        )
        assert value == "returned"

    def test_inside_transaction_defers_commit(self, db):
        manager, url = db
        with manager.transaction():
            new_id = manager.execute(
                "INSERT INTO items (name) VALUES (:name)", {"name": "tx"}, return_id=True
            )
            assert new_id == 1
            # Not yet visible to another connection: the block owns the commit.
            reader = DatabaseManager(url)
            assert reader.connect()
            try:
                assert reader.execute("SELECT id FROM items") == []
            finally:
                reader.disconnect()

        reader = DatabaseManager(url)
        assert reader.connect()
        try:
            assert reader.execute("SELECT id FROM items") == [{"id": 1}]
        finally:
            reader.disconnect()

    def test_nothing_inserted_returns_none(self, db):
        manager, _ = db
        manager.execute("INSERT INTO items (name) VALUES (:name)", {"name": "dup"})
        skipped = manager.execute(
            "INSERT OR IGNORE INTO items (name) VALUES (:name)", {"name": "dup"},
            return_id=True,
        )
        assert skipped is None

    def test_non_insert_rejected(self, db):
        manager, _ = db
        with pytest.raises(ValueError, match="only applies to INSERT"):
            manager.execute("UPDATE items SET name = :n", {"n": "x"}, return_id=True)
        with pytest.raises(ValueError, match="only applies to INSERT"):
            manager.execute("SELECT 1", return_id=True)

    def test_failure_rolls_back_and_connection_survives(self, db):
        manager, _ = db
        manager.execute("INSERT INTO items (name) VALUES (:name)", {"name": "taken"})
        with pytest.raises(sqlite3.IntegrityError):
            manager.execute(
                "INSERT INTO items (name) VALUES (:name)", {"name": "taken"},
                return_id=True,
            )
        assert manager.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 1}

    async def test_execute_async_passes_return_id(self, db):
        manager, _ = db
        new_id = await manager.execute_async(
            "INSERT INTO items (name) VALUES (:name)", {"name": "async"}, return_id=True
        )
        assert new_id == 1

    def test_default_behaviour_unchanged(self, db):
        """Without the flag an INSERT still returns the empty row list."""
        manager, _ = db
        assert manager.execute(
            "INSERT INTO items (name) VALUES (:name)", {"name": "plain"}
        ) == []


# ---------------------------------------------------------------------------
# MySQL and SQL Server branches against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, *, lastrowid=None, rowcount=1, result_sets=None):
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self.executed = []
        # Each result set is (description, rows); None description means a
        # row-count-only result, as pyodbc reports for an INSERT in a batch.
        self._sets = list(result_sets or [(None, [])])
        self.description = self._sets[0][0]
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def nextset(self):
        self._sets.pop(0)
        if not self._sets:
            return False
        self.description = self._sets[0][0]
        return True

    def fetchone(self):
        rows = self._sets[0][1]
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._sets[0][1])

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def unconnected(db_type: str) -> DatabaseManager:
    """A manager with dialect config but no live connection."""
    urls = {
        "mysql": "mysql://u:p@localhost/db",
        "mssql": "mssql://u:p@localhost/db",
        "postgresql": "postgresql://u:p@localhost/db",
    }
    return DatabaseManager(ConnectionConfig.from_url(urls[db_type]))


class TestMysqlBranch:
    def test_reads_lastrowid(self):
        cursor = FakeCursor(lastrowid=42)
        conn = FakeConnection(cursor)
        new_id = unconnected("mysql")._execute_return_id(
            "INSERT INTO t (name) VALUES (:name)", {"name": "x"}, conn
        )
        assert new_id == 42
        assert cursor.executed == [("INSERT INTO t (name) VALUES (%s)", ("x",))]
        assert cursor.closed and conn.commits == 1

    def test_zero_means_no_auto_increment(self):
        """pymysql reports 0 when the INSERT generated no AUTO_INCREMENT value."""
        conn = FakeConnection(FakeCursor(lastrowid=0))
        assert unconnected("mysql")._execute_return_id(
            "INSERT INTO t (id) VALUES (:id)", {"id": 7}, conn
        ) is None

    def test_insert_ignore_conflict_returns_none(self):
        conn = FakeConnection(FakeCursor(lastrowid=5, rowcount=0))
        assert unconnected("mysql")._execute_return_id(
            "INSERT IGNORE INTO t (name) VALUES (:name)", {"name": "dup"}, conn
        ) is None


class TestMssqlBranch:
    def test_scope_identity_travels_in_the_same_batch(self):
        cursor = FakeCursor(result_sets=[(None, []), ([("", int)], [(99,)])])
        conn = FakeConnection(cursor)
        new_id = unconnected("mssql")._execute_return_id(
            "INSERT INTO t (name) VALUES (:name);", {"name": "x"}, conn
        )
        assert new_id == 99
        sql, params = cursor.executed[0]
        assert sql == (
            "INSERT INTO t (name) VALUES (?); "
            "SELECT CAST(SCOPE_IDENTITY() AS BIGINT)"
        )
        assert params == ("x",)
        assert conn.commits == 1

    def test_null_identity_returns_none(self):
        cursor = FakeCursor(result_sets=[(None, []), ([("", int)], [(None,)])])
        assert unconnected("mssql")._execute_return_id(
            "INSERT INTO t (name) VALUES (:name)", {"name": "x"}, FakeConnection(cursor)
        ) is None

    def test_output_clause_is_honoured(self):
        cursor = FakeCursor(result_sets=[([("id", int)], [(12,)])])
        new_id = unconnected("mssql")._execute_return_id(
            "INSERT INTO t (name) OUTPUT INSERTED.id VALUES (:name)", {"name": "x"},
            FakeConnection(cursor),
        )
        assert new_id == 12
        assert "SCOPE_IDENTITY" not in cursor.executed[0][0]


class TestPostgresBranch:
    def test_primary_key_is_looked_up_once_then_returned(self):
        """Catalog query first, then the INSERT with RETURNING appended."""
        class PgCursor(FakeCursor):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def execute(self, sql, params=None):
                self.executed.append((sql, params))
                self.calls += 1

            def fetchall(self):
                return [{"attname": "item_id"}]

            def fetchone(self):
                return {"item_id": 17}

        cursor = PgCursor()
        conn = FakeConnection(cursor)
        manager = unconnected("postgresql")

        first = manager._execute_return_id(
            "INSERT INTO public.items (name) VALUES (:name)", {"name": "x"}, conn
        )
        second = manager._execute_return_id(
            "INSERT INTO public.items (name) VALUES (:name)", {"name": "y"}, conn
        )
        assert first == second == 17

        catalog_sql, catalog_params = cursor.executed[0]
        assert "pg_index" in catalog_sql and catalog_params == {"table": "public.items"}
        insert_sql, _ = cursor.executed[1]
        assert insert_sql.endswith('VALUES (%(name)s) RETURNING "item_id"')
        # Second insert reused the cached column: three statements, not four.
        assert len(cursor.executed) == 3
        assert manager._pk_cache == {"public.items": "item_id"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    @pytest.mark.parametrize(
        "query, table",
        [
            ("INSERT INTO users (a) VALUES (1)", "users"),
            ("  insert into app.users (a) VALUES (1)", "app.users"),
            ('INSERT INTO "Users" (a) VALUES (1)', '"Users"'),
            ('INSERT INTO "my schema"."Users" (a) VALUES (1)', '"my schema"."Users"'),
            ("INSERT OR REPLACE INTO t VALUES (1)", "t"),
            ("INSERT IGNORE INTO t VALUES (1)", "t"),
            ("REPLACE INTO t VALUES (1)", "t"),
            ("INSERT INTO t\n  (a)\nVALUES (1)", "t"),
        ],
    )
    def test_insert_target_table(self, query, table):
        assert is_insert_statement(query)
        assert insert_target_table(query) == table

    @pytest.mark.parametrize(
        "query", ["UPDATE t SET a = 1", "SELECT 1", "DELETE FROM t", "INSERTION INTO t"]
    )
    def test_non_inserts(self, query):
        assert not is_insert_statement(query)
        with pytest.raises(ValueError):
            insert_target_table(query)

    def test_single_primary_key(self):
        assert single_primary_key("t", ["id"]) == "id"
        with pytest.raises(ValueError, match="no primary key"):
            single_primary_key("t", [])
        with pytest.raises(ValueError, match="composite"):
            single_primary_key("t", ["a", "b"])

    def test_with_returning_handles_semicolon_and_quotes(self):
        assert with_returning("INSERT INTO t (a) VALUES (1);  ", "id") == (
            'INSERT INTO t (a) VALUES (1) RETURNING "id"'
        )
        assert with_returning("INSERT INTO t (a) VALUES (1)", 'we"ird') == (
            'INSERT INTO t (a) VALUES (1) RETURNING "we""ird"'
        )
        assert strip_trailing_semicolon("x;;\n") == "x"

    def test_first_value_across_row_shapes(self):
        assert first_value({"id": 3, "name": "n"}) == 3
        assert first_value((4, "n")) == 4
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        assert first_value(conn.execute("SELECT 5 AS id, 'n' AS name").fetchone()) == 5
