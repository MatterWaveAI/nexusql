"""
Nested transaction() blocks as savepoints, against live backends.

The same code must behave identically on SQLite, PostgreSQL, MySQL and SQL
Server: an inner block that fails is undone on its own, the outer block's
rows survive and its commit works. Backends that are not reachable are
skipped at collection (see conftest). The async PostgreSQL backend is
covered at the end of the file and skips when the server is down.
"""

import os
import uuid

import pytest

from nexusql import DatabaseManager, PSYCOPG_AVAILABLE
from tests.conftest import backend_reachable

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


def table_name() -> str:
    return "svp_" + uuid.uuid4().hex[:10]


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL UNIQUE)"
    )
    yield name
    try:
        db_manager.execute(f"DROP TABLE {name}")
    except Exception:
        pass


def insert(db, table, name):
    db.execute(f"INSERT INTO {table} (name) VALUES (:name)", {"name": name})


def names(db, table):
    return [r["name"] for r in db.execute(f"SELECT name FROM {table} ORDER BY id")]


def names_elsewhere(url, table):
    """What a second connection sees: committed rows only."""
    reader = DatabaseManager(url)
    assert reader.connect()
    try:
        return names(reader, table)
    finally:
        reader.disconnect()


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestSavepoints:
    def test_inner_failure_leaves_outer_rows(self, db_manager, table):
        with db_manager.transaction():
            insert(db_manager, table, "a")
            with pytest.raises(Exception):
                with db_manager.transaction():
                    insert(db_manager, table, "b")
                    insert(db_manager, table, "a")
            insert(db_manager, table, "c")
        assert names_elsewhere(db_manager.database_url, table) == ["a", "c"]

    def test_inner_python_error_is_undone(self, db_manager, table):
        with db_manager.transaction():
            insert(db_manager, table, "a")
            try:
                with db_manager.transaction():
                    insert(db_manager, table, "b")
                    raise ValueError("boom")
            except ValueError:
                pass
        assert names_elsewhere(db_manager.database_url, table) == ["a"]

    def test_three_deep_nesting(self, db_manager, table):
        with db_manager.transaction():
            insert(db_manager, table, "a")
            with db_manager.transaction():
                insert(db_manager, table, "b")
                with pytest.raises(Exception):
                    with db_manager.transaction():
                        insert(db_manager, table, "c")
                        insert(db_manager, table, "a")
                insert(db_manager, table, "d")
        assert names_elsewhere(db_manager.database_url, table) == ["a", "b", "d"]

    def test_inner_exception_reaches_caller(self, db_manager, table):
        err = ValueError("boom")
        with db_manager.transaction():
            with pytest.raises(ValueError) as excinfo:
                with db_manager.transaction():
                    insert(db_manager, table, "a")
                    raise err
            assert excinfo.value is err
        assert names_elsewhere(db_manager.database_url, table) == []

    def test_nested_block_first_in_outer(self, db_manager, table):
        """The nested block may be the first statement the outer block runs."""
        with pytest.raises(ValueError):
            with db_manager.transaction():
                with db_manager.transaction():
                    insert(db_manager, table, "a")
                insert(db_manager, table, "b")
                raise ValueError("boom")
        assert names_elsewhere(db_manager.database_url, table) == []

        with db_manager.transaction():
            with db_manager.transaction():
                insert(db_manager, table, "a")
            insert(db_manager, table, "b")
        assert names_elsewhere(db_manager.database_url, table) == ["a", "b"]

    def test_uncaught_inner_failure_rolls_back_everything(self, db_manager, table):
        with pytest.raises(ValueError):
            with db_manager.transaction():
                insert(db_manager, table, "a")
                with db_manager.transaction():
                    insert(db_manager, table, "b")
                    raise ValueError("boom")
        assert names_elsewhere(db_manager.database_url, table) == []

    def test_join_mode_keeps_old_behaviour(self, db_manager, table):
        with db_manager.transaction():
            insert(db_manager, table, "a")
            try:
                with db_manager.transaction(nested="join"):
                    insert(db_manager, table, "b")
                    raise ValueError("boom")
            except ValueError:
                pass
        assert names_elsewhere(db_manager.database_url, table) == ["a", "b"]

    def test_connection_is_reusable_afterwards(self, db_manager, table):
        with db_manager.transaction():
            with pytest.raises(Exception):
                with db_manager.transaction():
                    db_manager.fetch_one(f"SELECT * FROM {table}_missing")
            insert(db_manager, table, "a")
        insert(db_manager, table, "b")
        assert names(db_manager, table) == ["a", "b"]
        assert db_manager._in_transaction is False


# ---------------------------------------------------------------------------
# Async PostgreSQL backend
# ---------------------------------------------------------------------------

PG_URL = os.getenv(
    "TEST_POSTGRESQL_URL",
    "postgresql://postgres:mwtest@127.0.0.1:15432/mwtest",
)


@pytest.fixture
async def backend():
    if not PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed")
    if not backend_reachable(PG_URL):
        pytest.skip(f"PostgreSQL not reachable at {PG_URL.split('@')[-1]}")
    from nexusql import AsyncPostgresBackend

    b = AsyncPostgresBackend(PG_URL)
    assert await b.connect(), "pool creation failed"
    yield b
    await b.disconnect()


@pytest.fixture
async def async_table(backend):
    name = table_name()
    await backend.execute(
        f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL UNIQUE)"
    )
    yield name
    try:
        await backend.execute(f"DROP TABLE {name}")
    except Exception:
        pass


async def ainsert(backend, table, name):
    await backend.execute(f"INSERT INTO {table} (name) VALUES (:name)", {"name": name})


async def anames(backend, table):
    rows = await backend.execute(f"SELECT name FROM {table} ORDER BY id")
    return [r["name"] for r in rows]


@pytest.mark.integration
@pytest.mark.postgresql
async def test_async_inner_failure_leaves_outer_rows(backend, async_table):
    async with backend.transaction():
        await ainsert(backend, async_table, "a")
        with pytest.raises(Exception):
            async with backend.transaction():
                await ainsert(backend, async_table, "b")
                await ainsert(backend, async_table, "a")
        await ainsert(backend, async_table, "c")
    assert await anames(backend, async_table) == ["a", "c"]


@pytest.mark.integration
@pytest.mark.postgresql
async def test_async_three_deep_nesting(backend, async_table):
    async with backend.transaction():
        await ainsert(backend, async_table, "a")
        async with backend.transaction():
            await ainsert(backend, async_table, "b")
            with pytest.raises(ValueError):
                async with backend.transaction():
                    await ainsert(backend, async_table, "c")
                    raise ValueError("boom")
            await ainsert(backend, async_table, "d")
    assert await anames(backend, async_table) == ["a", "b", "d"]


@pytest.mark.integration
@pytest.mark.postgresql
async def test_async_nested_block_first_in_outer(backend, async_table):
    with pytest.raises(ValueError):
        async with backend.transaction():
            async with backend.transaction():
                await ainsert(backend, async_table, "a")
            raise ValueError("boom")
    assert await anames(backend, async_table) == []


@pytest.mark.integration
@pytest.mark.postgresql
async def test_async_join_mode_keeps_old_behaviour(backend, async_table):
    async with backend.transaction():
        await ainsert(backend, async_table, "a")
        try:
            async with backend.transaction(nested="join"):
                await ainsert(backend, async_table, "b")
                raise ValueError("boom")
        except ValueError:
            pass
    assert await anames(backend, async_table) == ["a", "b"]
