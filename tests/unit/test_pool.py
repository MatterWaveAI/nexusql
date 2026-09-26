"""
Tests for the connection pool implementations and the manager's use of them.
"""

import threading
import time

import pytest

from nexusql import DatabaseManager, ConnectionConfig, DatabaseType
from nexusql.pool import GenericPool, NullPool, PoolTimeout


class _FakeConn:
    """Minimal stand-in for a DBAPI connection."""

    def __init__(self, ident):
        self.ident = ident
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _factory():
    counter = iter(range(1000))
    return lambda: _FakeConn(next(counter))


class TestGenericPool:
    def test_opens_min_size_eagerly(self):
        pool = GenericPool(_factory(), min_size=3, max_size=5)
        assert pool.size == 3
        assert pool.idle == 3

    def test_reuses_idle_connection(self):
        pool = GenericPool(_factory(), min_size=1, max_size=5)
        first = pool.acquire()
        pool.release(first)
        second = pool.acquire()
        assert second is first
        assert pool.size == 1

    def test_grows_to_max_under_concurrent_checkout(self):
        pool = GenericPool(_factory(), min_size=1, max_size=4)
        held = [pool.acquire() for _ in range(4)]
        assert len({c.ident for c in held}) == 4
        assert pool.size == 4
        for c in held:
            pool.release(c)

    def test_blocks_then_times_out_at_capacity(self):
        pool = GenericPool(_factory(), min_size=1, max_size=1, timeout=0.2)
        held = pool.acquire()
        start = time.monotonic()
        with pytest.raises(PoolTimeout):
            pool.acquire()
        # It waited rather than failing instantly.
        assert time.monotonic() - start >= 0.2
        pool.release(held)

    def test_waiter_gets_connection_when_released(self):
        pool = GenericPool(_factory(), min_size=1, max_size=1, timeout=5)
        held = pool.acquire()
        got = []

        def waiter():
            got.append(pool.acquire())

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        pool.release(held)
        t.join(timeout=5)

        assert got and got[0] is held

    def test_discard_destroys_instead_of_returning(self):
        pool = GenericPool(_factory(), min_size=1, max_size=5)
        conn = pool.acquire()
        pool.release(conn, discard=True)
        assert conn.closed is True
        assert pool.size == 0

    def test_validation_failure_replaces_connection(self):
        dead = set()

        def validate(conn):
            return conn.ident not in dead

        pool = GenericPool(_factory(), min_size=1, max_size=5, validate=validate)
        first = pool.acquire()
        dead.add(first.ident)
        pool.release(first)

        replacement = pool.acquire()
        assert replacement is not first
        assert first.closed is True

    def test_close_closes_idle_connections(self):
        pool = GenericPool(_factory(), min_size=3, max_size=5)
        conns = [pool.acquire() for _ in range(3)]
        for c in conns:
            pool.release(c)
        pool.close()
        assert all(c.closed for c in conns)

    def test_rejects_bad_sizes(self):
        with pytest.raises(ValueError):
            GenericPool(_factory(), max_size=0)
        with pytest.raises(ValueError):
            GenericPool(_factory(), min_size=5, max_size=2)


class TestNullPool:
    def test_always_returns_same_connection(self):
        pool = NullPool(_factory())
        assert pool.acquire() is pool.acquire()

    def test_release_is_a_noop(self):
        pool = NullPool(_factory())
        conn = pool.acquire()
        pool.release(conn)
        assert conn.closed is False
        assert pool.acquire() is conn

    def test_discard_replaces_connection(self):
        pool = NullPool(_factory())
        conn = pool.acquire()
        pool.release(conn, discard=True)
        assert conn.closed is True
        assert pool.acquire() is not conn


@pytest.fixture
def db(tmp_path):
    config = ConnectionConfig(
        database_type=DatabaseType.SQLITE,
        database_url=f"sqlite:///{tmp_path / 'pool.db'}",
    )
    manager = DatabaseManager(config)
    manager.connect()
    manager.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    yield manager
    manager.disconnect()


class TestManagerTransactions:
    def test_commits_on_clean_exit(self, db):
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "kept"})

        assert len(db.execute("SELECT * FROM t")) == 1

    def test_rolls_back_on_exception(self, db):
        with pytest.raises(ValueError):
            with db.transaction():
                db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "dropped"})
                raise ValueError("boom")

        assert db.execute("SELECT * FROM t") == []

    def test_nested_transaction_joins_outer(self, db):
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "a"})
            with db.transaction():
                db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "b"})

        assert len(db.execute("SELECT * FROM t")) == 2

    def test_inner_failure_rolls_back_everything(self, db):
        with pytest.raises(ValueError):
            with db.transaction():
                db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "a"})
                with db.transaction():
                    raise ValueError("boom")

        assert db.execute("SELECT * FROM t") == []

    def test_in_transaction_tracks_state(self, db):
        assert db._in_transaction is False
        with db.transaction():
            assert db._in_transaction is True
        assert db._in_transaction is False


class TestRemovedTransactionProtocol:
    @pytest.mark.parametrize("stmt", [
        "BEGIN", "begin", "BEGIN TRANSACTION", "START TRANSACTION",
        "COMMIT", "commit;", "ROLLBACK", "ROLLBACK TRANSACTION",
    ])
    def test_string_protocol_raises(self, db, stmt):
        with pytest.raises(RuntimeError, match="transaction"):
            db.execute(stmt)

    def test_error_names_the_replacement(self, db):
        with pytest.raises(RuntimeError, match=r"db\.transaction\(\)"):
            db.execute("BEGIN")

    def test_ordinary_queries_still_run(self, db):
        db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "x"})
        assert len(db.execute("SELECT * FROM t")) == 1


class TestConnectionCompatibility:
    def test_connection_attribute_is_available(self, db):
        assert db._connection is not None

    def test_connection_is_stable_within_a_thread(self, db):
        assert db._connection is db._connection

    def test_disconnect_clears_it(self, db):
        db.disconnect()
        assert db._connection is None


class TestFetchApi:
    def test_fetch_all_is_removed(self, db):
        assert not hasattr(db, 'fetch_all')

    def test_fetch_one_returns_dict_or_none(self, db):
        db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "a"})
        assert db.fetch_one("SELECT * FROM t WHERE v = :v", {"v": "a"})["v"] == "a"
        assert db.fetch_one("SELECT * FROM t WHERE v = :v", {"v": "zz"}) is None

    @pytest.mark.asyncio
    async def test_fetch_one_async_matches_sync(self, db):
        db.execute("INSERT INTO t (v) VALUES (:v)", {"v": "a"})
        got = await db.fetch_one_async("SELECT * FROM t WHERE v = :v", {"v": "a"})
        assert got == db.fetch_one("SELECT * FROM t WHERE v = :v", {"v": "a"})

    @pytest.mark.asyncio
    async def test_fetch_one_async_returns_none_when_missing(self, db):
        assert await db.fetch_one_async("SELECT * FROM t WHERE v = :v", {"v": "zz"}) is None

    def test_fetch_one_async_is_a_coroutine_function(self, db):
        import inspect
        assert inspect.iscoroutinefunction(db.fetch_one_async)

    def test_execute_returning_is_durable(self, db):
        rows = db.execute("INSERT INTO t (v) VALUES (:v) RETURNING id", {"v": "r"})
        assert rows and "id" in rows[0]
        # Survives the connection going back to the pool and being reused.
        assert db.execute("SELECT * FROM t WHERE v = :v", {"v": "r"})


class TestConnectTimeout:
    """An unreachable server must be reported promptly, not after the full
    pool checkout timeout."""

    def test_connect_returns_false_for_unreachable_server(self):
        db = DatabaseManager(
            "postgresql://nouser:nopass@127.0.0.1:1/nodb",
            connect_timeout=2,
        )
        assert db.connect() is False

    def test_connect_timeout_is_honoured(self):
        start = time.monotonic()
        db = DatabaseManager(
            "postgresql://nouser:nopass@127.0.0.1:1/nodb",
            pool_timeout=60,
            connect_timeout=2,
        )
        assert db.connect() is False
        # Bounded by connect_timeout rather than the 60s checkout timeout.
        # The upper bound allows for psycopg's worker-thread shutdown.
        assert time.monotonic() - start < 30

    def test_queries_fail_clearly_when_not_connected(self):
        db = DatabaseManager("postgresql://nouser:nopass@127.0.0.1:1/nodb",
                             connect_timeout=2)
        db.connect()
        with pytest.raises(RuntimeError, match="not connected"):
            db.execute("SELECT 1")
