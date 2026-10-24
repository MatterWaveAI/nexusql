"""
Query performance tools: slow-query logging, explain(), query_stats().

The SQLite tests exercise the real driver path without a server, with the
clock faked so a threshold can be crossed deterministically. The MySQL and
SQL Server explain() branches issue backend-specific statements (EXPLAIN
ANALYZE, SET SHOWPLAN_ALL), so they are checked against fake connections
shaped like those drivers; live backends are covered by
tests/integration/test_query_performance.py when reachable.
"""

import logging
import sqlite3
import threading

import pytest

from nexusql import ConnectionConfig, DatabaseManager, DatabaseType, QueryStat, QueryTiming
from nexusql import performance
from nexusql.performance import (
    QueryMonitor,
    check_analyzable,
    explain_sql,
    normalise_sql,
    plan_text,
    row_count,
)


class FakeClock:
    """
    perf_counter stand-in. The timing wrapper reads the clock twice per
    statement; the second read advances by the next duration in seconds, so
    each statement appears to take exactly that long. The last duration
    repeats once the list runs out.
    """

    def __init__(self, *durations):
        self.durations = list(durations)
        self.now = 1000.0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls % 2 == 0:
            if len(self.durations) > 1:
                self.now += self.durations.pop(0)
            else:
                self.now += self.durations[0]
        return self.now


@pytest.fixture
def clock(monkeypatch):
    def install(*steps):
        fake = FakeClock(*steps)
        monkeypatch.setattr(performance, 'perf_counter', fake)
        return fake
    return install


def sqlite_manager(temp_db_path, **options) -> DatabaseManager:
    """A monitored manager over a two-row table created by a plain one."""
    url = f"sqlite:///{temp_db_path}"
    setup = DatabaseManager(url)
    assert setup.connect(), "SQLite connect failed"
    setup.execute("CREATE TABLE IF NOT EXISTS items (id SERIAL PRIMARY KEY, name TEXT)")
    if setup.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 0}:
        setup.execute("INSERT INTO items (name) VALUES (:n)", {"n": "a"})
        setup.execute("INSERT INTO items (name) VALUES (:n)", {"n": "b"})
    setup.disconnect()

    manager = DatabaseManager(url, **options)
    assert manager.connect()
    return manager


# ---------------------------------------------------------------------------
# Slow-query logging on SQLite
# ---------------------------------------------------------------------------

class TestSlowQueryLogging:
    def test_disabled_by_default(self, temp_db_path, caplog):
        db = sqlite_manager(temp_db_path)
        assert db._monitor is None
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            assert db.execute("SELECT name FROM items ORDER BY id") == [{"name": "a"}, {"name": "b"}]
        assert not [r for r in caplog.records if "Slow query" in r.getMessage()]
        db.disconnect()

    def test_callback_without_threshold_is_inert(self, temp_db_path):
        seen = []
        db = sqlite_manager(temp_db_path, on_slow_query=seen.append)
        assert db._monitor is None
        db.execute("SELECT 1")
        assert seen == []
        db.disconnect()

    def test_fires_at_or_over_threshold(self, temp_db_path, clock, caplog):
        seen = []
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=100,
                            on_slow_query=seen.append)
        clock(0.250)
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            rows = db.execute("SELECT name FROM items\n  WHERE name = :n", {"n": "a"})
        assert rows == [{"name": "a"}]

        records = [r for r in caplog.records if "Slow query" in r.getMessage()]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        assert record.name == "nexusql.performance"
        message = record.getMessage()
        assert "250.0 ms" in message and "sqlite" in message
        assert "SELECT name FROM items WHERE name = :n" in message

        assert len(seen) == 1
        timing = seen[0]
        assert isinstance(timing, QueryTiming)
        assert timing.sql == "SELECT name FROM items\n  WHERE name = :n"
        assert timing.params == {"n": "a"}
        assert timing.duration_ms == pytest.approx(250.0)
        assert timing.backend == "sqlite"
        assert timing.rows == 1
        db.disconnect()

    def test_does_not_fire_under_threshold(self, temp_db_path, clock, caplog):
        seen = []
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=100,
                            on_slow_query=seen.append)
        clock(0.001)
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            db.execute("SELECT name FROM items")
            db.fetch_one("SELECT name FROM items")
        assert seen == []
        assert not [r for r in caplog.records if "Slow query" in r.getMessage()]
        db.disconnect()

    def test_threshold_zero_logs_everything(self, temp_db_path, caplog):
        seen = []
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=0,
                            on_slow_query=seen.append)
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            db.execute("SELECT 1 AS one")
        assert len(seen) == 1 and seen[0].sql == "SELECT 1 AS one"
        db.disconnect()

    def test_params_absent_from_log_by_default(self, temp_db_path, clock, caplog):
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=0)
        clock(0.01)
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            db.fetch_one("SELECT name FROM items WHERE name = :secret",
                         {"secret": "pii-value"})
        messages = [r.getMessage() for r in caplog.records if "Slow query" in r.getMessage()]
        assert len(messages) == 1
        assert "pii-value" not in messages[0]
        assert ":secret" in messages[0]
        db.disconnect()

    def test_params_logged_when_asked(self, temp_db_path, clock, caplog):
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=0, log_params=True)
        clock(0.01)
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            db.fetch_one("SELECT name FROM items WHERE name = :n", {"n": "visible"})
        messages = [r.getMessage() for r in caplog.records if "Slow query" in r.getMessage()]
        assert len(messages) == 1 and "'visible'" in messages[0]
        db.disconnect()

    def test_row_counts_per_call_shape(self, temp_db_path, clock):
        seen = []
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=0,
                            on_slow_query=seen.append)
        clock(0.01)
        db.execute("SELECT name FROM items")
        db.execute("INSERT INTO items (name) VALUES (:n)", {"n": "c"})
        db.execute("INSERT INTO items (name) VALUES (:n)", {"n": "d"}, return_id=True)
        db.fetch_one("SELECT name FROM items WHERE name = :n", {"n": "a"})
        db.fetch_one("SELECT name FROM items WHERE name = :n", {"n": "zzz"})
        assert [t.rows for t in seen] == [2, 0, None, 1, 0]
        db.disconnect()

    def test_failure_is_recorded_with_unknown_rows(self, temp_db_path, clock):
        seen = []
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=0,
                            on_slow_query=seen.append)
        clock(0.01)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("SELECT nope FROM missing_table")
        assert len(seen) == 1
        assert seen[0].rows is None
        assert seen[0].sql == "SELECT nope FROM missing_table"
        # The failed statement did not poison the connection.
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 2}
        db.disconnect()

    async def test_async_entry_points_are_timed(self, temp_db_path, clock):
        seen = []
        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=0,
                            on_slow_query=seen.append)
        clock(0.01)
        await db.execute_async("SELECT name FROM items")
        await db.fetch_one_async("SELECT name FROM items")
        assert [t.rows for t in seen] == [2, 1]
        db.disconnect()

    def test_negative_threshold_rejected(self, temp_db_path):
        with pytest.raises(ValueError):
            DatabaseManager(f"sqlite:///{temp_db_path}", slow_query_threshold_ms=-1)

    def test_signatures_survive_wrapping(self):
        import inspect
        params = list(inspect.signature(DatabaseManager.execute).parameters)
        assert params == ["self", "query", "params", "return_id"]
        assert list(inspect.signature(DatabaseManager.fetch_one).parameters) == [
            "self", "query", "params"]


# ---------------------------------------------------------------------------
# Query stats
# ---------------------------------------------------------------------------

class TestQueryStats:
    def test_aggregates_by_normalised_sql(self, temp_db_path, clock, caplog):
        db = sqlite_manager(temp_db_path, collect_stats=True)
        clock(0.010, 0.030, 0.020)
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            db.execute("SELECT name FROM items")
            db.execute("SELECT   name\n\tFROM items")
            db.fetch_one("SELECT COUNT(*) AS n FROM items")

        stats = db.query_stats()
        assert all(isinstance(s, QueryStat) for s in stats)
        by_sql = {s.sql: s for s in stats}
        assert set(by_sql) == {"SELECT name FROM items", "SELECT COUNT(*) AS n FROM items"}

        select = by_sql["SELECT name FROM items"]
        assert select.count == 2
        assert select.total_ms == pytest.approx(40.0)
        assert select.max_ms == pytest.approx(30.0)
        assert select.avg_ms == pytest.approx(20.0)
        assert by_sql["SELECT COUNT(*) AS n FROM items"].count == 1

        # Slowest total first, and stats alone never log.
        assert [s.sql for s in stats][0] == "SELECT name FROM items"
        assert not [r for r in caplog.records if "Slow query" in r.getMessage()]
        db.disconnect()

    def test_reset(self, temp_db_path):
        db = sqlite_manager(temp_db_path, collect_stats=True)
        db.execute("SELECT name FROM items")
        assert db.query_stats()
        db.reset_query_stats()
        assert db.query_stats() == []
        db.execute("SELECT 1")
        assert [s.sql for s in db.query_stats()] == ["SELECT 1"]
        db.disconnect()

    def test_query_stats_requires_opt_in(self, temp_db_path):
        db = sqlite_manager(temp_db_path)
        with pytest.raises(RuntimeError, match="collect_stats=True"):
            db.query_stats()
        db.reset_query_stats()  # harmless when nothing is collected
        db.disconnect()

        db = sqlite_manager(temp_db_path, slow_query_threshold_ms=1000)
        with pytest.raises(RuntimeError, match="collect_stats=True"):
            db.query_stats()
        db.disconnect()

    def test_record_is_thread_safe(self):
        monitor = QueryMonitor("sqlite", collect_stats=True)

        def hammer():
            for i in range(500):
                monitor.record("SELECT  1", None, float(i % 7), 1)
                monitor.record("SELECT 2", None, 1.0, 1)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        by_sql = {s.sql: s for s in monitor.query_stats()}
        assert by_sql["SELECT 1"].count == 4000
        assert by_sql["SELECT 1"].max_ms == 6.0
        assert by_sql["SELECT 2"].count == 4000
        assert by_sql["SELECT 2"].total_ms == pytest.approx(4000.0)

    def test_normalise_sql(self):
        assert normalise_sql("  SELECT\n\t 1   FROM  t ") == "SELECT 1 FROM t"


# ---------------------------------------------------------------------------
# explain() on SQLite
# ---------------------------------------------------------------------------

class TestExplainSqlite:
    def test_returns_plan_rows(self, temp_db_path):
        db = sqlite_manager(temp_db_path)
        rows = db.explain("SELECT name FROM items WHERE name = :n", {"n": "a"})
        assert rows
        for row in rows:
            assert row["plan"] == row["detail"]
            assert isinstance(row["plan"], str) and row["plan"]
        assert any("items" in r["plan"] for r in rows)
        db.disconnect()

    def test_analyze_returns_same_plan_without_running(self, temp_db_path):
        db = sqlite_manager(temp_db_path)
        plain = db.explain("SELECT name FROM items")
        analyzed = db.explain("SELECT name FROM items", analyze=True)
        assert [r["plan"] for r in analyzed] == [r["plan"] for r in plain]
        db.disconnect()

    def test_analyze_refuses_writes(self, temp_db_path):
        db = sqlite_manager(temp_db_path)
        for sql in [
            "DELETE FROM items",
            "INSERT INTO items (name) VALUES ('x')",
            "WITH gone AS (DELETE FROM items RETURNING id) SELECT * FROM gone",
            "UPDATE items SET name = 'x'",
        ]:
            with pytest.raises(ValueError, match="only SELECT"):
                db.explain(sql, analyze=True)
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 2}
        # A plain explain of a write only plans it.
        assert db.explain("DELETE FROM items WHERE name = :n", {"n": "a"})
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 2}
        db.disconnect()

    def test_inside_transaction_leaves_it_intact(self, temp_db_path):
        db = sqlite_manager(temp_db_path)
        with db.transaction():
            db.execute("INSERT INTO items (name) VALUES (:n)", {"n": "tx"})
            assert db.explain("SELECT name FROM items")
            assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 3}
        assert db.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 3}
        db.disconnect()

    def test_explain_is_not_counted_in_stats(self, temp_db_path):
        db = sqlite_manager(temp_db_path, collect_stats=True)
        db.reset_query_stats()
        db.explain("SELECT name FROM items")
        assert db.query_stats() == []
        db.disconnect()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_explain_sql_per_dialect(self):
        assert explain_sql(DatabaseType.POSTGRESQL, "SELECT 1;", False) == "EXPLAIN SELECT 1"
        assert explain_sql(DatabaseType.POSTGRESQL, "SELECT 1", True) == (
            "EXPLAIN (ANALYZE, FORMAT TEXT) SELECT 1")
        assert explain_sql(DatabaseType.SQLITE, " SELECT 1 ", False) == "EXPLAIN QUERY PLAN SELECT 1"
        assert explain_sql(DatabaseType.SQLITE, "SELECT 1", True) == "EXPLAIN QUERY PLAN SELECT 1"
        assert explain_sql(DatabaseType.MYSQL, "SELECT 1", False) == "EXPLAIN SELECT 1"
        assert explain_sql(DatabaseType.MYSQL, "SELECT 1", True) == "EXPLAIN ANALYZE SELECT 1"
        with pytest.raises(NotImplementedError):
            explain_sql(DatabaseType.MSSQL, "SELECT 1", False)
        with pytest.raises(NotImplementedError):
            explain_sql(DatabaseType.DUCKDB, "SELECT 1", False)

    def test_check_analyzable(self):
        check_analyzable("SELECT * FROM t")
        check_analyzable("  -- note\n select 1")
        check_analyzable("WITH a AS (SELECT 1) SELECT * FROM a")
        check_analyzable("SELECT updated_at, created_by FROM t")
        for bad in ["DELETE FROM t", "WITH a AS (UPDATE t SET x = 1) SELECT 1",
                    "CREATE TABLE t (a INT)", "", "EXEC sp_who"]:
            with pytest.raises(ValueError):
                check_analyzable(bad)

    def test_plan_text_per_backend(self):
        assert plan_text(DatabaseType.POSTGRESQL, {"QUERY PLAN": "Seq Scan"}) == "Seq Scan"
        assert plan_text(DatabaseType.SQLITE, {"id": 2, "detail": "SCAN t"}) == "SCAN t"
        assert plan_text(DatabaseType.MYSQL, {"EXPLAIN": "-> Table scan"}) == "-> Table scan"
        assert plan_text(DatabaseType.MYSQL, {"id": 1, "table": "t", "key": None, "rows": 3}) == (
            "id=1 table=t rows=3")
        assert plan_text(DatabaseType.MSSQL, {"StmtText": "SELECT ...", "StmtId": 1}) == "SELECT ..."
        assert plan_text(DatabaseType.MSSQL, {"Microsoft SQL Server 2005 XML Showplan": "<x/>"}) == "<x/>"
        assert plan_text(DatabaseType.DUCKDB, {"anything": "v"}) == "v"

    def test_row_count_shapes(self):
        assert row_count([{"a": 1}, {"a": 2}]) == 2
        assert row_count([]) == 0
        assert row_count({"a": 1}) == 1
        assert row_count(None) == 0
        assert row_count(42) is None
        from nexusql import create_query_result
        assert row_count(create_query_result(True, data=[{"a": 1}])) == 1


# ---------------------------------------------------------------------------
# MySQL and SQL Server explain() branches against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, result_sets=None, fail_on=None):
        self.executed = []
        # Each result set is (description, rows). The statement cursor and
        # the SET cursors share this object so the order of everything sent
        # on the connection can be asserted.
        self._sets = list(result_sets or [(None, [])])
        self.description = self._sets[0][0]
        self.fail_on = fail_on

    def execute(self, sql, params=None):
        self.executed.append((sql, params) if params is not None else sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")

    def nextset(self):
        self._sets.pop(0)
        if not self._sets:
            self.description = None
            return False
        self.description = self._sets[0][0]
        return True

    def fetchall(self):
        return list(self._sets[0][1])

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self, timeout=None):
        return self.conn

    def release(self, conn, *, discard=False):
        pass


def faked(db_type: str, cursor: FakeCursor) -> DatabaseManager:
    """A manager with dialect config whose pool hands out one fake connection."""
    urls = {"mysql": "mysql://u:p@localhost/db", "mssql": "mssql://u:p@localhost/db"}
    manager = DatabaseManager(ConnectionConfig.from_url(urls[db_type]))
    manager._pool = FakePool(FakeConnection(cursor))
    return manager


class TestMysqlBranch:
    def test_plain_explain_flattens_the_table(self):
        row = {"id": 1, "select_type": "SIMPLE", "table": "t", "type": "ALL",
               "key": None, "rows": 3, "Extra": None}
        cursor = FakeCursor([([("id",), ("select_type",)], [row])])
        db = faked("mysql", cursor)
        rows = db.explain("SELECT * FROM t WHERE id = :id", {"id": 1})
        assert cursor.executed == [("EXPLAIN SELECT * FROM t WHERE id = %s", (1,))]
        assert rows == [{**row, "plan": "id=1 select_type=SIMPLE table=t type=ALL rows=3"}]
        assert db._pool.conn.rollbacks == 1

    def test_analyze_uses_explain_analyze(self):
        cursor = FakeCursor([([("EXPLAIN",)], [{"EXPLAIN": "-> Table scan on t  (cost=0.55 rows=3)"}])])
        rows = faked("mysql", cursor).explain("SELECT * FROM t", analyze=True)
        assert cursor.executed == ["EXPLAIN ANALYZE SELECT * FROM t"]
        assert rows[0]["plan"] == "-> Table scan on t  (cost=0.55 rows=3)"

    def test_analyze_refuses_before_touching_the_connection(self):
        cursor = FakeCursor()
        with pytest.raises(ValueError):
            faked("mysql", cursor).explain("DELETE FROM t", analyze=True)
        assert cursor.executed == []


class TestMssqlBranch:
    SHOWPLAN_DESC = [("StmtText",), ("StmtId",), ("NodeId",), ("PhysicalOp",)]

    def test_showplan_all_wraps_the_statement(self):
        cursor = FakeCursor([(self.SHOWPLAN_DESC, [
            ("SELECT * FROM t WHERE id = @P1", 1, 1, None),
            ("  |--Clustered Index Scan", 1, 2, "Clustered Index Scan"),
        ])])
        db = faked("mssql", cursor)
        rows = db.explain("SELECT * FROM t WHERE id = :id", {"id": 1})
        assert cursor.executed == [
            "SET SHOWPLAN_ALL ON",
            ("SELECT * FROM t WHERE id = ?", (1,)),
            "SET SHOWPLAN_ALL OFF",
        ]
        assert [r["plan"] for r in rows] == [
            "SELECT * FROM t WHERE id = @P1", "  |--Clustered Index Scan"]
        assert rows[1]["PhysicalOp"] == "Clustered Index Scan"
        assert db._pool.conn.rollbacks == 1

    def test_showplan_is_switched_off_after_an_error(self):
        cursor = FakeCursor(fail_on="FROM broken")
        with pytest.raises(RuntimeError, match="boom"):
            faked("mssql", cursor).explain("SELECT * FROM broken")
        assert cursor.executed == [
            "SET SHOWPLAN_ALL ON", "SELECT * FROM broken", "SET SHOWPLAN_ALL OFF"]

    def test_statistics_xml_collects_the_showplan_result_set(self):
        xml = "<ShowPlanXML xmlns='http://schemas.microsoft.com/sqlserver/2004/07/showplan'/>"
        cursor = FakeCursor([
            ([("id",), ("name",)], [(1, "a"), (2, "b")]),
            ([("Microsoft SQL Server 2005 XML Showplan",)], [(xml,)]),
        ])
        rows = faked("mssql", cursor).explain("SELECT id, name FROM t", analyze=True)
        assert cursor.executed == [
            "SET STATISTICS XML ON", "SELECT id, name FROM t", "SET STATISTICS XML OFF"]
        assert rows == [{"Microsoft SQL Server 2005 XML Showplan": xml, "plan": xml}]

    def test_analyze_refuses_writes(self):
        cursor = FakeCursor()
        with pytest.raises(ValueError):
            faked("mssql", cursor).explain("UPDATE t SET a = 1", analyze=True)
        assert cursor.executed == []


# ---------------------------------------------------------------------------
# Async PostgreSQL backend against a fake connection
# ---------------------------------------------------------------------------

class AsyncFakeCursor:
    def __init__(self, log, rows):
        self.log = log
        self.rows = rows
        self.description = [("QUERY PLAN",)] if rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append((sql, params))

    async def fetchall(self):
        return list(self.rows)


class AsyncFakeConnection:
    def __init__(self, rows):
        self.log = []
        self.rows = rows
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return AsyncFakeCursor(self.log, self.rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def async_backend_with(conn, **options):
    from contextlib import asynccontextmanager
    from nexusql.async_backend import AsyncPostgresBackend

    backend = AsyncPostgresBackend("postgresql://u:p@localhost/db", **options)

    @asynccontextmanager
    async def borrow():
        yield conn

    backend._borrow = borrow
    return backend


class TestAsyncBackend:
    async def test_execute_is_timed(self, clock):
        seen = []
        conn = AsyncFakeConnection([{"QUERY PLAN": "x"}, {"QUERY PLAN": "y"}])
        backend = async_backend_with(conn, slow_query_threshold_ms=100,
                                     on_slow_query=seen.append, collect_stats=True)
        clock(0.200)
        rows = await backend.execute("SELECT * FROM t WHERE id = :id", {"id": 3})
        assert len(rows) == 2
        assert len(seen) == 1
        assert seen[0].backend == "postgresql"
        assert seen[0].rows == 2
        assert seen[0].duration_ms == pytest.approx(200.0)
        assert seen[0].params == {"id": 3}
        assert backend.query_stats()[0].sql == "SELECT * FROM t WHERE id = :id"

        # fetch_one runs through execute, so it is timed once, not twice.
        await backend.fetch_one("SELECT 1")
        assert len(seen) == 2 and seen[1].rows == 2

    async def test_disabled_by_default(self):
        conn = AsyncFakeConnection([])
        backend = async_backend_with(conn)
        assert backend._monitor is None
        assert await backend.execute("SELECT 1") == []
        with pytest.raises(RuntimeError, match="collect_stats=True"):
            backend.query_stats()

    async def test_explain(self):
        conn = AsyncFakeConnection([{"QUERY PLAN": "Seq Scan on t"}])
        backend = async_backend_with(conn)
        rows = await backend.explain("SELECT * FROM t WHERE id = :id;", {"id": 1})
        assert conn.log == [("EXPLAIN SELECT * FROM t WHERE id = %(id)s", {"id": 1})]
        assert rows == [{"QUERY PLAN": "Seq Scan on t", "plan": "Seq Scan on t"}]
        assert conn.rollbacks == 1 and conn.commits == 0

        await backend.explain("SELECT 1", analyze=True)
        assert conn.log[-1][0] == "EXPLAIN (ANALYZE, FORMAT TEXT) SELECT 1"

        with pytest.raises(ValueError):
            await backend.explain("DELETE FROM t", analyze=True)
