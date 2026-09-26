"""
Integration regression tests for B-003 against a real PostgreSQL: a failed
fetch must raise - including when the connection dies mid-fetch - never
return None/[] (sync) or a success=False QueryResult (async).

Cases:
- missing table through fetch_one/fetch_all, sync and async: the driver's
  error must surface
- connection killed mid-fetch (pg_terminate_backend from a second session):
  the error must surface, and the server must still serve fresh connections
- async: pool recreated and usable again after a mid-fetch kill (reconnect
  preserved), and when the retry itself is killed too, the exception still
  surfaces (never an empty result)

The sync tests use the suite-wide postgresql_url/skip_if_no_postgresql
fixtures (TEST_POSTGRESQL_URL); the async tests target the T-022 container
(127.0.0.1:15432, user postgres, password mwtest, db mwtest) and skip
loudly when it is down, like tests/integration/test_async_postgres.py.
"""

import asyncio
import os
import threading
import time
import uuid
from urllib.parse import urlparse

import psycopg
import pytest

from nexusql import AsyncPostgresBackend, DatabaseManager

# Connection-level failures, as opposed to query errors.
CONNECTION_ERRORS = (psycopg.OperationalError, ConnectionError, OSError)

TEST_URL = os.getenv(
    "TEST_POSTGRESQL_URL",
    "postgresql://postgres:mwtest@127.0.0.1:15432/mwtest",
)


async def _server_reachable(url: str) -> bool:
    try:
        conn = await asyncio.wait_for(
            psycopg.AsyncConnection.connect(url), timeout=5
        )
    except Exception:
        return False
    await conn.close()
    return True


def _admin_connect(url: str):
    parsed = urlparse(url)
    return psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        dbname=parsed.path.lstrip("/"),
    )


def _terminate_backends_like(admin, pattern: str) -> None:
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE query LIKE %s AND pid <> pg_backend_pid()",
            (f"%{pattern}%",),
        )
    admin.commit()


# ---------------------------------------------------------------------------
# Sync backend
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
def test_sync_fetch_missing_table_raises(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect(), "Postgres connect failed"
    try:
        with pytest.raises(psycopg.ProgrammingError):
            db.fetch_one("SELECT * FROM b003_missing WHERE x = :x", {"x": 1})
        with pytest.raises(psycopg.ProgrammingError):
            db.execute("SELECT * FROM b003_missing WHERE x = :x", {"x": 1})
        ok = db.fetch_one("SELECT 1 AS one")
        assert ok == {"one": 1}
    finally:
        db.disconnect()


@pytest.mark.integration
@pytest.mark.postgresql
def test_sync_fetch_no_rows_still_none_and_empty(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect(), "Postgres connect failed"
    try:
        assert db.fetch_one("SELECT 1 AS one WHERE 1 = :f", {"f": 0}) is None
        assert db.execute("SELECT 1 AS one WHERE 1 = :f", {"f": 0}) == []
    finally:
        db.disconnect()


@pytest.mark.integration
@pytest.mark.postgresql
def test_sync_fetch_all_killed_mid_fetch_raises(postgresql_url, skip_if_no_postgresql):
    marker = f"b003_kill_{uuid.uuid4().hex[:8]}"

    def kill_later():
        time.sleep(0.7)
        admin = _admin_connect(postgresql_url)
        admin.autocommit = True
        try:
            _terminate_backends_like(admin, marker)
        finally:
            admin.close()

    db = DatabaseManager(postgresql_url)
    assert db.connect(), "Postgres connect failed"
    killer = threading.Thread(target=kill_later)
    killer.start()
    try:
        with pytest.raises(psycopg.Error):
            db.execute(f"SELECT pg_sleep(3) AS s /* {marker} */")
    finally:
        killer.join()
        db.disconnect()

    fresh = DatabaseManager(postgresql_url)
    assert fresh.connect(), "Postgres reconnect failed"
    try:
        assert fresh.fetch_one("SELECT 1 AS one") == {"one": 1}
    finally:
        fresh.disconnect()


# ---------------------------------------------------------------------------
# Async backend
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_async_fetch_missing_table_raises():
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run the B-003 integration tests."
        )
    b = AsyncPostgresBackend(TEST_URL, min_pool_size=1, max_pool_size=1)
    assert await b.connect()
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            await b.fetch_one("SELECT * FROM b003_missing WHERE x = :x", {"x": 1})
        with pytest.raises(psycopg.errors.UndefinedTable):
            await b.execute("SELECT * FROM b003_missing WHERE x = :x", {"x": 1})
        ok = await b.fetch_one("SELECT 1 AS one")
        assert ok.success and ok.data == [{"one": 1}]
    finally:
        await b.disconnect()


@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_async_fetch_all_killed_mid_fetch_raises():
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run the B-003 integration tests."
        )
    marker = f"b003_kill_{uuid.uuid4().hex[:8]}"
    b = AsyncPostgresBackend(
        TEST_URL, min_pool_size=1, max_pool_size=1, max_reconnect_attempts=0
    )
    assert await b.connect()

    async def kill_later():
        await asyncio.sleep(0.7)
        admin = await psycopg.AsyncConnection.connect(TEST_URL)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE query LIKE $1 AND pid <> pg_backend_pid()",
                f"%{marker}%",
            )
        finally:
            await admin.close()

    task = asyncio.create_task(kill_later())
    try:
        with pytest.raises(CONNECTION_ERRORS):
            await b.execute(f"SELECT pg_sleep(3) AS s /* {marker} */")
    finally:
        await task

    ok = await b.fetch_one("SELECT 1 AS one")
    assert ok.success and ok.data == [{"one": 1}]
    await b.disconnect()


@pytest.mark.integration
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_async_kill_after_reconnect_still_raises():
    """The reconnect may be attempted, but if the retry also fails the
    exception must surface - never an empty/None result."""
    if not await _server_reachable(TEST_URL):
        pytest.skip(
            f"SKIPPED - PostgreSQL unreachable at {TEST_URL}; start the "
            f"Postgres 16 test container to run the B-003 integration tests."
        )
    marker = f"b003_kill_{uuid.uuid4().hex[:8]}"
    b = AsyncPostgresBackend(
        TEST_URL, min_pool_size=1, max_pool_size=1, max_reconnect_attempts=1
    )
    assert await b.connect()

    async def keep_killing():
        admin = await psycopg.AsyncConnection.connect(TEST_URL)
        try:
            for _ in range(6):
                await asyncio.sleep(0.5)
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE query LIKE $1 AND pid <> pg_backend_pid()",
                    f"%{marker}%",
                )
        finally:
            await admin.close()

    task = asyncio.create_task(keep_killing())
    try:
        with pytest.raises(CONNECTION_ERRORS):
            await b.execute(f"SELECT pg_sleep(3) AS s /* {marker} */")
    finally:
        await task

    await b.disconnect()
