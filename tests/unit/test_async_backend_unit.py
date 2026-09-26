"""
Unit tests for the async backend - no database server needed.

Covers:
- PostgreSQL dialect translation used by the async path
- parameter style parity with the sync path: psycopg takes %(name)s on both,
  so there is no separate conversion to test
- backward compatibility: the package still imports (with the full public
  API) when the postgresql extra is NOT installed
- the sync manager's async entry points
"""

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

import nexusql
from nexusql import ConnectionConfig, DatabaseManager, DatabaseType
from nexusql.async_backend import (
    AsyncPostgresBackend,
    _translate_sql_for_postgres,
)


def pg_manager():
    return DatabaseManager(ConnectionConfig(
        database_type=DatabaseType.POSTGRESQL,
        database_url="postgresql://host/db",
    ))


# ---------------------------------------------------------------------------
# Dialect translation
# ---------------------------------------------------------------------------

def test_translate_sql_postgres_dialect():
    assert _translate_sql_for_postgres(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"
    ) == "CREATE TABLE t (id SERIAL PRIMARY KEY, name TEXT)"
    assert _translate_sql_for_postgres(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)"
    ) == "CREATE TABLE t (id SERIAL PRIMARY KEY)"
    assert _translate_sql_for_postgres(
        "SELECT GROUP_CONCAT(name) FROM t"
    ) == "SELECT STRING_AGG(name::text, ',') FROM t"


def test_translation_matches_sync_manager():
    """Both paths share one translator, so they must agree exactly."""
    sync = pg_manager()
    for sql in [
        "CREATE TABLE t (id INTEGER PRIMARY KEY)",
        "SELECT GROUP_CONCAT(x) FROM t",
        "SELECT a::int FROM t WHERE id = :id",
    ]:
        assert _translate_sql_for_postgres(sql) == sync._translate_sql(sql)


# ---------------------------------------------------------------------------
# Parameter style
# ---------------------------------------------------------------------------

def test_named_params_convert_to_pyformat():
    """psycopg takes %(name)s, which both paths now share."""
    converted, params = pg_manager()._convert_params(
        "SELECT * FROM t WHERE id = :id AND org = :id_org",
        {"id": 1, "id_org": 2},
    )
    assert "%(id)s" in converted
    assert "%(id_org)s" in converted
    assert params == {"id": 1, "id_org": 2}


def test_prefix_names_stay_distinct():
    """:id must not match the start of :id_org."""
    converted, _ = pg_manager()._convert_params(
        "SELECT :id, :id_org", {"id": 1, "id_org": 2}
    )
    assert converted == "SELECT %(id)s, %(id_org)s"


def test_casts_are_not_parameters():
    """A ::cast must never be read as a :parameter."""
    converted, _ = pg_manager()._convert_params(
        "SELECT val::text FROM t WHERE id = :id", {"id": 1}
    )
    assert "::text" in converted
    assert "%(text)s" not in converted
    assert converted.count("%(id)s") == 1


def test_unknown_token_left_untouched():
    converted, _ = pg_manager()._convert_params(
        "SELECT :known, :unknown", {"known": 1}
    )
    assert "%(known)s" in converted
    assert ":unknown" in converted


# ---------------------------------------------------------------------------
# One PostgreSQL driver
# ---------------------------------------------------------------------------

def test_no_asyncpg_dependency():
    """psycopg covers sync and async, so asyncpg is not imported anywhere."""
    import nexusql.async_backend as ab
    import nexusql.manager as mgr
    assert not hasattr(ab, 'asyncpg')
    assert not hasattr(mgr, 'asyncpg')
    assert not hasattr(mgr, 'psycopg2')


def test_both_paths_use_psycopg():
    import nexusql.async_backend as ab
    import nexusql.manager as mgr
    assert hasattr(ab, 'psycopg')
    assert hasattr(mgr, 'psycopg')


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_public_api_backward_compatible():
    """The pre-existing public surface must stay exactly where it was."""
    for name in ("DatabaseManager", "ConnectionConfig", "DatabaseType",
                 "QueryResult", "DatabaseInterface",
                 "create_query_result", "create_error_result"):
        assert hasattr(nexusql, name), f"missing {name}"
        assert name in nexusql.__all__

    if nexusql.PSYCOPG_AVAILABLE:
        from nexusql.interfaces import DatabaseInterface
        assert issubclass(AsyncPostgresBackend, DatabaseInterface)
        # Instantiable without a server: nothing abstract left unimplemented.
        backend = AsyncPostgresBackend("postgresql://user:pw@localhost:5432/db")
        assert backend.db_type == nexusql.DatabaseType.POSTGRESQL


def test_package_importable_without_psycopg():
    """Consumers install this without the postgresql extra; the package must
    keep importing (sync-only) when psycopg is absent."""
    repo_root = str(Path(nexusql.__file__).resolve().parent.parent)
    code = (
        "import sys; sys.modules['psycopg'] = None; "
        "import nexusql; "
        "assert nexusql.PSYCOPG_AVAILABLE is False; "
        "assert not hasattr(nexusql, 'AsyncPostgresBackend'); "
        "assert nexusql.DatabaseManager is not None; "
        "print('ok')"
    )
    env = dict(os.environ, PYTHONPATH=repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def test_backend_rejects_non_postgres_url():
    with pytest.raises(ValueError):
        AsyncPostgresBackend("sqlite:///tmp/x.db")


# ---------------------------------------------------------------------------
# The sync manager's async entry points
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_async_is_genuinely_async(tmp_path):
    """execute_async must return a real coroutine, not a completed value."""
    db = DatabaseManager(f"sqlite:///{tmp_path / 'exec_async.db'}")
    assert db.connect()

    coro = db.execute_async("CREATE TABLE t (a INTEGER)")
    assert inspect.iscoroutine(coro)
    result = await coro
    assert isinstance(result, list)

    await db.execute_async("INSERT INTO t (a) VALUES (:a)", {"a": 1})
    assert await db.execute_async("SELECT a FROM t") == [{"a": 1}]

    db.disconnect()


@pytest.mark.asyncio
async def test_fetch_one_async(tmp_path):
    db = DatabaseManager(f"sqlite:///{tmp_path / 'fetch_async.db'}")
    assert db.connect()
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t (a) VALUES (:a)", {"a": 7})

    assert inspect.iscoroutinefunction(db.fetch_one_async)
    assert await db.fetch_one_async("SELECT a FROM t") == {"a": 7}
    assert await db.fetch_one_async(
        "SELECT a FROM t WHERE a = :a", {"a": 99}
    ) is None

    db.disconnect()
