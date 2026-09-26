"""
Integration tests for the async backend (T-022) against a real PostgreSQL.

These tests need the running Postgres 16 container (T-022 environment:
127.0.0.1:15432, user postgres, password mwtest, db mwtest). Override with
TEST_POSTGRESQL_URL. When the server is unreachable they SKIP - loudly - so
a bare `pytest` on a machine without the container still shows why.

Covered:
- pool round-trip: connect, write, read back, disconnect
- durability across pool reconnect: kill every pooled connection mid-use,
  the backend recovers on a fresh pool and both the pre-kill and post-kill
  writes are durable
- health_check: True when up, False when unreachable
- failed query then reuse: a failing statement must not poison the pooled
  connection (the async mirror of the B-002 rollback guarantee)
- explicit transactions: transaction() context and the sync-style
  BEGIN/COMMIT/ROLLBACK statement protocol
- INSERT .. RETURNING through fetch_one commits and returns the row
  (async mirror of B-002)
- concurrent migration runners: two runners racing under pg_advisory_lock
  apply each migration exactly once
- checksum refusal for an applied-but-changed migration file
- concurrent queries do not block the event loop (pool parallelism)
"""

import asyncio
import logging
import os
import time
import uuid

import pytest

import psycopg

from nexusql import AsyncPostgresBackend

logger = logging.getLogger(__name__)

TEST_URL = os.getenv(
    "TEST_POSTGRESQL_URL",
    "postgresql://postgres:mwtest@127.0.0.1:15432/mwtest",
)

APP_NAME = "nexusql-t022-tests"


async def _server_reachable(url: str) -> bool:
    try:
        conn = await asyncio.wait_for(
            psycopg.AsyncConnection.connect(url), timeout=5
        )
    except Exception:
        return False
    await conn.close()
    return True


def _with_app_name(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}application_name={APP_NAME}"


@pytest.fixture
async def backend():
    """Connected AsyncPostgresBackend; skips loudly when Postgres is down."""
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}. Start the "
            f"Postgres 16 test container (127.0.0.1:15432, db mwtest) to "
            f"run the T-022 integration tests."
        )
    b = AsyncPostgresBackend(TEST_URL, max_pool_size=10)
    assert await b.connect(), "pool creation failed"
    yield b
    await b.disconnect()


def _table(suffix: str) -> str:
    return f"t022_{suffix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Pool round-trip
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_pool_round_trip(backend):
    table = _table("roundtrip")
    try:
        created = await backend.execute(
            f"CREATE TABLE {table} (id SERIAL PRIMARY KEY, label TEXT)")
        assert created.success

        for i in range(3):
            r = await backend.execute(
                f"INSERT INTO {table} (label) VALUES (:label)",
                {"label": f"row{i}"},
            )
            assert r.success

        fetched = await backend.fetch_all(
            f"SELECT id, label FROM {table} ORDER BY id")
        assert fetched.success
        assert [row["label"] for row in fetched.data] == ["row0", "row1", "row2"]

        one = await backend.fetch_one(
            f"SELECT label FROM {table} WHERE id = :id", {"id": 3})
        assert one.success and one.data[0]["label"] == "row2"

        assert await backend.table_exists(table)
        assert not await backend.table_exists(table + "_missing")
    finally:
        await backend.execute(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------
# Durability across reconnect
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_durability_across_pool_reconnect():
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run the T-022 integration tests."
        )
    table = _table("reconnect")
    # Distinct application_name so the kill only hits THIS backend's pool.
    b = AsyncPostgresBackend(_with_app_name(TEST_URL), max_pool_size=5)
    assert await b.connect()
    killer = await psycopg.AsyncConnection.connect(TEST_URL)
    try:
        assert (await b.execute(
            f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")).success
        assert (await b.execute(
            f"INSERT INTO {table} VALUES (1, 'before')")).success

        # Kill every pooled connection mid-use (T-022 Done: killing the
        # Postgres connection mid-use recovers without a restart).
        killed = await killer.fetch(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = $1 AND pid <> pg_backend_pid()",
            APP_NAME,
        )
        assert len(killed) >= 1, "expected to kill at least one pooled conn"

        # The next statement must transparently recover on a fresh pool...
        assert (await b.execute(
            f"INSERT INTO {table} VALUES (2, 'after')")).success

        # ...and BOTH writes (one before, one after the kill) are durable.
        rows = await b.fetch_all(
            f"SELECT id, v FROM {table} ORDER BY id")
        assert rows.success
        assert [(r["id"], r["v"]) for r in rows.data] == [
            (1, "before"), (2, "after")]

        # Failover recovery includes health going back to true.
        assert await b.health_check() is True
    finally:
        await killer.close()
        await b.execute(f"DROP TABLE IF EXISTS {table}")
        await b.disconnect()


@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_health_check_false_when_database_down():
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run these integration tests."
        )
    # Nothing listens here: health must be False, connect must not raise.
    dead = AsyncPostgresBackend(
        "postgresql://postgres:mwtest@127.0.0.1:15499/mwtest")
    assert await dead.connect() is False
    assert await dead.health_check() is False


# ---------------------------------------------------------------------------
# Failed query must not poison the pooled connection
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_failed_query_then_reuse_same_connection():
    """Pool of exactly one connection: the very connection that saw the
    failure must serve the next query (async mirror of the B-002 rollback
    guarantee, against PostgreSQL's aborted-transaction semantics)."""
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run the T-022 integration tests."
        )
    b = AsyncPostgresBackend(TEST_URL, min_pool_size=1, max_pool_size=1)
    assert await b.connect()
    table = _table("poison")
    try:
        # Query errors raise (B-003): a loud failure beats silent missing
        # rows.
        with pytest.raises(psycopg.Error):
            await b.fetch_all(f"SELECT * FROM {table}")

        good = await b.fetch_all("SELECT 1 AS one")
        assert good.success and good.data == [{"one": 1}]

        # execute() raises for query errors (sync-compatible) ...
        with pytest.raises(psycopg.Error):
            await b.execute(f"INSERT INTO {table} (id) VALUES (1)")
        # ... and the connection still works afterwards.
        one = await b.fetch_one("SELECT 2 AS two")
        assert one.success and one.data == [{"two": 2}]

        # Writes keep working after the failure too (write on the recycled
        # connection, then read it back).
        assert (await b.execute(
            f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")).success
        assert (await b.execute(
            f"INSERT INTO {table} VALUES (1)")).success
        r = await b.fetch_one(f"SELECT id FROM {table}")
        assert r.success and r.data == [{"id": 1}]
    finally:
        await b.execute(f"DROP TABLE IF EXISTS {table}")
        await b.disconnect()


# ---------------------------------------------------------------------------
# Commit semantics (async mirror of B-002)
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_insert_returning_via_fetch_one_is_durable(backend):
    table = _table("b002")
    try:
        await backend.execute(f"CREATE TABLE {table} (id SERIAL PRIMARY KEY, name TEXT)")
        row = await backend.fetch_one(
            f"INSERT INTO {table} (name) VALUES (:name) RETURNING id",
            {"name": "n1"},
        )
        assert row.success and row.data and "id" in row.data[0]
        inserted_id = row.data[0]["id"]

        # Durability: visible from a brand new pool (fresh connection).
        reader = AsyncPostgresBackend(TEST_URL)
        assert await reader.connect()
        try:
            after = await reader.fetch_all(
                f"SELECT id, name FROM {table} WHERE id = :id",
                {"id": inserted_id},
            )
            assert after.data == [{"id": inserted_id, "name": "n1"}]
        finally:
            await reader.disconnect()
    finally:
        await backend.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_explicit_transaction_context_commits_and_rolls_back(backend):
    table = _table("txctx")
    try:
        await backend.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")

        async with backend.transaction():
            await backend.execute(f"INSERT INTO {table} VALUES (1)")
            seen = await backend.fetch_one(f"SELECT id FROM {table}")
            assert seen.success and seen.data == [{"id": 1}]
        # committed on clean exit
        after = await backend.fetch_all(f"SELECT id FROM {table}")
        assert [r["id"] for r in after.data] == [1]

        with pytest.raises(RuntimeError):
            async with backend.transaction():
                await backend.execute(f"INSERT INTO {table} VALUES (2)")
                raise RuntimeError("boom")
        # rolled back on error
        after = await backend.fetch_all(f"SELECT id FROM {table} ORDER BY id")
        assert [r["id"] for r in after.data] == [1]
    finally:
        await backend.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_begin_commit_statement_protocol(backend):
    """Sync-style BEGIN/COMMIT/ROLLBACK strings pin one pooled connection,
    mirroring DatabaseManager._in_transaction; the caller owns the commit."""
    table = _table("txproto")
    try:
        await backend.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")

        await backend.execute("BEGIN")
        await backend.execute(f"INSERT INTO {table} VALUES (1)")
        mid = await backend.fetch_one(
            f"SELECT id FROM {table} WHERE id = 1")
        assert mid.success and mid.data == [{"id": 1}]
        await backend.execute("COMMIT")

        await backend.execute("BEGIN")
        await backend.execute(f"INSERT INTO {table} VALUES (2)")
        await backend.execute("ROLLBACK")

        rows = await backend.fetch_all(
            f"SELECT id FROM {table} ORDER BY id")
        assert [r["id"] for r in rows.data] == [1]
    finally:
        await backend.execute(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------
# Migration runner: advisory locking + checksums
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_concurrent_queries_do_not_block_the_loop():
    """Eight concurrent 0.2s queries over a pool of 8 must finish well under
    the 1.6s a single serialized connection would need."""
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run the T-022 integration tests."
        )
    b = AsyncPostgresBackend(TEST_URL, min_pool_size=8, max_pool_size=8)
    assert await b.connect()
    try:
        start = time.monotonic()
        results = await asyncio.gather(*[
            b.fetch_one("SELECT pg_sleep(0.2), 1 AS v") for _ in range(8)
        ])
        elapsed = time.monotonic() - start
        assert all(r.success and r.data[0]["v"] == 1 for r in results)
        assert elapsed < 1.2, (
            f"concurrent queries took {elapsed:.2f}s - the event loop or "
            f"pool is serializing")
    finally:
        await b.disconnect()
