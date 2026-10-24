"""
stream() / stream_chunks() against live backends.

A 10k-row table is streamed in chunks and must come back complete and in
order on every backend; the pool must get its connection back after an early
break and after a mid-stream error. Backends that are not reachable are
skipped at collection (see conftest). The PostgreSQL-only tests pin down the
server-side cursor: no client-side buffering, a clean (idle) connection back
in the pool, and the async backend's equivalent guarantees.
"""

import contextlib
import logging
import os
import uuid

import pytest

from nexusql import DatabaseManager

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]
ROWS = 10_000


def table_name() -> str:
    return "stream_" + uuid.uuid4().hex[:10]


def fill(db, name: str, rows: int = ROWS) -> None:
    """Insert ``rows`` rows with v = 0..rows-1, in one transaction."""
    with db.transaction():
        for start in range(0, rows, 500):
            values = ",".join(f"({v})" for v in range(start, min(start + 500, rows)))
            db.execute(f"INSERT INTO {name} (v) VALUES {values}")


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, v INTEGER NOT NULL)")
    fill(db_manager, name)
    yield name
    try:
        db_manager.execute(f"DROP TABLE {name}")
    except Exception:
        pass


@pytest.fixture
def single_connection(db_manager):
    """A second manager on the same database whose pool holds one connection.

    A connection the stream failed to hand back would make the next query
    time out here instead of quietly borrowing another one.
    """
    db = DatabaseManager(db_manager.database_url, pool_max=1, pool_timeout=3)
    assert db.connect()
    yield db
    db.disconnect()


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestStream:
    def test_all_rows_in_order(self, db_manager, table):
        rows = list(db_manager.stream(f"SELECT id, v FROM {table} ORDER BY id", chunk_size=777))
        assert len(rows) == ROWS
        assert [r["v"] for r in rows] == list(range(ROWS))
        assert set(rows[0]) == {"id", "v"}

    def test_chunks(self, db_manager, table):
        chunks = list(db_manager.stream_chunks(f"SELECT v FROM {table} ORDER BY id", chunk_size=3000))
        assert [len(c) for c in chunks] == [3000, 3000, 3000, 1000]
        assert chunks[0][0] == {"v": 0} and chunks[-1][-1] == {"v": ROWS - 1}

    def test_named_params(self, db_manager, table):
        got = [
            r["v"] for r in db_manager.stream(
                f"SELECT v FROM {table} WHERE v >= :lo AND v < :hi ORDER BY v",
                {"lo": 100, "hi": 103},
            )
        ]
        assert got == [100, 101, 102]

    def test_early_break_returns_the_connection(self, db_manager, table, single_connection):
        db = single_connection
        seen = 0
        for _ in db.stream(f"SELECT v FROM {table} ORDER BY id", chunk_size=100):
            seen += 1
            if seen == 5:
                break
        # With one connection in the pool this would time out if the stream
        # still held it.
        assert db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS}
        assert db.execute(f"SELECT v FROM {table} WHERE v = :v", {"v": 7}) == [{"v": 7}]

    def test_exhausted_stream_returns_the_connection(self, db_manager, table, single_connection):
        db = single_connection
        assert sum(1 for _ in db.stream(f"SELECT v FROM {table}", chunk_size=4000)) == ROWS
        assert db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS}

    def test_error_raises_and_leaves_the_pool_usable(self, db_manager, table, single_connection):
        db = single_connection
        with pytest.raises(Exception):
            list(db.stream(f"SELECT * FROM {table}_does_not_exist"))
        assert db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS}
        db.execute(f"INSERT INTO {table} (v) VALUES (:v)", {"v": -1})
        assert db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS + 1}

    def test_inside_transaction(self, db_manager, table, single_connection):
        db = single_connection
        with db.transaction():
            db.execute(f"INSERT INTO {table} (v) VALUES (:v)", {"v": -1})
            # Reuses the pinned connection: a second checkout would time out.
            got = [r["v"] for r in db.stream(f"SELECT v FROM {table} WHERE v < :lim ORDER BY v", {"lim": 1})]
            assert got == [-1, 0]
            assert db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS + 1}
        # Committed by the block, and the connection is back.
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS + 1}

    def test_rolled_back_transaction_discards_rows_seen_by_stream(self, db_manager, table):
        with pytest.raises(RuntimeError, match="abort"):
            with db_manager.transaction():
                db_manager.execute(f"INSERT INTO {table} (v) VALUES (:v)", {"v": -1})
                assert [r["v"] for r in db_manager.stream(f"SELECT v FROM {table} WHERE v < 0")] == [-1]
                raise RuntimeError("abort")
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": ROWS}

    def test_empty_result(self, db_manager, table):
        assert list(db_manager.stream(f"SELECT v FROM {table} WHERE v < 0")) == []


# ---------------------------------------------------------------------------
# PostgreSQL: server-side cursor specifics
# ---------------------------------------------------------------------------

@pytest.fixture
def pg(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url, pool_max=1, pool_timeout=3)
    assert db.connect()
    name = table_name()
    db.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, v INTEGER NOT NULL)")
    db.execute(f"INSERT INTO {name} (v) SELECT generate_series(0, {ROWS - 1})")
    yield db, name
    db.execute(f"DROP TABLE IF EXISTS {name}")
    db.disconnect()


def test_postgres_uses_a_server_side_cursor(pg):
    """The rows arrive per FETCH: a cursor is open on the server mid-stream."""
    db, name = pg
    gen = db.stream(f"SELECT v FROM {name} ORDER BY id", chunk_size=100)
    next(gen)
    # The stream's connection is the pool's only one, so look through it.
    conn = db._state.current
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM pg_cursors WHERE name LIKE 'nexusql_stream_%'")
        open_cursors = [r["name"] for r in cur.fetchall()]
    assert len(open_cursors) == 1
    gen.close()
    assert db.fetch_one("SELECT COUNT(*) AS n FROM pg_cursors WHERE name LIKE 'nexusql_stream_%'") == {"n": 0}


def test_postgres_error_mid_stream(pg):
    """The server evaluates lazily, so a bad row fails in the chunk that reaches it."""
    db, name = pg
    seen = 0
    with pytest.raises(Exception) as excinfo:
        for _ in db.stream(f"SELECT 1 / (5000 - v) AS x FROM {name} ORDER BY id", chunk_size=1000):
            seen += 1
    assert "division by zero" in str(excinfo.value)
    # v runs 0..9999 in id order, so the five FETCHes before the bad row succeed.
    assert seen == 5000
    # Rolled back and released: the single pooled connection still works.
    assert db.fetch_one("SELECT 1 AS ok") == {"ok": 1}


def test_postgres_connection_returns_idle(pg, caplog):
    """The stream ends its own transaction, so the pool has nothing to roll back."""
    db, name = pg
    with caplog.at_level(logging.WARNING, logger="psycopg.pool"):
        list(db.stream(f"SELECT v FROM {name}", chunk_size=5000))
        for _ in db.stream(f"SELECT v FROM {name}"):
            break
        with pytest.raises(Exception):
            list(db.stream("SELECT * FROM nope_nope"))
    assert not [r for r in caplog.records if "rolling back returned connection" in r.getMessage()]


def test_postgres_autocommit_connection(pg):
    """A connection switched to autocommit still gets the transaction block a named cursor needs."""
    db, name = pg
    db._connection.autocommit = True
    try:
        assert sum(1 for _ in db.stream(f"SELECT v FROM {name}", chunk_size=4000)) == ROWS
        assert db.fetch_one("SELECT 1 AS ok") == {"ok": 1}
    finally:
        db._connection.autocommit = False


# ---------------------------------------------------------------------------
# Async PostgreSQL backend
# ---------------------------------------------------------------------------

ASYNC_URL = os.getenv(
    "TEST_POSTGRESQL_URL", "postgresql://postgres:mwtest@127.0.0.1:15432/mwtest"
)


@pytest.fixture
async def async_backend():
    from nexusql import PSYCOPG_AVAILABLE
    if not PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed")
    from nexusql import AsyncPostgresBackend

    # One pooled connection, so a stream that failed to release would make
    # the next statement time out rather than borrow another.
    backend = AsyncPostgresBackend(ASYNC_URL, max_pool_size=1, timeout=3)
    try:
        if not await backend.connect():
            pytest.skip("PostgreSQL not reachable for async tests")
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable for async tests: {exc}")
    yield backend
    await backend.disconnect()


@pytest.fixture
async def async_table(async_backend):
    name = table_name()
    await async_backend.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, v INTEGER NOT NULL)")
    await async_backend.execute(f"INSERT INTO {name} (v) SELECT generate_series(0, {ROWS - 1})")
    yield name
    await async_backend.execute(f"DROP TABLE IF EXISTS {name}")


async def test_async_all_rows_in_order(async_backend, async_table):
    rows = [r async for r in async_backend.stream(f"SELECT id, v FROM {async_table} ORDER BY id", chunk_size=999)]
    assert len(rows) == ROWS
    assert [r["v"] for r in rows] == list(range(ROWS))

    chunks = [c async for c in async_backend.stream_chunks(f"SELECT v FROM {async_table} ORDER BY id", chunk_size=3000)]
    assert [len(c) for c in chunks] == [3000, 3000, 3000, 1000]


async def test_async_named_params(async_backend, async_table):
    got = [
        r["v"] async for r in async_backend.stream(
            f"SELECT v FROM {async_table} WHERE v >= :lo AND v < :hi ORDER BY v",
            {"lo": 10, "hi": 12},
        )
    ]
    assert got == [10, 11]


async def test_async_early_break_returns_the_connection(async_backend, async_table):
    seen = 0
    async with contextlib.aclosing(async_backend.stream(f"SELECT v FROM {async_table}", chunk_size=100)) as rows:
        async for _ in rows:
            seen += 1
            if seen == 5:
                break
    assert await async_backend.execute(f"SELECT COUNT(*) AS n FROM {async_table}") == [{"n": ROWS}]


async def test_async_explicit_aclose(async_backend, async_table):
    gen = async_backend.stream(f"SELECT v FROM {async_table}", chunk_size=100)
    await gen.__anext__()
    await gen.aclose()
    assert await async_backend.execute("SELECT 1 AS ok") == [{"ok": 1}]


async def test_async_error_mid_stream_leaves_pool_usable(async_backend, async_table):
    seen = 0
    with pytest.raises(Exception) as excinfo:
        async for _ in async_backend.stream(f"SELECT 1 / (5000 - v) AS x FROM {async_table} ORDER BY id", chunk_size=1000):
            seen += 1
    assert "division by zero" in str(excinfo.value)
    assert seen == 5000
    assert await async_backend.execute("SELECT 1 AS ok") == [{"ok": 1}]


async def test_async_inside_transaction(async_backend, async_table):
    async with async_backend.transaction():
        await async_backend.execute(f"INSERT INTO {async_table} (v) VALUES (:v)", {"v": -1})
        got = [r["v"] async for r in async_backend.stream(f"SELECT v FROM {async_table} WHERE v < 0")]
        assert got == [-1]
        # Still the pinned connection, still inside the transaction.
        assert await async_backend.execute(f"SELECT COUNT(*) AS n FROM {async_table}") == [{"n": ROWS + 1}]
    assert await async_backend.execute(f"SELECT COUNT(*) AS n FROM {async_table}") == [{"n": ROWS + 1}]


async def test_async_transaction_statements_rejected(async_backend):
    with pytest.raises(RuntimeError, match="transaction"):
        async_backend.stream("BEGIN")
    with pytest.raises(ValueError, match="chunk_size"):
        async_backend.stream("SELECT 1", chunk_size=0)
