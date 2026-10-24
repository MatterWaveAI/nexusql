"""
db.cdc against live backends.

The same enable / write / read sequence must produce the same three change
rows on SQLite, PostgreSQL, MySQL and SQL Server. Each test creates its own
uniquely named table and removes it, its triggers and (on PostgreSQL) its
trigger function afterwards; the test database is shared. Backends that are
not reachable are skipped at collection (see conftest).
"""

import os
import uuid

import pytest

from nexusql import DatabaseManager
from nexusql.cdc import changes_table

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


def table_name() -> str:
    return "cdc_" + uuid.uuid4().hex[:10]


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name VARCHAR(50), qty INTEGER, note TEXT)"
    )
    yield name
    for cleanup in (lambda: db_manager.cdc.drop(name),
                    lambda: db_manager.execute(f"DROP TABLE {name}")):
        try:
            cleanup()
        except Exception:
            pass


def insert_update_delete(db, table, name="widget"):
    new_id = db.execute(
        f"INSERT INTO {table} (name, qty) VALUES (:name, :qty)",
        {"name": name, "qty": 1}, return_id=True,
    )
    db.execute(f"UPDATE {table} SET qty = :qty WHERE id = :id", {"qty": 2, "id": new_id})
    db.execute(f"DELETE FROM {table} WHERE id = :id", {"id": new_id})
    return new_id


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestChangeDataCapture:
    def test_insert_update_delete_produce_three_changes(self, db_manager, table):
        db_manager.cdc.enable(table)
        assert db_manager.cdc.is_enabled(table)
        new_id = insert_update_delete(db_manager, table)

        changes = db_manager.cdc.changes(table)
        assert [c["op"] for c in changes] == ["I", "U", "D"]
        assert [c["pk"] for c in changes] == [str(new_id)] * 3
        ids = [c["change_id"] for c in changes]
        assert ids == sorted(ids) and len(set(ids)) == 3

        inserted, updated, deleted = changes
        v1 = {"id": new_id, "name": "widget", "qty": 1, "note": None}
        v2 = {"id": new_id, "name": "widget", "qty": 2, "note": None}
        assert inserted["old_row"] is None and inserted["new_row"] == v1
        assert updated["old_row"] == v1 and updated["new_row"] == v2
        assert deleted["old_row"] == v2 and deleted["new_row"] is None
        assert all(c["changed_at"] is not None for c in changes)

    def test_enable_is_idempotent(self, db_manager, table):
        db_manager.cdc.enable(table)
        db_manager.cdc.enable(table)
        db_manager.execute(f"INSERT INTO {table} (name) VALUES (:name)", {"name": "once"})
        assert len(db_manager.cdc.changes(table)) == 1

    def test_disable_stops_capture_and_keeps_history(self, db_manager, table):
        db_manager.cdc.enable(table)
        insert_update_delete(db_manager, table)
        db_manager.cdc.disable(table)
        assert not db_manager.cdc.is_enabled(table)
        insert_update_delete(db_manager, table, name="unseen")
        assert len(db_manager.cdc.changes(table)) == 3
        assert db_manager.table_exists(changes_table(table))

    def test_purge_and_paging(self, db_manager, table):
        db_manager.cdc.enable(table)
        for i in range(5):
            db_manager.execute(f"INSERT INTO {table} (name) VALUES (:name)", {"name": str(i)})
        page = db_manager.cdc.changes(table, limit=2)
        assert [c["new_row"]["name"] for c in page] == ["0", "1"]
        rest = db_manager.cdc.changes(table, since_id=page[-1]["change_id"])
        assert [c["new_row"]["name"] for c in rest] == ["2", "3", "4"]

        db_manager.cdc.purge(table, before_id=rest[0]["change_id"])
        assert [c["new_row"]["name"] for c in db_manager.cdc.changes(table)] == ["2", "3", "4"]

    def test_drop_removes_everything(self, db_manager, table):
        db_manager.cdc.enable(table)
        insert_update_delete(db_manager, table)
        db_manager.cdc.drop(table)
        assert not db_manager.cdc.is_enabled(table)
        assert not db_manager.table_exists(changes_table(table))
        db_manager.execute(f"INSERT INTO {table} (name) VALUES (:name)", {"name": "free"})

    def test_rolled_back_write_leaves_no_change(self, db_manager, table):
        db_manager.cdc.enable(table)
        with pytest.raises(RuntimeError):
            with db_manager.transaction():
                db_manager.execute(f"INSERT INTO {table} (name) VALUES (:name)", {"name": "x"})
                raise RuntimeError("abort")
        assert db_manager.cdc.changes(table) == []

    def test_unknown_table_is_rejected(self, db_manager):
        with pytest.raises(ValueError, match="does not exist"):
            db_manager.cdc.enable(table_name())


def test_postgres_function_is_removed_with_the_triggers(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    name = table_name()
    try:
        db.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, v TEXT)")
        db.cdc.enable(name)
        fn = db.fetch_one("SELECT COUNT(*) AS n FROM pg_proc WHERE proname = :p",
                          {"p": f"{name}__cdc_fn"})
        assert fn == {"n": 1}
        db.cdc.disable(name)
        fn = db.fetch_one("SELECT COUNT(*) AS n FROM pg_proc WHERE proname = :p",
                          {"p": f"{name}__cdc_fn"})
        assert fn == {"n": 0}
    finally:
        db.cdc.drop(name)
        db.execute(f"DROP TABLE IF EXISTS {name}")
        db.disconnect()


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

    backend = AsyncPostgresBackend(ASYNC_URL, max_pool_size=4)
    if not await backend.connect():
        pytest.skip("PostgreSQL not reachable for async tests")
    yield backend
    await backend.disconnect()


async def test_async_backend_cdc(async_backend):
    name = table_name()
    try:
        await async_backend.execute(
            f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name TEXT, qty INTEGER)")
        await async_backend.cdc.enable(name)
        assert await async_backend.cdc.is_enabled(name)
        await async_backend.cdc.enable(name)

        new_id = await async_backend.execute(
            f"INSERT INTO {name} (name, qty) VALUES (:n, :q)", {"n": "a", "q": 1},
            return_id=True)
        await async_backend.execute(
            f"UPDATE {name} SET qty = :q WHERE id = :id", {"q": 2, "id": new_id})
        await async_backend.execute(f"DELETE FROM {name} WHERE id = :id", {"id": new_id})

        changes = await async_backend.cdc.changes(name)
        assert [c["op"] for c in changes] == ["I", "U", "D"]
        assert changes[1]["old_row"] == {"id": new_id, "name": "a", "qty": 1}
        assert changes[1]["new_row"] == {"id": new_id, "name": "a", "qty": 2}
        assert changes[2]["new_row"] is None

        page = await async_backend.cdc.changes(name, since_id=changes[0]["change_id"], limit=1)
        assert [c["op"] for c in page] == ["U"]

        await async_backend.cdc.purge(name, before_id=changes[2]["change_id"])
        assert [c["op"] for c in await async_backend.cdc.changes(name)] == ["D"]

        await async_backend.cdc.disable(name)
        assert not await async_backend.cdc.is_enabled(name)
        await async_backend.execute(f"INSERT INTO {name} (name) VALUES (:n)", {"n": "b"})
        assert len(await async_backend.cdc.changes(name)) == 1

        await async_backend.cdc.drop(name)
        assert not await async_backend.table_exists(changes_table(name))
    finally:
        await async_backend.cdc.drop(name)
        await async_backend.execute(f"DROP TABLE IF EXISTS {name}")
