"""
Regression tests for B-002: fetch_one/fetch_all never commit.

fetch_one and fetch_all opened a cursor, fetched, and returned without ever
committing, so a write issued through them (the standard INSERT .. RETURNING
pattern) returned the row, was readable in the same session, and was gone
after reconnect - no error, no log entry.

The fix commits after a successful fetch of a write statement, unless the
caller is inside an explicit transaction (BEGIN/COMMIT tracked by execute()),
in which case the caller owns the commit. execute() now also detects
RETURNING and fetches results for write statements.

The SQLite tests run without a server and share the exact fetch_one/fetch_all
code paths. The PostgreSQL tests are skipped when no server is available.
"""

import pytest
from nexusql import DatabaseManager


@pytest.fixture
def sqlite_manager(temp_db_path):
    """Connected DatabaseManager on a fresh SQLite file, plus its URL."""
    url = f"sqlite:///{temp_db_path}"
    db = DatabaseManager(url)
    assert db.connect(), "SQLite connect failed"
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    yield db, url
    try:
        db.disconnect()
    except Exception:
        pass


def reconnect(url):
    """Fresh connection to the same database (durability check)."""
    reader = DatabaseManager(url)
    assert reader.connect(), "reconnect failed"
    return reader


def test_fetch_one_insert_returning_survives_reconnect(sqlite_manager):
    """B-002 core scenario: INSERT .. RETURNING through fetch_one is durable."""
    db, url = sqlite_manager
    row = db.fetch_one("INSERT INTO items (name) VALUES (:name) RETURNING id", {"name": "n1"})
    assert row is not None and row["id"] == 1

    reader = reconnect(url)
    rows = reader.execute("SELECT id, name FROM items")
    reader.disconnect()
    assert rows == [{"id": 1, "name": "n1"}]


def test_fetch_all_update_returning_survives_reconnect(sqlite_manager):
    """UPDATE .. RETURNING through fetch_all commits too."""
    db, url = sqlite_manager
    db.execute("INSERT INTO items (id, name) VALUES (:id, :name)", {"id": 1, "name": "old"})
    rows = db.execute(
        "UPDATE items SET name = :new WHERE id = :id RETURNING id, name",
        {"new": "new", "id": 1},
    )
    assert rows == [{"id": 1, "name": "new"}]

    reader = reconnect(url)
    after = reader.execute("SELECT id, name FROM items")
    reader.disconnect()
    assert after == [{"id": 1, "name": "new"}]


def test_execute_insert_returning_returns_rows_and_commits(sqlite_manager):
    """execute() must not swallow INSERT .. RETURNING: fetch and commit."""
    db, url = sqlite_manager
    rows = db.execute("INSERT INTO items (name) VALUES (:name) RETURNING id", {"name": "n2"})
    assert rows == [{"id": 1}]

    reader = reconnect(url)
    after = reader.execute("SELECT id, name FROM items")
    reader.disconnect()
    assert after == [{"id": 1, "name": "n2"}]


def test_fetch_one_respects_explicit_transaction(sqlite_manager):
    """Inside a transaction the block owns the commit; fetch_* must not commit early."""
    db, url = sqlite_manager
    with db.transaction():
        db.fetch_one("INSERT INTO items (name) VALUES (:name) RETURNING id", {"name": "tx"})

    reader = reconnect(url)
    rows = reader.execute("SELECT id, name FROM items")
    reader.disconnect()
    assert rows == [{"id": 1, "name": "tx"}]


def test_fetch_one_rollback_leaves_connection_usable(sqlite_manager):
    """A failed query through fetch_one must raise (B-003) AND not poison
    the connection (B-002 rollback guarantee)."""
    db, _ = sqlite_manager
    with pytest.raises(Exception):
        db.fetch_one("THIS IS NOT SQL", None)

    ok = db.fetch_one("SELECT 1 AS one", None)
    assert ok == {"one": 1}


def test_fetch_all_rollback_leaves_connection_usable(sqlite_manager):
    """Same guarantee for fetch_all."""
    db, _ = sqlite_manager
    with pytest.raises(Exception):
        db.execute("SELECT * FROM missing_table", None)

    ok = db.fetch_one("SELECT 1 AS one", None)
    assert ok == {"one": 1}


def test_postgres_fetch_one_insert_returning_survives_reconnect(postgresql_url, skip_if_no_postgresql):
    """Postgres durability: the row must still be there after a fresh connection."""
    writer = DatabaseManager(postgresql_url)
    assert writer.connect(), "Postgres connect failed"
    try:
        writer.execute("DROP TABLE IF EXISTS b002_durability")
        writer.execute("CREATE TABLE b002_durability (id SERIAL PRIMARY KEY, name TEXT)")
        row = writer.fetch_one(
            "INSERT INTO b002_durability (name) VALUES (:name) RETURNING id",
            {"name": "n1"},
        )
        assert row is not None and "id" in row
        inserted_id = row["id"]
    finally:
        writer.disconnect()

    reader = DatabaseManager(postgresql_url)
    assert reader.connect(), "Postgres reconnect failed"
    try:
        rows = reader.execute(
            "SELECT id, name FROM b002_durability WHERE id = :id", {"id": inserted_id}
        )
    finally:
        reader.disconnect()
    assert rows == [{"id": inserted_id, "name": "n1"}]
