"""
db.fts against live backends.

The same documents, index and queries run on SQLite, PostgreSQL, MySQL and
SQL Server; the ranking engines differ, so the assertions are about order
and membership rather than scores. Backends that are not reachable are
skipped at collection (see conftest). The PostgreSQL database is shared, so
every table and index is uniquely named and removed afterwards.
"""

import os
import time
import uuid

import pytest

from nexusql import DatabaseManager
from nexusql.interfaces import DatabaseType

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]

MALICIOUS = "'; DROP TABLE docs; -- \" OR 1=1"

DOCS = [
    ("PostgreSQL full text search", "PostgreSQL offers tsvector based search with ranking"),
    ("Cooking pasta", "boil water, add pasta, wait"),
    ("Search engines", "search search search everywhere"),
    ("100% natural", "organic"),
    ("1000 ways", "to cook"),
]


def table_name() -> str:
    return "fts_" + uuid.uuid4().hex[:10]


def search(db, table, columns, query, **kw):
    """
    SQL Server populates a full-text index in the background, so a search
    right after a write can legitimately come back empty for a moment. Poll
    briefly there; the other engines index synchronously.
    """
    hits = db.fts.search(table, columns, query, **kw)
    if db.config.database_type == DatabaseType.MSSQL:
        deadline = time.monotonic() + 15
        while not hits and time.monotonic() < deadline:
            time.sleep(0.5)
            hits = db.fts.search(table, columns, query, **kw)
    return hits


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, title VARCHAR(200), body TEXT)"
    )
    for title, body in DOCS:
        db_manager.execute(
            f"INSERT INTO {name} (title, body) VALUES (:title, :body)",
            {"title": title, "body": body},
        )
    db_manager.fts.create_index(name, ["title", "body"])
    yield name
    for cleanup in (
        lambda: db_manager.fts.drop_index(name),
        lambda: db_manager.execute(f"DROP TABLE {name}"),
    ):
        try:
            cleanup()
        except Exception:
            pass


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestFullTextSearch:
    def test_ranking_order(self, db_manager, table):
        hits = search(db_manager, table, ["title", "body"], "search")
        assert [h["id"] for h in hits] == [3, 1]
        assert hits[0]["rank"] > hits[1]["rank"]
        assert hits[0]["title"] == "Search engines"

    def test_only_matching_rows(self, db_manager, table):
        hits = search(db_manager, table, ["title", "body"], "pasta")
        assert [h["id"] for h in hits] == [2]
        assert db_manager.fts.search(table, ["title", "body"], "zebra") == []
        assert db_manager.fts.search(table, ["title", "body"], "") == []

    def test_limit_and_offset(self, db_manager, table):
        first = search(db_manager, table, ["title", "body"], "search", limit=1)
        second = search(db_manager, table, ["title", "body"], "search", limit=1, offset=1)
        assert [h["id"] for h in first] == [3]
        assert [h["id"] for h in second] == [1]

    def test_updates_and_deletes_are_reflected(self, db_manager, table):
        db_manager.execute(
            f"UPDATE {table} SET body = :b WHERE id = :id", {"b": "search for pasta", "id": 2})
        db_manager.execute(f"DELETE FROM {table} WHERE id = :id", {"id": 3})
        db_manager.execute(
            f"INSERT INTO {table} (title, body) VALUES (:t, :b)",
            {"t": "Fresh", "b": "a new search document"},
        )
        deadline = time.monotonic() + 15
        while True:
            ids = {h["id"] for h in search(db_manager, table, ["title", "body"], "search")}
            if ids == {1, 2, 6} or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        assert ids == {1, 2, 6}

    def test_malicious_query_is_just_text(self, db_manager, table):
        assert db_manager.fts.search(table, ["title", "body"], MALICIOUS) == []
        assert db_manager.fts.suggest(table, "title", MALICIOUS) == []
        assert db_manager.table_exists(table)
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 5

    def test_suggest_prefix(self, db_manager, table):
        assert db_manager.fts.suggest(table, "title", "Postg") == ["PostgreSQL full text search"]
        assert db_manager.fts.suggest(table, "title", "co") == ["Cooking pasta"]
        assert db_manager.fts.suggest(table, "title", "1") == ["100% natural", "1000 ways"]
        assert db_manager.fts.suggest(table, "title", "1", limit=1) == ["100% natural"]
        assert db_manager.fts.suggest(table, "title", "zzz") == []

    def test_suggest_escapes_like_wildcards(self, db_manager, table):
        assert db_manager.fts.suggest(table, "title", "100%") == ["100% natural"]
        assert db_manager.fts.suggest(table, "title", "100_") == []

    def test_drop_then_recreate(self, db_manager, table):
        db_manager.fts.drop_index(table)
        db_manager.fts.create_index(table, ["title", "body"])
        assert [h["id"] for h in search(db_manager, table, ["title", "body"], "pasta")] == [2]

    def test_bad_identifiers_never_reach_the_database(self, db_manager, table):
        with pytest.raises(ValueError):
            db_manager.fts.search(f"{table}; DROP TABLE {table}", ["title"], "x")
        with pytest.raises(ValueError):
            db_manager.fts.create_index(table, ['title" --'])
        assert db_manager.table_exists(table)


# ---------------------------------------------------------------------------
# PostgreSQL specifics
# ---------------------------------------------------------------------------

def test_postgres_index_is_created_and_dropped(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    name = table_name()
    try:
        db.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, title TEXT)")
        db.fts.create_index(name, ["title"], name=f"{name}_ix")
        indexes = db.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i",
            {"t": name, "i": f"{name}_ix"},
        )
        assert len(indexes) == 1 and "USING gin" in indexes[0]["indexdef"]
        db.fts.drop_index(name, name=f"{name}_ix")
        assert db.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = :i", {"i": f"{name}_ix"}) == []
    finally:
        db.execute(f"DROP TABLE IF EXISTS {name}")
        db.disconnect()


def test_postgres_trigram_suggestions(postgresql_url, skip_if_no_postgresql):
    """
    With pg_trgm, a misspelling finds its target. The extension is installed
    through install=True only if it was absent, and removed again afterwards
    so the shared database is left as it was found.
    """
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    name = table_name()
    had_trgm = db.fetch_one(
        "SELECT 1 AS present FROM pg_extension WHERE extname = 'pg_trgm'") is not None
    try:
        db.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, title TEXT)")
        for title, _ in DOCS:
            db.execute(f"INSERT INTO {name} (title) VALUES (:t)", {"t": title})

        without = db.fts.suggest(name, "title", "Cookin pasta")
        if not had_trgm:
            assert without == []  # plain ILIKE prefix: a typo finds nothing

        fuzzy = db.fts.suggest(name, "title", "Cookin pasta", install=True)
        assert fuzzy == ["Cooking pasta"]
        # Prefix matches still come first (ordered among themselves by
        # similarity), then trigram neighbours.
        ordered = db.fts.suggest(name, "title", "100")
        assert set(ordered[:2]) == {"100% natural", "1000 ways"}
    finally:
        db.execute(f"DROP TABLE IF EXISTS {name}")
        if not had_trgm:
            db.execute("DROP EXTENSION IF EXISTS pg_trgm")
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
    try:
        if not await backend.connect():
            pytest.skip("PostgreSQL not reachable for async tests")
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable for async tests: {exc}")
    yield backend
    await backend.disconnect()


async def test_async_backend_fts(async_backend):
    name = table_name()
    try:
        await async_backend.execute(
            f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, title TEXT, body TEXT)")
        for title, body in DOCS:
            await async_backend.execute(
                f"INSERT INTO {name} (title, body) VALUES (:t, :b)", {"t": title, "b": body})
        await async_backend.fts.create_index(name, ["title", "body"])

        hits = await async_backend.fts.search(name, ["title", "body"], "search")
        assert [h["id"] for h in hits] == [3, 1]
        assert hits[0]["rank"] > hits[1]["rank"]
        assert await async_backend.fts.search(name, ["title", "body"], MALICIOUS) == []

        assert await async_backend.fts.suggest(name, "title", "co") == ["Cooking pasta"]
        assert await async_backend.fts.suggest(name, "title", "100%") == ["100% natural"]

        await async_backend.fts.drop_index(name)
        assert await async_backend.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = :i", {"i": f"{name}_fts"}) == []
    finally:
        await async_backend.execute(f"DROP TABLE IF EXISTS {name}")
