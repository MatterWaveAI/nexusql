"""
Nested transaction() blocks as savepoints.

The SQLite tests exercise the real driver path without a server, including
the explicit BEGIN that SQLite needs when the nested block is the first
thing the outer block does. The PostgreSQL, MySQL and SQL Server statement
sequences are checked against fake connections shaped like those drivers;
the live backends are covered by tests/integration/test_savepoints.py when
reachable. The async backend is driven through a fake psycopg-shaped pool.
"""

import sqlite3

import pytest

from nexusql import (
    ConnectionConfig,
    DatabaseManager,
    DatabaseType,
    NESTED_MODES,
    SavepointSQL,
    savepoint_sql,
)
from nexusql.async_backend import AsyncPostgresBackend
from nexusql.savepoints import (
    MSSQL_ENSURE_TRANSACTION,
    savepoint_name,
    transaction_open,
    validate_nested,
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


def insert(manager, name):
    manager.execute("INSERT INTO items (name) VALUES (:name)", {"name": name})


def names(manager):
    return [r["name"] for r in manager.execute("SELECT name FROM items ORDER BY id")]


def names_elsewhere(url):
    """What a second connection sees: committed rows only."""
    reader = DatabaseManager(url)
    assert reader.connect()
    try:
        return names(reader)
    finally:
        reader.disconnect()


# ---------------------------------------------------------------------------
# SQLite: the real driver path
# ---------------------------------------------------------------------------

class TestSqlite:
    def test_inner_failure_leaves_outer_rows(self, db):
        manager, url = db
        with manager.transaction():
            insert(manager, "a")
            try:
                with manager.transaction():
                    insert(manager, "b")
                    insert(manager, "a")
            except sqlite3.IntegrityError:
                pass
            insert(manager, "c")
        assert names_elsewhere(url) == ["a", "c"]

    def test_inner_python_error_is_undone(self, db):
        manager, url = db
        with manager.transaction():
            insert(manager, "a")
            try:
                with manager.transaction():
                    insert(manager, "b")
                    raise ValueError("boom")
            except ValueError:
                pass
        assert names_elsewhere(url) == ["a"]

    def test_inner_exception_reaches_caller(self, db):
        manager, _ = db
        err = ValueError("boom")
        with manager.transaction():
            with pytest.raises(ValueError) as excinfo:
                with manager.transaction():
                    raise err
            assert excinfo.value is err

    def test_three_deep_nesting(self, db):
        manager, url = db
        with manager.transaction():
            insert(manager, "a")
            with manager.transaction():
                insert(manager, "b")
                try:
                    with manager.transaction():
                        assert manager._state.depth == 3
                        insert(manager, "c")
                        raise ValueError("boom")
                except ValueError:
                    pass
                insert(manager, "d")
        assert names_elsewhere(url) == ["a", "b", "d"]

    def test_uncaught_inner_failure_rolls_back_everything(self, db):
        """Outermost semantics are unchanged: an exception that reaches it rolls back."""
        manager, url = db
        with pytest.raises(ValueError):
            with manager.transaction():
                insert(manager, "a")
                with manager.transaction():
                    insert(manager, "b")
                    raise ValueError("boom")
        assert names_elsewhere(url) == []

    def test_nested_block_first_in_outer_still_rolls_back(self, db):
        """
        SQLite opens a transaction on SAVEPOINT if none is active, and
        releasing that savepoint commits. The nested block must not be able
        to commit the outer block's work.
        """
        manager, url = db
        with pytest.raises(ValueError):
            with manager.transaction():
                with manager.transaction():
                    insert(manager, "a")
                insert(manager, "b")
                raise ValueError("boom")
        assert names_elsewhere(url) == []

    def test_nested_block_first_in_outer_defers_commit(self, db):
        manager, url = db
        with manager.transaction():
            with manager.transaction():
                insert(manager, "a")
            assert names_elsewhere(url) == []
            insert(manager, "b")
        assert names_elsewhere(url) == ["a", "b"]

    def test_fetch_one_error_inside_savepoint_keeps_outer_rows(self, db):
        manager, url = db
        with manager.transaction():
            insert(manager, "a")
            try:
                with manager.transaction():
                    manager.fetch_one("SELECT * FROM no_such_table")
            except sqlite3.OperationalError:
                pass
        assert names_elsewhere(url) == ["a"]

    def test_join_mode_keeps_old_behaviour(self, db):
        """A joined block's rows stay when its exception is caught outside it."""
        manager, url = db
        with manager.transaction():
            insert(manager, "a")
            try:
                with manager.transaction(nested="join"):
                    insert(manager, "b")
                    raise ValueError("boom")
            except ValueError:
                pass
        assert names_elsewhere(url) == ["a", "b"]

    def test_join_mode_failed_statement_rolls_back_whole_transaction(self, db):
        """
        The previous behaviour, kept for nested="join": a failed statement
        rolls back the connection, taking the outer block's rows with it.
        """
        manager, url = db
        with manager.transaction():
            insert(manager, "a")
            try:
                with manager.transaction(nested="join"):
                    insert(manager, "a")
            except sqlite3.IntegrityError:
                pass
            insert(manager, "c")
        assert names_elsewhere(url) == ["c"]

    def test_outermost_join_is_a_plain_transaction(self, db):
        manager, url = db
        with manager.transaction(nested="join"):
            insert(manager, "a")
        assert names_elsewhere(url) == ["a"]

    def test_state_is_restored(self, db):
        manager, _ = db
        assert manager._in_savepoint is False
        with manager.transaction():
            with manager.transaction():
                assert manager._in_savepoint is True
                assert manager._state.depth == 2
            assert manager._in_savepoint is False
            assert manager._state.depth == 1
        assert manager._in_transaction is False
        assert manager._state.depth == 0

    def test_state_is_restored_after_failure(self, db):
        manager, url = db
        with pytest.raises(ValueError):
            with manager.transaction():
                with manager.transaction():
                    raise ValueError("boom")
        assert manager._in_transaction is False
        assert manager._in_savepoint is False
        insert(manager, "after")
        assert names_elsewhere(url) == ["after"]

    def test_invalid_mode_rejected(self, db):
        manager, _ = db
        with pytest.raises(ValueError, match="nested="):
            with manager.transaction(nested="nope"):
                pass
        assert manager._in_transaction is False


# ---------------------------------------------------------------------------
# Statement construction per dialect
# ---------------------------------------------------------------------------

class TestStatements:
    @pytest.mark.parametrize("db_type", [
        DatabaseType.SQLITE, DatabaseType.POSTGRESQL, DatabaseType.MYSQL,
    ])
    def test_standard_dialects(self, db_type):
        sql = savepoint_sql(db_type, "sp_2")
        assert sql == SavepointSQL(
            name="sp_2",
            begin=["SAVEPOINT sp_2"],
            release=["RELEASE SAVEPOINT sp_2"],
            rollback=["ROLLBACK TO SAVEPOINT sp_2"],
        )

    def test_sqlite_begins_when_no_transaction_is_open(self):
        sql = savepoint_sql(DatabaseType.SQLITE, "sp_2", transaction_open=False)
        assert sql.begin == ["BEGIN", "SAVEPOINT sp_2"]

    def test_only_sqlite_begins(self):
        for db_type in (DatabaseType.POSTGRESQL, DatabaseType.MYSQL):
            sql = savepoint_sql(db_type, "sp_2", transaction_open=False)
            assert sql.begin == ["SAVEPOINT sp_2"]

    def test_mssql_dialect(self):
        sql = savepoint_sql(DatabaseType.MSSQL, "sp_2", transaction_open=False)
        assert sql.begin == [MSSQL_ENSURE_TRANSACTION, "SAVE TRANSACTION sp_2"]
        assert sql.release == []
        assert sql.rollback == ["ROLLBACK TRANSACTION sp_2"]
        assert "BEGIN TRANSACTION" not in MSSQL_ENSURE_TRANSACTION

    def test_names_follow_depth(self):
        assert savepoint_name(2) == "sp_2"
        assert savepoint_name(3) == "sp_3"

    def test_validate_nested(self):
        assert NESTED_MODES == ("savepoint", "join")
        for mode in NESTED_MODES:
            assert validate_nested(mode) == mode
        with pytest.raises(ValueError):
            validate_nested("nope")

    def test_transaction_open(self):
        conn = sqlite3.connect(":memory:")
        assert transaction_open(DatabaseType.SQLITE, conn) is False
        conn.execute("BEGIN")
        assert transaction_open(DatabaseType.SQLITE, conn) is True
        conn.close()
        assert transaction_open(DatabaseType.POSTGRESQL, object()) is True
        assert transaction_open(DatabaseType.MSSQL, object()) is True


# ---------------------------------------------------------------------------
# Server dialects against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._conn.log.append(sql)
        if self._conn.fail_on and sql.startswith(self._conn.fail_on):
            raise RuntimeError(f"fake driver error on {sql!r}")

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConnection:
    """Records every statement, commit and rollback in order."""

    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on
        self.closed = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.log.append("<commit>")

    def rollback(self):
        self.log.append("<rollback>")


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.released = []

    def acquire(self, timeout=None):
        return self.conn

    def release(self, conn, *, discard=False):
        self.released.append((conn, discard))


def fake_manager(db_type: str, fail_on=None):
    urls = {
        "mysql": "mysql://u:p@localhost/db",
        "mssql": "mssql://u:p@localhost/db",
        "postgresql": "postgresql://u:p@localhost/db",
    }
    manager = DatabaseManager(ConnectionConfig.from_url(urls[db_type]))
    conn = FakeConnection(fail_on=fail_on)
    manager._pool = FakePool(conn)
    return manager, conn


def write(manager):
    manager.execute("INSERT INTO t (v) VALUES (:v)", {"v": 1})


class TestMssqlBranch:
    def test_rollback_sequence(self):
        manager, conn = fake_manager("mssql")
        with manager.transaction():
            with pytest.raises(ValueError):
                with manager.transaction():
                    write(manager)
                    raise ValueError("boom")
        assert conn.log == [
            MSSQL_ENSURE_TRANSACTION,
            "SAVE TRANSACTION sp_2",
            "INSERT INTO t (v) VALUES (?)",
            "ROLLBACK TRANSACTION sp_2",
            "<commit>",
        ]

    def test_clean_exit_has_no_release(self):
        manager, conn = fake_manager("mssql")
        with manager.transaction():
            with manager.transaction():
                write(manager)
        assert conn.log == [
            MSSQL_ENSURE_TRANSACTION,
            "SAVE TRANSACTION sp_2",
            "INSERT INTO t (v) VALUES (?)",
            "<commit>",
        ]
        assert not any("RELEASE" in s for s in conn.log)

    def test_failed_statement_does_not_roll_back_connection(self):
        manager, conn = fake_manager("mssql", fail_on="INSERT")
        with manager.transaction():
            with pytest.raises(RuntimeError, match="fake driver error"):
                with manager.transaction():
                    write(manager)
        assert "<rollback>" not in conn.log
        assert conn.log[-2:] == ["ROLLBACK TRANSACTION sp_2", "<commit>"]


class TestMysqlBranch:
    def test_clean_exit_releases(self):
        manager, conn = fake_manager("mysql")
        with manager.transaction():
            with manager.transaction():
                write(manager)
        assert conn.log == [
            "SAVEPOINT sp_2",
            "INSERT INTO t (v) VALUES (%s)",
            "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]

    def test_failure_rolls_back_to_savepoint_then_releases(self):
        manager, conn = fake_manager("mysql")
        with manager.transaction():
            with pytest.raises(ValueError):
                with manager.transaction():
                    raise ValueError("boom")
        assert conn.log == [
            "SAVEPOINT sp_2",
            "ROLLBACK TO SAVEPOINT sp_2",
            "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]


class TestPostgresBranch:
    def test_three_deep_names(self):
        manager, conn = fake_manager("postgresql")
        with manager.transaction():
            with manager.transaction():
                with pytest.raises(ValueError):
                    with manager.transaction():
                        raise ValueError("boom")
        assert conn.log == [
            "SAVEPOINT sp_2",
            "SAVEPOINT sp_3",
            "ROLLBACK TO SAVEPOINT sp_3",
            "RELEASE SAVEPOINT sp_3",
            "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]

    def test_failed_statement_inside_savepoint_skips_connection_rollback(self):
        manager, conn = fake_manager("postgresql", fail_on="INSERT")
        with manager.transaction():
            with pytest.raises(RuntimeError):
                with manager.transaction():
                    write(manager)
        assert conn.log == [
            "SAVEPOINT sp_2",
            "INSERT INTO t (v) VALUES (%(v)s)",
            "ROLLBACK TO SAVEPOINT sp_2",
            "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]

    def test_failed_statement_outside_savepoint_still_rolls_back(self):
        """Outermost-only behaviour is untouched: execute() rolls the connection back."""
        manager, conn = fake_manager("postgresql", fail_on="INSERT")
        with manager.transaction():
            with pytest.raises(RuntimeError):
                write(manager)
        assert conn.log == [
            "INSERT INTO t (v) VALUES (%(v)s)",
            "<rollback>",
            "<commit>",
        ]

    def test_join_mode_issues_no_savepoint(self):
        manager, conn = fake_manager("postgresql")
        with manager.transaction():
            with manager.transaction(nested="join"):
                write(manager)
        assert conn.log == ["INSERT INTO t (v) VALUES (%(v)s)", "<commit>"]

    def test_uncaught_failure_rolls_back_outer(self):
        manager, conn = fake_manager("postgresql")
        with pytest.raises(ValueError):
            with manager.transaction():
                with manager.transaction():
                    raise ValueError("boom")
        assert conn.log == [
            "SAVEPOINT sp_2",
            "ROLLBACK TO SAVEPOINT sp_2",
            "RELEASE SAVEPOINT sp_2",
            "<rollback>",
        ]


# ---------------------------------------------------------------------------
# Async backend against a psycopg-shaped fake pool
# ---------------------------------------------------------------------------

class FakeAsyncCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self._conn.log.append(sql)
        if self._conn.fail_on and sql.startswith(self._conn.fail_on):
            raise RuntimeError(f"fake driver error on {sql!r}")

    async def fetchall(self):
        return []


class FakeAsyncConnection:
    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on

    def cursor(self):
        return FakeAsyncCursor(self)

    async def commit(self):
        self.log.append("<commit>")

    async def rollback(self):
        self.log.append("<rollback>")


class FakeAsyncPool:
    def __init__(self, conn):
        self.conn = conn

    async def getconn(self, timeout=None):
        return self.conn

    async def putconn(self, conn):
        pass


def fake_backend(fail_on=None):
    backend = AsyncPostgresBackend("postgresql://u:p@localhost/db")
    conn = FakeAsyncConnection(fail_on=fail_on)
    backend._pool = FakeAsyncPool(conn)
    return backend, conn


class TestAsyncBackend:
    async def test_rollback_sequence(self):
        backend, conn = fake_backend()
        async with backend.transaction():
            with pytest.raises(ValueError):
                async with backend.transaction():
                    await backend.execute("INSERT INTO t (v) VALUES (:v)", {"v": 1})
                    raise ValueError("boom")
        assert conn.log == [
            "SAVEPOINT sp_2",
            "INSERT INTO t (v) VALUES (%(v)s)",
            "ROLLBACK TO SAVEPOINT sp_2",
            "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]

    async def test_clean_exit_releases(self):
        backend, conn = fake_backend()
        async with backend.transaction():
            async with backend.transaction():
                pass
        assert conn.log == ["SAVEPOINT sp_2", "RELEASE SAVEPOINT sp_2", "<commit>"]

    async def test_three_deep_names(self):
        backend, conn = fake_backend()
        async with backend.transaction():
            async with backend.transaction():
                async with backend.transaction():
                    assert backend._depth == 3
        assert conn.log == [
            "SAVEPOINT sp_2", "SAVEPOINT sp_3",
            "RELEASE SAVEPOINT sp_3", "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]
        assert backend._depth == 0

    async def test_failed_statement_inside_savepoint(self):
        backend, conn = fake_backend(fail_on="INSERT")
        async with backend.transaction():
            with pytest.raises(RuntimeError):
                async with backend.transaction():
                    await backend.execute("INSERT INTO t (v) VALUES (:v)", {"v": 1})
        assert conn.log == [
            "SAVEPOINT sp_2",
            "INSERT INTO t (v) VALUES (%(v)s)",
            "ROLLBACK TO SAVEPOINT sp_2",
            "RELEASE SAVEPOINT sp_2",
            "<commit>",
        ]

    async def test_join_mode_issues_no_savepoint(self):
        backend, conn = fake_backend()
        async with backend.transaction():
            async with backend.transaction(nested="join"):
                assert backend._depth == 2
        assert conn.log == ["<commit>"]

    async def test_invalid_mode_rejected(self):
        backend, conn = fake_backend()
        with pytest.raises(ValueError, match="nested="):
            async with backend.transaction(nested="nope"):
                pass
        assert conn.log == []
        assert backend._pinned_conn is None
