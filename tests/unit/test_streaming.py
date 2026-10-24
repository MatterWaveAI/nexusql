"""
stream() / stream_chunks(): large results in bounded memory.

The SQLite tests exercise the real driver path without a server. The
PostgreSQL, MySQL and SQL Server branches each pick a different cursor
(named server-side cursor, pymysql SSDictCursor, pyodbc rows converted via
cursor.description), so they are checked here against driver-shaped fakes
behind a fake pool, which also makes connection release observable. The live
backends are covered by tests/integration/test_streaming.py when reachable.
"""

import sqlite3
import types

import pytest

from nexusql import ConnectionConfig, DatabaseManager
from nexusql import streaming

ROWS = 10_000


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute("CREATE TABLE items (id SERIAL PRIMARY KEY, v INTEGER)")
    with manager.transaction():
        for start in range(0, ROWS, 500):
            values = ",".join(f"({v})" for v in range(start, start + 500))
            manager.execute(f"INSERT INTO items (v) VALUES {values}")
    yield manager
    try:
        manager.disconnect()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SQLite: the real driver path
# ---------------------------------------------------------------------------

class TestSqlite:
    @pytest.mark.parametrize("chunk_size", [1, 7, 1000, ROWS * 2])
    def test_all_rows_in_order(self, db, chunk_size):
        rows = list(db.stream("SELECT id, v FROM items ORDER BY id", chunk_size=chunk_size))
        assert len(rows) == ROWS
        assert [r["v"] for r in rows] == list(range(ROWS))
        assert rows[0] == {"id": 1, "v": 0}

    def test_chunks_are_lists_of_chunk_size(self, db):
        chunks = list(db.stream_chunks("SELECT v FROM items ORDER BY id", chunk_size=3000))
        assert [len(c) for c in chunks] == [3000, 3000, 3000, 1000]
        assert chunks[0][0] == {"v": 0}
        assert chunks[-1][-1] == {"v": ROWS - 1}

    def test_named_params(self, db):
        got = [
            r["v"]
            for r in db.stream(
                "SELECT v FROM items WHERE v >= :lo AND v < :hi ORDER BY v",
                {"lo": 10, "hi": 13},
            )
        ]
        assert got == [10, 11, 12]

    def test_is_lazy_and_returns_a_generator(self, db):
        gen = db.stream("SELECT * FROM no_such_table")
        assert isinstance(gen, types.GeneratorType)
        # Nothing ran yet: the error surfaces on the first next().
        with pytest.raises(sqlite3.OperationalError):
            next(gen)

    def test_early_break_leaves_manager_usable(self, db):
        seen = 0
        for _ in db.stream("SELECT v FROM items ORDER BY id", chunk_size=10):
            seen += 1
            if seen == 3:
                break
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": ROWS}
        db.execute("INSERT INTO items (v) VALUES (:v)", {"v": -1})
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": ROWS + 1}

    def test_error_mid_stream_raises_and_leaves_manager_usable(self, db):
        def boom(v):
            if v == 5000:
                raise RuntimeError("boom")
            return v

        db._connection.create_function("boom", 1, boom)
        seen = 0
        with pytest.raises(sqlite3.OperationalError):
            for _ in db.stream("SELECT boom(v) AS v FROM items ORDER BY id", chunk_size=1000):
                seen += 1
        # sqlite3 steps to the next row as it hands one back, so the chunk
        # ending just before the bad row is lost along with it.
        assert seen == 4000
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": ROWS}

    def test_inside_transaction_sees_uncommitted_rows_and_defers_commit(self, db, temp_db_path):
        with db.transaction():
            db.execute("INSERT INTO items (v) VALUES (:v)", {"v": -1})
            got = [r["v"] for r in db.stream("SELECT v FROM items WHERE v < :lim ORDER BY v", {"lim": 1})]
            assert got == [-1, 0]
            reader = DatabaseManager(f"sqlite:///{temp_db_path}")
            assert reader.connect()
            try:
                assert reader.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": ROWS}
            finally:
                reader.disconnect()
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": ROWS + 1}

    def test_write_without_rows_streams_nothing(self, db):
        assert list(db.stream("DELETE FROM items WHERE v >= :v", {"v": ROWS - 10})) == []
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": ROWS - 10}

    def test_empty_result(self, db):
        assert list(db.stream("SELECT v FROM items WHERE v < 0")) == []
        assert list(db.stream_chunks("SELECT v FROM items WHERE v < 0")) == []

    @pytest.mark.parametrize("bad", [0, -1, 2.5, True, "10"])
    def test_bad_chunk_size_rejected_at_call_time(self, db, bad):
        with pytest.raises(ValueError, match="chunk_size"):
            db.stream("SELECT 1", chunk_size=bad)
        with pytest.raises(ValueError, match="chunk_size"):
            db.stream_chunks("SELECT 1", chunk_size=bad)

    def test_transaction_statements_rejected_at_call_time(self, db):
        with pytest.raises(RuntimeError, match="transaction"):
            db.stream("BEGIN")
        with pytest.raises(RuntimeError, match="transaction"):
            db.stream_chunks("COMMIT;")

    def test_not_connected(self, temp_db_path):
        manager = DatabaseManager(f"sqlite:///{temp_db_path}")
        with pytest.raises(RuntimeError, match="not connected"):
            next(manager.stream("SELECT 1"))

    def test_memory_profile_rows_are_not_retained(self, db):
        """Only the current chunk is alive: the generator keeps no row list."""
        import gc
        gen = db.stream("SELECT v FROM items ORDER BY id", chunk_size=100)
        first = next(gen)
        for _ in range(250):
            next(gen)
        gc.collect()
        frame_locals = gen.gi_frame.f_locals
        retained = [v for v in frame_locals.values() if isinstance(v, list)]
        assert all(len(v) <= 100 for v in retained)
        assert first == {"v": 0}
        gen.close()


# ---------------------------------------------------------------------------
# Server dialects against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """A DB-API cursor over a fixed row list; records what was asked of it."""

    def __init__(self, conn, rows, description, *, fail_at=None, name=None):
        self.conn = conn
        self._rows = list(rows)
        self._pos = 0
        self.description = None
        self._description = description
        self._fail_at = fail_at
        self.name = name
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.description = self._description

    def fetchmany(self, size):
        if self._fail_at is not None and self._pos + size > self._fail_at:
            raise RuntimeError("server went away")
        chunk = self._rows[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True
        self.conn.closed_cursors.append(self)


class FakeConnection:
    def __init__(self, rows, description, *, fail_at=None, autocommit=False):
        self._rows = rows
        self._description = description
        self._fail_at = fail_at
        self.autocommit = autocommit
        self.cursors = []
        self.closed_cursors = []
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *args, **kwargs):
        cur = FakeCursor(self, self._rows, self._description, fail_at=self._fail_at,
                         name=kwargs.get("name"))
        cur.cursor_class = args[0] if args else None
        self.cursors.append(cur)
        return cur

    def execute(self, sql):
        self.statements.append(sql)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    """Hands out one connection and counts checkouts and returns."""

    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0
        self.released = []

    def acquire(self, timeout=None):
        self.acquired += 1
        return self.conn

    def release(self, conn, *, discard=False):
        self.released.append(discard)

    def close(self):
        pass

    @property
    def size(self):
        return 1


def fake_manager(db_type: str, conn) -> DatabaseManager:
    urls = {
        "mysql": "mysql://u:p@localhost/db",
        "mssql": "mssql://u:p@localhost/db",
        "postgresql": "postgresql://u:p@localhost/db",
    }
    manager = DatabaseManager(ConnectionConfig.from_url(urls[db_type]))
    manager._pool = FakePool(conn)
    return manager


DICT_ROWS = [{"id": i, "v": i * 10} for i in range(25)]
TUPLE_ROWS = [(i, i * 10) for i in range(25)]
DESCRIPTION = [("id", int), ("v", int)]


class TestPostgresBranch:
    def test_named_cursor_commit_on_exhaustion(self):
        conn = FakeConnection(DICT_ROWS, DESCRIPTION)
        manager = fake_manager("postgresql", conn)
        rows = list(manager.stream("SELECT id, v FROM t WHERE v > :v", {"v": -1}, chunk_size=10))
        assert rows == DICT_ROWS

        cursor, = conn.cursors
        assert cursor.name.startswith("nexusql_stream_")
        assert cursor.executed == [("SELECT id, v FROM t WHERE v > %(v)s", {"v": -1})]
        assert cursor.closed and conn.commits == 1 and conn.rollbacks == 0
        assert conn.statements == []
        assert manager._pool.acquired == 1 and manager._pool.released == [False]

    def test_each_stream_gets_its_own_cursor_name(self):
        conn = FakeConnection(DICT_ROWS, DESCRIPTION)
        manager = fake_manager("postgresql", conn)
        list(manager.stream("SELECT 1"))
        list(manager.stream("SELECT 1"))
        names = {c.name for c in conn.cursors}
        assert len(names) == 2

    def test_early_break_closes_cursor_commits_and_releases(self):
        conn = FakeConnection(DICT_ROWS, DESCRIPTION)
        manager = fake_manager("postgresql", conn)
        for i, _ in enumerate(manager.stream("SELECT 1", chunk_size=5)):
            if i == 1:
                break
        cursor, = conn.cursors
        assert cursor.closed and conn.commits == 1 and conn.rollbacks == 0
        assert manager._pool.released == [False]

    def test_connection_is_borrowed_lazily(self):
        conn = FakeConnection(DICT_ROWS, DESCRIPTION)
        manager = fake_manager("postgresql", conn)
        gen = manager.stream("SELECT 1")
        assert manager._pool.acquired == 0
        next(gen)
        assert manager._pool.acquired == 1
        gen.close()
        assert manager._pool.released == [False]

    def test_error_mid_stream_rolls_back_and_releases(self):
        conn = FakeConnection(DICT_ROWS, DESCRIPTION, fail_at=20)
        manager = fake_manager("postgresql", conn)
        seen = []
        with pytest.raises(RuntimeError, match="server went away"):
            for row in manager.stream("SELECT 1", chunk_size=10):
                seen.append(row)
        assert len(seen) == 20
        cursor, = conn.cursors
        assert cursor.closed and conn.rollbacks == 1 and conn.commits == 0
        assert manager._pool.released == [False]

    def test_inside_transaction_reuses_pinned_connection_without_commit(self):
        conn = FakeConnection(DICT_ROWS, DESCRIPTION)
        manager = fake_manager("postgresql", conn)
        with manager.transaction():
            assert manager._pool.acquired == 1
            rows = list(manager.stream("SELECT 1", chunk_size=10))
            assert len(rows) == 25
            # Still pinned, nothing committed by the stream.
            assert manager._pool.acquired == 1 and manager._pool.released == []
            assert conn.commits == 0
        assert conn.commits == 1  # transaction() committed
        assert manager._pool.released == [False]

    def test_autocommit_connection_gets_an_explicit_begin(self):
        """A named cursor needs a transaction block; psycopg will not open one in autocommit mode."""
        conn = FakeConnection(DICT_ROWS, DESCRIPTION, autocommit=True)
        manager = fake_manager("postgresql", conn)
        list(manager.stream("SELECT 1"))
        assert conn.statements == ["BEGIN"]
        assert conn.commits == 1


class TestMysqlBranch:
    def test_uses_unbuffered_dict_cursor(self):
        cursors = pytest.importorskip("pymysql.cursors")
        conn = FakeConnection(DICT_ROWS, DESCRIPTION)
        manager = fake_manager("mysql", conn)
        rows = list(manager.stream("SELECT id, v FROM t WHERE v > :v", {"v": -1}, chunk_size=10))
        assert rows == DICT_ROWS
        cursor, = conn.cursors
        assert cursor.cursor_class is cursors.SSDictCursor
        assert cursor.executed == [("SELECT id, v FROM t WHERE v > %s", (-1,))]
        assert cursor.closed and conn.commits == 1
        assert manager._pool.released == [False]


class TestMssqlBranch:
    def test_rows_converted_via_description(self):
        conn = FakeConnection(TUPLE_ROWS, DESCRIPTION)
        manager = fake_manager("mssql", conn)
        rows = list(manager.stream("SELECT id, v FROM t WHERE v > :v", {"v": -1}, chunk_size=10))
        assert rows == DICT_ROWS
        cursor, = conn.cursors
        assert cursor.cursor_class is None and cursor.name is None
        assert cursor.executed == [("SELECT id, v FROM t WHERE v > ?", (-1,))]
        assert cursor.closed and conn.commits == 1
        assert manager._pool.released == [False]

    def test_no_result_set_streams_nothing(self):
        conn = FakeConnection([], None)
        manager = fake_manager("mssql", conn)
        assert list(manager.stream("UPDATE t SET v = 1")) == []
        assert conn.commits == 1


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def test_default_chunk_size_is_shared():
    import inspect
    assert streaming.DEFAULT_CHUNK_SIZE == 1000
    sig = inspect.signature(DatabaseManager.stream)
    assert sig.parameters["chunk_size"].default == streaming.DEFAULT_CHUNK_SIZE
