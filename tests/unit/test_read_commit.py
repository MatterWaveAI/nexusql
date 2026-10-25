"""
A read outside transaction() must leave its connection idle.

psycopg, pymysql and pyodbc open a transaction on the first statement, so a
plain SELECT through execute() or fetch_one() used to hand the connection
back to the pool still in-transaction; psycopg_pool then rolled it back and
logged a warning on every return. Both read paths now commit the (empty)
transaction before releasing, unless the caller holds a transaction, in
which case the block still owns the single commit.

These tests drive a PostgreSQL-configured manager against a fake pool and
connection, so they run without a server; the live check against psycopg's
transaction status is in tests/integration/test_read_commit.py.
"""

import pytest

from nexusql import ConnectionConfig, DatabaseManager
from nexusql.pool import ConnectionPool


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("one", None)] if rows else None
        self.closed = False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [{"one": 1}]
        self.commits = 0
        self.rollbacks = 0
        self.cursors = []

    def cursor(self):
        cur = FakeCursor(self.rows)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool(ConnectionPool):
    def __init__(self, conn):
        self.conn = conn
        self.releases = []

    def acquire(self, timeout=None):
        return self.conn

    def release(self, conn, *, discard=False):
        self.releases.append(discard)

    def close(self):
        pass

    @property
    def size(self):
        return 1


@pytest.fixture
def db():
    manager = DatabaseManager(ConnectionConfig.from_url("postgresql://u:p@localhost/db"))
    conn = FakeConnection()
    manager._pool = FakePool(conn)
    return manager, conn


def test_execute_select_commits_before_release(db):
    manager, conn = db
    assert manager.execute("SELECT 1 AS one") == [{"one": 1}]
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.cursors[-1].closed, "cursor closed before the commit"


def test_fetch_one_select_commits_before_release(db):
    manager, conn = db
    assert manager.fetch_one("SELECT 1 AS one") == {"one": 1}
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_empty_result_still_commits(db):
    manager, conn = db
    conn.rows = []
    assert manager.execute("SELECT 1 AS one WHERE false") == []
    assert manager.fetch_one("SELECT 1 AS one WHERE false") is None
    assert conn.commits == 2


def test_reads_inside_transaction_leave_the_commit_to_the_block(db):
    manager, conn = db
    with manager.transaction():
        manager.execute("SELECT 1 AS one")
        manager.fetch_one("SELECT 1 AS one")
        assert conn.commits == 0
    assert conn.commits == 1
