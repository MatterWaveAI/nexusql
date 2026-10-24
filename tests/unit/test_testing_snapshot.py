"""
nexusql.testing snapshots and truncate on SQLite.

The SQLite identity counter lives in the sqlite_sequence catalog, which only
exists once a table with AUTOINCREMENT has been created, so the file is
tested with and without it.
"""

import pytest

from nexusql import DatabaseManager
from nexusql.testing import Factory, Sequence, Snapshot, TableDiff, snapshot, truncate


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute(
        "CREATE TABLE accounts (id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
    )
    manager.execute(
        "CREATE TABLE users (id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
        "account_id INTEGER REFERENCES accounts(id))"
    )
    # The factories are shared by the module, so their sequences restart
    # with each database file.
    for factory in (accounts, users):
        for default in factory.defaults.values():
            if isinstance(default, Sequence):
                default.reset()
    yield manager
    manager.disconnect()


accounts = Factory("accounts", name=Sequence("acct{n}"))
users = Factory("users", email=Sequence("user{n}@example.com"))


def seed(db):
    a = accounts.create(db)
    users.create_batch(db, 2, account_id=a["id"])
    return a


class TestSnapshot:
    def test_holds_rows_in_memory(self, db):
        seed(db)
        snap = snapshot(db, ["accounts", "users"])
        assert isinstance(snap, Snapshot)
        assert snap.tables == ["accounts", "users"]
        assert snap.rows["accounts"] == [{"id": 1, "name": "acct1"}]
        assert [r["email"] for r in snap.rows["users"]] == [
            "user1@example.com", "user2@example.com"]
        db.execute("DELETE FROM users")
        assert len(snap.rows["users"]) == 2

    def test_single_table_name(self, db):
        seed(db)
        assert snapshot(db, "accounts").tables == ["accounts"]

    def test_restore_puts_rows_back_with_their_keys(self, db):
        seed(db)
        snap = snapshot(db, ["accounts", "users"])
        db.execute("DELETE FROM users")
        db.execute("UPDATE accounts SET name = :n", {"n": "renamed"})
        users.create(db)

        snap.restore(db)

        assert db.execute("SELECT * FROM accounts") == [{"id": 1, "name": "acct1"}]
        assert db.execute("SELECT id, email FROM users ORDER BY id") == [
            {"id": 1, "email": "user1@example.com"},
            {"id": 2, "email": "user2@example.com"},
        ]
        assert not any(snap.diff(db).values())

    def test_restore_keeps_generated_keys_moving(self, db):
        seed(db)
        snap = snapshot(db, ["accounts", "users"])
        snap.restore(db)
        assert users.create(db)["id"] == 3

    def test_restore_of_empty_snapshot_empties_tables(self, db):
        snap = snapshot(db, ["accounts", "users"])
        seed(db)
        snap.restore(db)
        assert db.execute("SELECT * FROM users") == []
        assert db.execute("SELECT * FROM accounts") == []

    def test_diff_reports_added_removed_changed_by_key(self, db):
        seed(db)
        snap = snapshot(db, ["accounts", "users"])

        db.execute("DELETE FROM users WHERE id = :id", {"id": 1})
        db.execute("UPDATE users SET email = :e WHERE id = :id",
                   {"e": "changed@example.com", "id": 2})
        new = users.create(db, account_id=1)

        diff = snap.diff(db)
        assert set(diff) == {"accounts", "users"}
        assert not diff["accounts"]
        changes: TableDiff = diff["users"]
        assert changes
        assert set(changes.added) == {new["id"]}
        assert changes.added[new["id"]]["email"] == new["email"]
        assert set(changes.removed) == {1}
        assert changes.removed[1]["email"] == "user1@example.com"
        assert set(changes.changed) == {2}
        before, after = changes.changed[2]
        assert before["email"] == "user2@example.com"
        assert after["email"] == "changed@example.com"

    def test_diff_pk_per_table(self, db):
        db.execute("CREATE TABLE tags (tag_id SERIAL PRIMARY KEY, label TEXT)")
        Factory("tags", pk="tag_id", label="a").create(db)
        snap = snapshot(db, ["tags"], pk={"tags": "tag_id"})
        Factory("tags", pk="tag_id", label="b").create(db)
        assert set(snap.diff(db)["tags"].added) == {2}

    def test_diff_names_the_table_when_the_key_is_missing(self, db):
        db.execute("CREATE TABLE tags (tag_id SERIAL PRIMARY KEY, label TEXT)")
        Factory("tags", pk="tag_id", label="a").create(db)
        snap = snapshot(db, ["tags"])
        with pytest.raises(KeyError, match="'tags' has no column 'id'"):
            snap.diff(db)


class TestTruncate:
    def test_empties_and_resets_identity(self, db):
        seed(db)
        truncate(db, ["accounts", "users"])
        assert db.execute("SELECT * FROM users") == []
        assert db.execute("SELECT * FROM accounts") == []
        assert users.create(db)["id"] == 1
        assert accounts.create(db)["id"] == 1

    def test_keeps_identity_when_asked(self, db):
        seed(db)
        truncate(db, ["accounts", "users"], reset_identity=False)
        assert db.execute("SELECT * FROM users") == []
        assert users.create(db)["id"] == 3

    def test_only_named_tables_are_touched(self, db):
        seed(db)
        truncate(db, "users")
        assert db.execute("SELECT * FROM users") == []
        assert db.fetch_one("SELECT COUNT(*) AS n FROM accounts") == {"n": 1}
        # The accounts counter was not reset either.
        assert accounts.create(db)["id"] == 2

    def test_without_sqlite_sequence_catalog(self, temp_db_path):
        """A file with no AUTOINCREMENT table has no sqlite_sequence to reset."""
        db = DatabaseManager(f"sqlite:///{temp_db_path}")
        assert db.connect()
        try:
            db.execute("CREATE TABLE plain (id INTEGER PRIMARY KEY, v TEXT)")
            db.execute("INSERT INTO plain (v) VALUES (:v)", {"v": "x"})
            assert not db.table_exists("sqlite_sequence")
            truncate(db, ["plain"])
            assert db.execute("SELECT * FROM plain") == []
        finally:
            db.disconnect()

    def test_empty_list_is_a_no_op(self, db):
        seed(db)
        truncate(db, [])
        assert db.fetch_one("SELECT COUNT(*) AS n FROM users") == {"n": 2}
