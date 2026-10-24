"""
nexusql.testing against live backends.

Factories insert through execute(..., return_id=True), snapshots re-insert
explicit keys, and truncate issues each dialect's own statement, so all
three must be checked on every backend. Tables carry a unique suffix: the
PostgreSQL database is shared with other suites running at the same time.
Backends that are not reachable skip at collection (see conftest).
"""

import uuid

import pytest

from nexusql.testing import Factory, Sequence, SubFactory, snapshot, truncate

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


@pytest.fixture
def tables(db_manager):
    """Parent/child pair in insert order, dropped child-first afterwards."""
    suffix = uuid.uuid4().hex[:10]
    parent, child = f"fxp_{suffix}", f"fxc_{suffix}"
    db_manager.execute(
        f"CREATE TABLE {parent} (id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL)"
    )
    db_manager.execute(
        f"CREATE TABLE {child} (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL, "
        f"parent_id INTEGER NOT NULL REFERENCES {parent}(id))"
    )
    yield parent, child
    for name in (child, parent):
        try:
            db_manager.execute(f"DROP TABLE {name}")
        except Exception:
            pass


def factories(tables):
    parent, child = tables
    parents = Factory(parent, name=Sequence("parent{n}"))
    children = Factory(child, email=Sequence("user{n}@example.com"),
                       parent_id=SubFactory(parents))
    return parents, children


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestFactories:
    def test_create_returns_generated_key(self, db_manager, tables):
        parents, _ = factories(tables)
        first = parents.create(db_manager)
        second = parents.create(db_manager, name="named")
        assert isinstance(first["id"], int) and second["id"] > first["id"]
        row = db_manager.fetch_one(
            f"SELECT name FROM {tables[0]} WHERE id = :id", {"id": second["id"]})
        assert row == {"name": "named"}

    def test_subfactory_creates_the_parent(self, db_manager, tables):
        _, children = factories(tables)
        rows = children.create_batch(db_manager, 2)
        assert [r["email"] for r in rows] == ["user1@example.com", "user2@example.com"]
        parents = db_manager.execute(f"SELECT id FROM {tables[0]} ORDER BY id")
        assert [p["id"] for p in parents] == [r["parent_id"] for r in rows]


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestSnapshot:
    def test_restore_and_diff(self, db_manager, tables):
        parent, child = tables
        parents, children = factories(tables)
        kept = children.create_batch(db_manager, 2)
        snap = snapshot(db_manager, [parent, child])

        db_manager.execute(f"DELETE FROM {child} WHERE id = :id", {"id": kept[0]["id"]})
        db_manager.execute(f"UPDATE {child} SET email = :e WHERE id = :id",
                           {"e": "edited@example.com", "id": kept[1]["id"]})
        added = children.create(db_manager, parent_id=kept[1]["parent_id"])

        diff = snap.diff(db_manager)
        assert not diff[parent]
        assert set(diff[child].added) == {added["id"]}
        assert set(diff[child].removed) == {kept[0]["id"]}
        assert set(diff[child].changed) == {kept[1]["id"]}

        snap.restore(db_manager)
        assert not any(snap.diff(db_manager).values())
        rows = db_manager.execute(f"SELECT id, email FROM {child} ORDER BY id")
        assert rows == [{"id": r["id"], "email": r["email"]} for r in kept]

    def test_restored_tables_keep_generating_fresh_keys(self, db_manager, tables):
        parent, child = tables
        parents, children = factories(tables)
        children.create_batch(db_manager, 3)
        snap = snapshot(db_manager, [parent, child])
        snap.restore(db_manager)
        # Had the key counters stayed where the truncate left them, these
        # inserts would collide with the restored rows.
        assert parents.create(db_manager)["id"] == 4
        assert children.create(db_manager)["id"] == 4


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestTruncate:
    def test_resets_identity(self, db_manager, tables):
        parent, child = tables
        parents, children = factories(tables)
        children.create_batch(db_manager, 2)
        truncate(db_manager, [parent, child])
        assert db_manager.execute(f"SELECT * FROM {child}") == []
        assert db_manager.execute(f"SELECT * FROM {parent}") == []
        assert parents.create(db_manager)["id"] == 1
        assert children.create(db_manager)["id"] == 1

    def test_keeps_identity_when_asked(self, db_manager, tables):
        parent, child = tables
        parents, children = factories(tables)
        last = children.create_batch(db_manager, 2)[-1]
        truncate(db_manager, [parent, child], reset_identity=False)
        assert db_manager.execute(f"SELECT * FROM {child}") == []
        assert children.create(db_manager)["id"] > last["id"]

    def test_referenced_parent_alone(self, db_manager, tables):
        """A parent still referenced by rows elsewhere is emptied by the dialect's fallback."""
        parent, child = tables
        parents, children = factories(tables)
        children.create(db_manager)
        db_manager.execute(f"DELETE FROM {child}")
        truncate(db_manager, [parent])
        assert db_manager.execute(f"SELECT * FROM {parent}") == []
        assert parents.create(db_manager)["id"] == 1
