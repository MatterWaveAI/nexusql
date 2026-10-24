"""
db.fts: portable full-text search.

The SQLite tests run the real FTS5 path without a server: index build,
ranking, trigger maintenance, suggestions. Every dialect's generated SQL is
then checked against a driver-shaped fake connection, which is also where
the security property is pinned down: the search string must reach the
driver only as a bound parameter, never inside the SQL text. The live
PostgreSQL, MySQL and SQL Server paths are covered by
tests/integration/test_fts.py when those backends are reachable.
"""

from contextlib import asynccontextmanager

import pytest

from nexusql import AsyncFullTextSearch, ConnectionConfig, DatabaseManager, FullTextSearch
from nexusql.fts import (
    fts5_match_query,
    like_prefix,
    quote_identifier,
    validate_identifier,
)
from nexusql.interfaces import DatabaseType

MALICIOUS = "'; DROP TABLE docs; -- \" OR 1=1"

DOCS = [
    ("PostgreSQL full text search", "PostgreSQL offers tsvector based search with ranking"),
    ("Cooking pasta", "boil water, add pasta, wait"),
    ("Search engines", "search search search everywhere"),
    ("100% natural", "organic"),
    ("1000 ways", "to cook"),
]


# ---------------------------------------------------------------------------
# SQLite: the real FTS5 path
# ---------------------------------------------------------------------------

@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute(
        "CREATE TABLE docs (id SERIAL PRIMARY KEY, title VARCHAR(200), body TEXT)"
    )
    for title, body in DOCS:
        manager.execute(
            "INSERT INTO docs (title, body) VALUES (:title, :body)",
            {"title": title, "body": body},
        )
    manager.fts.create_index("docs", ["title", "body"])
    yield manager
    try:
        manager.disconnect()
    except Exception:
        pass


class TestSqlite:
    def test_fts_property_is_lazy_and_cached(self, db):
        assert isinstance(db.fts, FullTextSearch)
        assert db.fts is db.fts

    def test_index_objects_exist(self, db):
        names = {
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE :p", {"p": "docs_fts%"})
        }
        assert {"docs_fts", "docs_fts_ai", "docs_fts_ad", "docs_fts_au"} <= names

    def test_ranking_order(self, db):
        hits = db.fts.search("docs", ["title", "body"], "search")
        assert [h["id"] for h in hits] == [3, 1]
        assert hits[0]["rank"] > hits[1]["rank"]
        # The base table's columns ride along with the rank.
        assert hits[0]["title"] == "Search engines"

    def test_words_are_anded(self, db):
        assert [h["id"] for h in db.fts.search("docs", ["title", "body"], "pasta water")] == [2]
        assert db.fts.search("docs", ["title", "body"], "pasta tsvector") == []

    def test_column_filter(self, db):
        # "pasta" appears only in doc 2; "ranking" only in doc 1's body.
        assert [h["id"] for h in db.fts.search("docs", ["title"], "ranking")] == []
        assert [h["id"] for h in db.fts.search("docs", ["body"], "ranking")] == [1]

    def test_limit_and_offset(self, db):
        first = db.fts.search("docs", ["title", "body"], "search", limit=1)
        second = db.fts.search("docs", ["title", "body"], "search", limit=1, offset=1)
        assert [h["id"] for h in first] == [3]
        assert [h["id"] for h in second] == [1]

    def test_updates_and_deletes_are_reflected(self, db):
        db.execute("UPDATE docs SET body = :b WHERE id = :id", {"b": "search for pasta", "id": 2})
        db.execute("DELETE FROM docs WHERE id = :id", {"id": 3})
        db.execute(
            "INSERT INTO docs (title, body) VALUES (:t, :b)",
            {"t": "Fresh", "b": "a new search document"},
        )
        ids = {h["id"] for h in db.fts.search("docs", ["title", "body"], "search")}
        assert ids == {1, 2, 6}

    def test_empty_query_matches_nothing(self, db):
        assert db.fts.search("docs", ["title", "body"], "") == []
        assert db.fts.search("docs", ["title", "body"], "   ") == []

    def test_malicious_query_is_just_text(self, db):
        assert db.fts.search("docs", ["title", "body"], MALICIOUS) == []
        assert db.fts.search("docs", ["title", "body"], 'AND OR NOT ("unbalanced') == []
        assert db.table_exists("docs")
        assert db.fetch_one("SELECT COUNT(*) AS n FROM docs") == {"n": 5}

    def test_suggest_prefix(self, db):
        assert db.fts.suggest("docs", "title", "p") == ["PostgreSQL full text search"]
        assert db.fts.suggest("docs", "title", "co") == ["Cooking pasta"]
        assert db.fts.suggest("docs", "title", "zzz") == []
        assert db.fts.suggest("docs", "title", "  ") == []

    def test_suggest_escapes_like_wildcards(self, db):
        assert db.fts.suggest("docs", "title", "100%") == ["100% natural"]
        assert db.fts.suggest("docs", "title", "100_") == []
        assert db.fts.suggest("docs", "title", MALICIOUS) == []

    def test_suggest_limit(self, db):
        assert db.fts.suggest("docs", "title", "", limit=1) == []
        assert len(db.fts.suggest("docs", "title", "1", limit=1)) == 1

    def test_custom_index_name(self, db):
        db.fts.drop_index("docs")
        db.fts.create_index("docs", ["title"], name="title_only")
        hits = db.fts.search("docs", ["title"], "pasta", name="title_only")
        assert [h["id"] for h in hits] == [2]
        db.fts.drop_index("docs", name="title_only")
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE :p", {"p": "title_only%"}) == []

    def test_drop_index_removes_triggers_and_table(self, db):
        db.fts.drop_index("docs")
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE :p", {"p": "docs_fts%"}) == []
        # Writes to the base table must work without the triggers.
        db.execute("INSERT INTO docs (title, body) VALUES (:t, :b)", {"t": "x", "b": "y"})
        db.fts.drop_index("docs")  # idempotent

    def test_rowid_table_without_integer_key(self, db):
        db.execute("CREATE TABLE notes (code TEXT PRIMARY KEY, text_body TEXT)")
        db.execute("INSERT INTO notes VALUES (:c, :b)", {"c": "a", "b": "alpha beta"})
        db.fts.create_index("notes", ["text_body"])
        db.execute("INSERT INTO notes VALUES (:c, :b)", {"c": "b", "b": "beta gamma"})
        hits = db.fts.search("notes", ["text_body"], "beta")
        assert sorted(h["code"] for h in hits) == ["a", "b"]

    def test_missing_table_is_reported(self, db):
        with pytest.raises(ValueError, match="does not exist"):
            db.fts.create_index("nothing_here", ["x"])

    def test_bad_identifiers_rejected(self, db):
        for bad in ["docs; DROP TABLE docs", 'docs"', "1docs", "a.b", "", "x" * 64]:
            with pytest.raises(ValueError):
                db.fts.create_index(bad, ["title"])
            with pytest.raises(ValueError):
                db.fts.search("docs", [bad], "x")
            with pytest.raises(ValueError):
                db.fts.search("docs", ["title"], "x", name=bad)
            with pytest.raises(ValueError):
                db.fts.suggest("docs", bad, "x")
        with pytest.raises(ValueError):
            db.fts.create_index("docs", [])

    def test_bad_limits_rejected(self, db):
        for limit, offset in [(0, 0), (-1, 0), (True, 0), ("5", 0), (5, -1), (5, "0")]:
            with pytest.raises(ValueError):
                db.fts.search("docs", ["title"], "x", limit=limit, offset=offset)
        with pytest.raises(TypeError):
            db.fts.search("docs", ["title"], None)
        with pytest.raises(TypeError):
            db.fts.suggest("docs", "title", 5)


# ---------------------------------------------------------------------------
# Driver-shaped fakes: generated SQL per dialect
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params, self._conn.autocommit))
        rows = self._conn.respond(sql)
        if rows is None:
            self.description = None
            self._rows = []
            return
        columns = list(rows[0].keys()) if rows else ["placeholder"]
        self.description = [(c, None) for c in columns]
        if self._conn.tuple_rows:
            self._rows = [tuple(r[c] for c in columns) for r in rows]
        else:
            self._rows = list(rows)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        pass


class FakeConnection:
    """
    Answers SELECTs by substring match on the SQL: ``responses`` maps a
    needle to the dict rows to return. Anything else is a row-less
    statement. ``tuple_rows`` mimics pyodbc, whose rows index by position.
    """

    def __init__(self, responses=None, tuple_rows=False):
        self.responses = responses or {}
        self.tuple_rows = tuple_rows
        self.executed = []
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0

    def respond(self, sql):
        for needle, rows in self.responses.items():
            if needle in sql:
                return rows
        return [] if sql.lstrip().upper().startswith("SELECT") else None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def sql(self):
        return [s for s, _, _ in self.executed]


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self, timeout=None):
        return self._conn

    def release(self, conn, *, discard=False):
        pass

    def close(self):
        pass

    @property
    def size(self):
        return 1


def fake_manager(db_type, responses=None):
    urls = {
        "mysql": "mysql://u:p@localhost/db",
        "mssql": "mssql://u:p@localhost/db",
        "postgresql": "postgresql://u:p@localhost/db",
        "sqlite": "sqlite:///fake.db",
    }
    manager = DatabaseManager(ConnectionConfig.from_url(urls[db_type]))
    conn = FakeConnection(responses, tuple_rows=(db_type == "mssql"))
    manager._pool = FakePool(conn)
    return manager, conn


# The ids avoid the backend names: conftest skips any test whose id names a
# backend without a TEST_*_URL, and these need no server at all.
@pytest.mark.parametrize(
    "db_type", ["sqlite", "postgresql", "mysql", "mssql"], ids=["lite", "pg", "my", "ms"])
def test_query_text_is_only_ever_a_parameter(db_type):
    responses = {
        "pragma_table_info": [{"name": "id", "type": "INTEGER", "pk": 1}],
        "sys.indexes": [{"index_name": "PK_docs", "column_name": "id"}],
    }
    manager, conn = fake_manager(db_type, responses)
    manager.fts.search("docs", ["title", "body"], MALICIOUS)
    manager.fts.suggest("docs", "title", MALICIOUS)
    for sql, params, _ in conn.executed:
        assert "DROP" not in sql
        assert MALICIOUS not in sql
    bound = [params for _, params, _ in conn.executed if params]
    search_params, suggest_params = bound[-2], bound[-1]
    # SQLite tokenises the text into quoted FTS5 terms, so look for a word.
    values = list(search_params.values()) if isinstance(search_params, dict) else list(search_params)
    assert any("DROP" in str(v) for v in values)
    values = list(suggest_params.values()) if isinstance(suggest_params, dict) else list(suggest_params)
    assert any(MALICIOUS in str(v) for v in values)


class TestMysqlSql:
    def test_create_and_drop(self):
        manager, conn = fake_manager("mysql")
        manager.fts.create_index("docs", ["title", "body"])
        manager.fts.drop_index("docs")
        assert conn.sql() == [
            "CREATE FULLTEXT INDEX `docs_fts` ON `docs` (`title`, `body`)",
            "DROP INDEX `docs_fts` ON `docs`",
        ]
        assert conn.commits == 2

    def test_search(self):
        manager, conn = fake_manager("mysql")
        manager.fts.search("docs", ["title", "body"], "pasta water", limit=5, offset=10)
        sql, params, _ = conn.executed[-1]
        assert sql == (
            "SELECT t.*, MATCH (t.`title`, t.`body`) AGAINST (%s IN NATURAL LANGUAGE MODE) AS `rank` "
            "FROM `docs` t WHERE MATCH (t.`title`, t.`body`) AGAINST (%s IN NATURAL LANGUAGE MODE) "
            "ORDER BY `rank` DESC LIMIT %s OFFSET %s"
        )
        assert params == ("pasta water", "pasta water", 5, 10)

    def test_suggest(self):
        manager, conn = fake_manager("mysql", {"suggestion": [{"suggestion": "Pasta"}]})
        assert manager.fts.suggest("docs", "title", "pa_", limit=3) == ["Pasta"]
        sql, params, _ = conn.executed[-1]
        assert sql == (
            "SELECT DISTINCT `title` AS suggestion FROM `docs` WHERE `title` LIKE %s ESCAPE '!' "
            "ORDER BY suggestion LIMIT %s"
        )
        assert params == ("pa!_%", 3)


class TestMssqlSql:
    RESPONSES = {
        "IsFullTextInstalled": [{"installed": 1}],
        "sys.indexes": [{"index_name": "PK_docs", "column_name": "id"}],
        "sys.fulltext_catalogs": [],
    }

    def test_create_runs_catalog_and_index_with_autocommit(self):
        manager, conn = fake_manager("mssql", self.RESPONSES)
        manager.fts.create_index("docs", ["title", "body"])
        ddl = [(s, auto) for s, _, auto in conn.executed if s.startswith("CREATE")]
        assert ddl == [
            ("CREATE FULLTEXT CATALOG [docs_fts]", True),
            ("CREATE FULLTEXT INDEX ON [docs] ([title], [body]) KEY INDEX [PK_docs] "
             "ON [docs_fts] WITH CHANGE_TRACKING AUTO", True),
        ]
        assert conn.autocommit is False
        # The key index lookup was parameterised on the table name.
        key_sql, key_params, _ = next(e for e in conn.executed if "sys.indexes" in e[0])
        assert "OBJECT_ID(?)" in key_sql and key_params == ("docs",)

    def test_existing_catalog_is_reused(self):
        responses = dict(self.RESPONSES, **{"sys.fulltext_catalogs": [{"present": 1}]})
        manager, conn = fake_manager("mssql", responses)
        manager.fts.create_index("docs", ["title"])
        assert not any("CREATE FULLTEXT CATALOG" in s for s in conn.sql())

    def test_component_missing_is_a_clear_error(self):
        responses = dict(self.RESPONSES, IsFullTextInstalled=[{"installed": 0}])
        manager, conn = fake_manager("mssql", responses)
        with pytest.raises(RuntimeError, match="IsFullTextInstalled"):
            manager.fts.create_index("docs", ["title"])
        assert not any(s.startswith("CREATE") for s in conn.sql())

    def test_table_without_key_index_is_rejected(self):
        responses = dict(self.RESPONSES, **{"sys.indexes": []})
        manager, _ = fake_manager("mssql", responses)
        with pytest.raises(ValueError, match="unique, non-nullable key index"):
            manager.fts.create_index("docs", ["title"])

    def test_ddl_inside_transaction_is_refused(self):
        manager, _ = fake_manager("mssql", self.RESPONSES)
        with manager.transaction():
            with pytest.raises(RuntimeError, match="inside a transaction"):
                manager.fts.create_index("docs", ["title"])

    def test_drop(self):
        responses = dict(self.RESPONSES, **{"sys.fulltext_catalogs": [{"present": 1}]})
        manager, conn = fake_manager("mssql", responses)
        manager.fts.drop_index("docs")
        ddl = [(s, auto) for s, _, auto in conn.executed if s.startswith("DROP")]
        assert ddl == [
            ("DROP FULLTEXT INDEX ON [docs]", True),
            ("DROP FULLTEXT CATALOG [docs_fts]", True),
        ]

    def test_search(self):
        manager, conn = fake_manager("mssql", self.RESPONSES)
        manager.fts.search("docs", ["title", "body"], "pasta", limit=5, offset=10)
        sql, params, _ = conn.executed[-1]
        assert sql == (
            "SELECT t.*, ft.[RANK] AS [rank] "
            "FROM FREETEXTTABLE([docs], ([title], [body]), ?) AS ft "
            "JOIN [docs] AS t ON t.[id] = ft.[KEY] "
            "ORDER BY ft.[RANK] DESC "
            "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        )
        assert params == ("pasta", 10, 5)
        # The key is looked up once per table, not once per search.
        manager.fts.search("docs", ["title"], "again")
        assert sum("sys.indexes" in s for s in conn.sql()) == 1

    def test_suggest_escapes_brackets(self):
        manager, conn = fake_manager("mssql", {"suggestion": [{"suggestion": "[x] 100%"}]})
        assert manager.fts.suggest("docs", "title", "[x] 100%") == ["[x] 100%"]
        sql, params, _ = conn.executed[-1]
        assert sql == (
            "SELECT DISTINCT TOP (?) [title] AS suggestion FROM [docs] "
            "WHERE [title] LIKE ? ESCAPE '!' ORDER BY suggestion"
        )
        assert params == (10, "![x] 100!%%")


class TestPostgresSql:
    def test_create_and_drop(self):
        manager, conn = fake_manager("postgresql")
        manager.fts.create_index("docs", ["title", "body"])
        manager.fts.drop_index("docs")
        assert conn.sql() == [
            'CREATE INDEX "docs_fts" ON "docs" USING GIN (to_tsvector(\'english\', '
            'coalesce("title"::text, \'\') || \' \' || coalesce("body"::text, \'\')))',
            'DROP INDEX IF EXISTS "docs_fts"',
        ]

    def test_search_uses_the_index_expression(self):
        manager, conn = fake_manager("postgresql")
        manager.fts.config = "simple"
        manager.fts.search("docs", ["title"], "pasta", limit=5, offset=10)
        sql, params, _ = conn.executed[-1]
        expr = "to_tsvector('simple', coalesce(\"title\"::text, ''))"
        assert sql == (
            f'SELECT t.*, ts_rank({expr}, websearch_to_tsquery(\'simple\', %(query)s)) AS "rank" '
            f'FROM "docs" t WHERE {expr} @@ websearch_to_tsquery(\'simple\', %(query)s) '
            'ORDER BY "rank" DESC LIMIT %(limit)s OFFSET %(offset)s'
        )
        assert params == {"query": "pasta", "limit": 5, "offset": 10}

    def test_bad_config_rejected(self):
        manager, _ = fake_manager("postgresql")
        manager.fts.config = "english'; DROP"
        with pytest.raises(ValueError, match="text search configuration"):
            manager.fts.search("docs", ["title"], "x")

    def test_suggest_without_trgm_uses_ilike(self):
        manager, conn = fake_manager("postgresql", {"pg_extension": []})
        manager.fts.suggest("docs", "title", "po")
        sql, params, _ = conn.executed[-1]
        assert sql == (
            'SELECT DISTINCT "title"::text AS suggestion FROM "docs" '
            'WHERE "title"::text ILIKE %(prefix)s ESCAPE \'!\' ORDER BY suggestion LIMIT %(limit)s'
        )
        assert params == {"prefix": "po%", "limit": 10}
        assert not any("CREATE EXTENSION" in s for s in conn.sql())

    def test_suggest_with_trgm_adds_similarity(self):
        manager, conn = fake_manager("postgresql", {"pg_extension": [{"installed": 1}]})
        manager.fts.similarity_threshold = 0.5
        manager.fts.suggest("docs", "title", "postgre", limit=4)
        sql, params, _ = conn.executed[-1]
        assert "similarity(\"title\"::text, %(term)s) >= %(threshold)s" in sql
        assert "ORDER BY is_prefix DESC, score DESC, suggestion LIMIT %(limit)s" in sql
        assert params == {"prefix": "postgre%", "limit": 4, "term": "postgre", "threshold": 0.5}
        # The extension check is cached.
        manager.fts.suggest("docs", "title", "again")
        assert sum("pg_extension" in s for s in conn.sql()) == 1

    def test_install_creates_the_extension(self):
        manager, conn = fake_manager("postgresql", {"pg_extension": []})
        manager.fts.suggest("docs", "title", "po")
        manager.fts.suggest("docs", "title", "po", install=True)
        assert conn.sql().count("CREATE EXTENSION IF NOT EXISTS pg_trgm") == 1
        assert "similarity(" in conn.sql()[-1]


# ---------------------------------------------------------------------------
# Async PostgreSQL backend
# ---------------------------------------------------------------------------

class FakeAsyncBackend:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for needle, rows in self.responses.items():
            if needle in sql:
                return rows
        return []

    @asynccontextmanager
    async def transaction(self):
        self.executed.append(("BEGIN", None))
        yield self
        self.executed.append(("COMMIT", None))


async def test_async_fts_shares_the_postgres_sql():
    backend = FakeAsyncBackend({"pg_extension": [{"installed": 1}], "suggestion": [{"suggestion": "a"}]})
    fts = AsyncFullTextSearch(backend)
    await fts.create_index("docs", ["title"])
    assert [s for s, _ in backend.executed] == [
        "BEGIN",
        'CREATE INDEX "docs_fts" ON "docs" USING GIN (to_tsvector(\'english\', coalesce("title"::text, \'\')))',
        "COMMIT",
    ]
    backend.executed.clear()

    await fts.search("docs", ["title"], MALICIOUS)
    sql, params = backend.executed[-1]
    assert MALICIOUS not in sql and params["query"] == MALICIOUS
    assert await fts.search("docs", ["title"], "  ") == []

    assert await fts.suggest("docs", "title", "a") == ["a"]
    sql, params = backend.executed[-1]
    assert "similarity(" in sql and params["term"] == "a"

    await fts.drop_index("docs")
    assert backend.executed[-1] == ('DROP INDEX IF EXISTS "docs_fts"', None)

    with pytest.raises(ValueError):
        await fts.create_index("docs;", ["title"])


def test_async_backend_exposes_fts():
    pytest.importorskip("psycopg")
    from nexusql import AsyncPostgresBackend
    backend = AsyncPostgresBackend("postgresql://u:p@localhost/db")
    assert isinstance(backend.fts, AsyncFullTextSearch)
    assert backend.fts is backend.fts


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_validate_identifier(self):
        assert validate_identifier("Docs_1") == "Docs_1"
        assert validate_identifier("_x") == "_x"
        for bad in ["", "1a", "a-b", "a b", "a.b", 'a"b', "a;", "x" * 64, None, 5]:
            with pytest.raises(ValueError):
                validate_identifier(bad)

    def test_quote_identifier(self):
        assert quote_identifier("t", DatabaseType.POSTGRESQL) == '"t"'
        assert quote_identifier("t", DatabaseType.SQLITE) == '"t"'
        assert quote_identifier("t", DatabaseType.MYSQL) == "`t`"
        assert quote_identifier("t", DatabaseType.MSSQL) == "[t]"

    def test_like_prefix(self):
        assert like_prefix("abc", DatabaseType.SQLITE) == "abc%"
        assert like_prefix("100%_!", DatabaseType.POSTGRESQL) == "100!%!_!!%"
        assert like_prefix("[a]", DatabaseType.MYSQL) == "[a]%"
        assert like_prefix("[a]", DatabaseType.MSSQL) == "![a]%"

    def test_fts5_match_query(self):
        assert fts5_match_query("hello world", ["a", "b"]) == '{a b} : "hello" "world"'
        assert fts5_match_query('say "hi" AND', ["a"]) == '{a} : "say" """hi""" "AND"'
        assert fts5_match_query("x", []) == '"x"'
