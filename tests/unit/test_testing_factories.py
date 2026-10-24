"""
nexusql.testing factories on the SQLite driver path.
"""

import itertools
import sys

import pytest

import nexusql
from nexusql import DatabaseManager
from nexusql.testing import Factory, Sequence, SubFactory


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute(
        "CREATE TABLE accounts (id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
    )
    manager.execute(
        "CREATE TABLE users (id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
        "name TEXT, active BOOLEAN DEFAULT TRUE, "
        "account_id INTEGER REFERENCES accounts(id))"
    )
    yield manager
    manager.disconnect()


class TestSequence:
    def test_formats_counter_from_start(self):
        seq = Sequence("user{n}@example.com")
        assert seq.next() == "user1@example.com"
        assert seq.next() == "user2@example.com"

    def test_callable_template_and_start(self):
        seq = Sequence(lambda n: n * 10, start=5)
        assert [seq.next(), seq.next()] == [50, 60]

    def test_reset(self):
        seq = Sequence(start=3)
        seq.next()
        seq.reset()
        assert seq.next() == "3"


class TestBuild:
    def test_values_callables_and_sequences(self):
        counter = itertools.count(100)
        users = Factory(
            "users",
            email=Sequence("user{n}@example.com"),
            name="Test User",
            active=True,
            score=lambda: next(counter),
        )
        first = users.build()
        second = users.build(name="Alice")
        assert first == {"email": "user1@example.com", "name": "Test User",
                         "active": True, "score": 100}
        assert second == {"email": "user2@example.com", "name": "Alice",
                          "active": True, "score": 101}

    def test_override_does_not_advance_sequence(self):
        users = Factory("users", email=Sequence("user{n}@example.com"))
        users.build(email="fixed@example.com")
        assert users.build()["email"] == "user1@example.com"

    def test_override_is_resolved_too(self):
        users = Factory("users", name="x")
        assert users.build(name=lambda: "called")["name"] == "called"

    def test_subfactory_has_no_key_without_a_database(self):
        accounts = Factory("accounts", name="acme")
        users = Factory("users", email="a@b.c", account_id=SubFactory(accounts))
        assert users.build()["account_id"] is None
        assert users.build(account_id=7)["account_id"] == 7

    def test_build_does_not_touch_the_database(self, db):
        Factory("users", email="a@b.c").build()
        assert db.execute("SELECT * FROM users") == []


class TestCreate:
    def test_returns_row_with_generated_id(self, db):
        users = Factory("users", email=Sequence("user{n}@example.com"), name="n")
        row = users.create(db, name="Alice")
        assert row == {"id": 1, "email": "user1@example.com", "name": "Alice"}
        stored = db.fetch_one("SELECT id, email, name FROM users WHERE id = :id",
                              {"id": row["id"]})
        assert stored == row

    def test_configurable_pk_column(self, db):
        db.execute("CREATE TABLE tags (tag_id SERIAL PRIMARY KEY, label TEXT)")
        tags = Factory("tags", pk="tag_id", label="t")
        assert tags.create(db)["tag_id"] == 1
        assert tags.create(db)["tag_id"] == 2

    def test_explicit_key_is_inserted_as_given(self, db):
        users = Factory("users", email="a@b.c")
        assert users.create(db, id=42)["id"] == 42
        assert db.fetch_one("SELECT id FROM users") == {"id": 42}

    def test_none_key_falls_back_to_generated(self, db):
        users = Factory("users", email="a@b.c", id=None)
        assert users.create(db)["id"] == 1

    def test_create_batch_advances_sequences(self, db):
        users = Factory("users", email=Sequence("user{n}@example.com"))
        rows = users.create_batch(db, 3, name="batch")
        assert [r["id"] for r in rows] == [1, 2, 3]
        assert [r["email"] for r in rows] == [
            "user1@example.com", "user2@example.com", "user3@example.com"]
        assert {r["name"] for r in rows} == {"batch"}
        assert db.fetch_one("SELECT COUNT(*) AS n FROM users") == {"n": 3}

    def test_subfactory_creates_parent(self, db):
        accounts = Factory("accounts", name=Sequence("acct{n}"))
        users = Factory("users", email=Sequence("u{n}@x.y"),
                        account_id=SubFactory(accounts, name="owner"))
        row = users.create(db)
        parent = db.fetch_one("SELECT id, name FROM accounts")
        assert parent == {"id": row["account_id"], "name": "owner"}
        # A second child gets its own parent, as SubFactory promises.
        users.create(db)
        assert db.fetch_one("SELECT COUNT(*) AS n FROM accounts") == {"n": 2}

    def test_default_values_insert_when_nothing_is_declared(self, db):
        db.execute("CREATE TABLE blanks (id SERIAL PRIMARY KEY, v INTEGER DEFAULT 9)")
        row = Factory("blanks").create(db)
        assert row == {"id": 1}
        assert db.fetch_one("SELECT v FROM blanks") == {"v": 9}

    def test_inside_transaction_is_rolled_back_with_it(self, db):
        users = Factory("users", email="a@b.c")
        with pytest.raises(RuntimeError):
            with db.transaction():
                users.create(db)
                raise RuntimeError("abort")
        assert db.execute("SELECT * FROM users") == []


def test_top_level_exports_are_lazy():
    assert nexusql.Factory is Factory
    assert nexusql.Sequence is Sequence
    assert nexusql.snapshot is not None and nexusql.truncate is not None
    with pytest.raises(AttributeError):
        nexusql.NoSuchThing


def test_importing_the_package_does_not_import_pytest():
    """Library users without pytest must still be able to import nexusql."""
    import subprocess
    code = ("import sys, nexusql, nexusql.testing; "
            "print('pytest' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "False"
