"""
Query performance tools against live backends.

explain() has to return at least one plan row with a ``plan`` text column on
every backend, slow-query logging has to report the backend's name and the
row count, and query_stats() has to aggregate the same statement written
with different whitespace. Backends that are not reachable are skipped at
collection (see conftest).
"""

import logging
import os
import uuid

import pytest

from nexusql import DatabaseManager, QueryTiming

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


def table_name() -> str:
    return "perf_" + uuid.uuid4().hex[:10]


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL)"
    )
    for value in ("one", "two", "three"):
        db_manager.execute(f"INSERT INTO {name} (name) VALUES (:name)", {"name": value})
    yield name
    try:
        db_manager.execute(f"DROP TABLE {name}")
    except Exception:
        pass


@pytest.fixture
def monitored(db_manager):
    """A second manager on the same database with every performance option on."""
    seen = []
    db = DatabaseManager(
        db_manager.database_url,
        slow_query_threshold_ms=0,
        on_slow_query=seen.append,
        collect_stats=True,
    )
    assert db.connect()
    yield db, seen
    db.disconnect()


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestExplain:
    def test_returns_plan_rows(self, db_manager, table):
        rows = db_manager.explain(
            f"SELECT name FROM {table} WHERE id = :id", {"id": 1}
        )
        assert rows
        plans = [r["plan"] for r in rows]
        assert all(isinstance(p, str) for p in plans)
        assert any(p.strip() for p in plans)
        # The backend's own columns travel alongside the normalised one.
        assert all(len(r) >= 2 for r in rows)
        # The connection is clean for ordinary work afterwards.
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 3}

    def test_analyze_select(self, db_manager, table):
        rows = db_manager.explain(
            f"SELECT name FROM {table} WHERE name = :name", {"name": "two"},
            analyze=True,
        )
        assert rows and any(r["plan"].strip() for r in rows)
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 3}

    def test_analyze_refuses_writes(self, db_manager, table):
        with pytest.raises(ValueError, match="only SELECT"):
            db_manager.explain(f"DELETE FROM {table}", analyze=True)
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 3}

    def test_plain_explain_of_a_write_does_not_run_it(self, db_manager, table):
        rows = db_manager.explain(
            f"DELETE FROM {table} WHERE name = :name", {"name": "one"}
        )
        assert rows
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 3}

    def test_inside_transaction(self, db_manager, table):
        with db_manager.transaction():
            db_manager.execute(
                f"INSERT INTO {table} (name) VALUES (:name)", {"name": "four"}
            )
            assert db_manager.explain(f"SELECT name FROM {table}")
            assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 4}
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 4}


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestSlowQueryLogging:
    def test_callback_and_log(self, db_manager, monitored, table, caplog):
        db, seen = monitored
        with caplog.at_level(logging.WARNING, logger="nexusql"):
            rows = db.execute(
                f"SELECT name FROM {table} WHERE name <> :skip ORDER BY id",
                {"skip": "secret-value"},
            )
        assert len(rows) == 3

        assert len(seen) == 1
        timing = seen[0]
        assert isinstance(timing, QueryTiming)
        assert timing.backend == db_manager.config.database_type.value
        assert timing.rows == 3
        assert timing.params == {"skip": "secret-value"}
        assert timing.duration_ms >= 0

        records = [r for r in caplog.records if "Slow query" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        assert records[0].name.startswith("nexusql")
        assert timing.backend in message
        assert "secret-value" not in message

    def test_fetch_one_is_timed(self, monitored, table):
        db, seen = monitored
        assert db.fetch_one(f"SELECT name FROM {table} WHERE id = :id", {"id": 2}) == {"name": "two"}
        assert db.fetch_one(f"SELECT name FROM {table} WHERE id = :id", {"id": 99}) is None
        assert [t.rows for t in seen] == [1, 0]


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestQueryStats:
    def test_aggregates(self, monitored, table):
        db, _ = monitored
        db.reset_query_stats()
        db.execute(f"SELECT name FROM {table}")
        db.execute(f"SELECT   name\n FROM {table}")
        db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")

        by_sql = {s.sql: s for s in db.query_stats()}
        assert set(by_sql) == {
            f"SELECT name FROM {table}", f"SELECT COUNT(*) AS n FROM {table}"}
        stat = by_sql[f"SELECT name FROM {table}"]
        assert stat.count == 2
        assert stat.total_ms >= stat.max_ms >= 0
        assert by_sql[f"SELECT COUNT(*) AS n FROM {table}"].count == 1

        db.reset_query_stats()
        assert db.query_stats() == []


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

    seen = []
    backend = AsyncPostgresBackend(
        ASYNC_URL, max_pool_size=4,
        slow_query_threshold_ms=0, on_slow_query=seen.append, collect_stats=True,
    )
    try:
        connected = await backend.connect()
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable for async tests: {exc}")
    if not connected:
        pytest.skip("PostgreSQL not reachable for async tests")
    yield backend, seen
    await backend.disconnect()


async def test_async_backend_performance_tools(async_backend, caplog):
    backend, seen = async_backend
    name = table_name()
    try:
        await backend.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name TEXT)")
        await backend.execute(f"INSERT INTO {name} (name) VALUES (:n)", {"n": "a"})
        backend.reset_query_stats()
        seen.clear()

        with caplog.at_level(logging.WARNING, logger="nexusql"):
            rows = await backend.execute(f"SELECT name FROM {name} WHERE name = :n", {"n": "a"})
        assert rows == [{"name": "a"}]
        assert len(seen) == 1
        assert seen[0].backend == "postgresql" and seen[0].rows == 1
        assert seen[0].params == {"n": "a"}
        assert [r for r in caplog.records if "Slow query" in r.getMessage()]

        result = await backend.fetch_one(f"SELECT name FROM {name}")
        assert result.data == [{"name": "a"}]
        assert len(seen) == 2

        stats = backend.query_stats()
        assert {s.sql for s in stats} == {
            f"SELECT name FROM {name} WHERE name = :n", f"SELECT name FROM {name}"}

        plan = await backend.explain(f"SELECT name FROM {name} WHERE name = :n", {"n": "a"})
        assert plan and all("QUERY PLAN" in r and r["plan"] for r in plan)

        analyzed = await backend.explain(
            f"SELECT name FROM {name} WHERE name = :n", {"n": "a"}, analyze=True)
        assert any("actual time" in r["plan"] for r in analyzed)

        with pytest.raises(ValueError, match="only SELECT"):
            await backend.explain(f"DELETE FROM {name}", analyze=True)

        # explain() is not a query method: it stays out of the stats.
        assert len(backend.query_stats()) == 2
        assert await backend.execute(f"SELECT COUNT(*) AS n FROM {name}") == [{"n": 1}]
    finally:
        await backend.execute(f"DROP TABLE IF EXISTS {name}")
