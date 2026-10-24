"""
Portable full-text search, exposed as ``db.fts``.

Every supported database can search text, but each one spells it
differently: PostgreSQL has ``tsvector`` columns and GIN indexes, MySQL has
``FULLTEXT`` indexes and ``MATCH .. AGAINST``, SQLite has the FTS5 virtual
table, and SQL Server has full-text catalogs and ``FREETEXTTABLE``. This
module puts one API in front of the four, so application code builds an
index, searches it and asks for completions without knowing which engine
is underneath.

Design notes
------------
- The search string is user input and only ever reaches the database as a
  bound parameter. Identifiers (table, column, index names) are validated
  against a strict pattern and quoted in the dialect's style; nothing else
  is interpolated into SQL.
- ``rank`` is normalised to "higher is better" on every backend. SQLite's
  ``bm25()`` natively returns lower-is-better scores, so it is negated.
- PostgreSQL gets an expression index rather than a generated ``tsvector``
  column: the table's rows come back unchanged from ``search()``, nothing is
  altered on the table itself, and ``drop_index`` is a single statement.
  ``search()`` rebuilds the identical expression so the planner uses the
  index.
- SQLite gets an external-content FTS5 table kept in step by triggers, so the
  base table remains the single source of truth and the FTS table stores
  only the index.
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .interfaces import DatabaseType

# One bare SQL identifier: letters, digits and underscores, not starting with
# a digit, at most 63 characters (PostgreSQL's limit, the strictest of the
# four). Schema qualification is deliberately not accepted; tables resolve in
# the connection's default schema.
IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')

# Escape character for LIKE prefixes. The conventional backslash would need
# a different spelling on MySQL, where a backslash is also an escape inside
# string literals; '!' is a plain character in every dialect.
LIKE_ESCAPE = '!'

# Minimum pg_trgm similarity for a fuzzy suggestion, matching the extension's
# own default for the % operator.
DEFAULT_SIMILARITY_THRESHOLD = 0.3

PG_TRGM_INSTALLED_SQL = "SELECT 1 AS installed FROM pg_extension WHERE extname = 'pg_trgm'"
PG_TRGM_INSTALL_SQL = "CREATE EXTENSION IF NOT EXISTS pg_trgm"

MSSQL_FULLTEXT_INSTALLED_SQL = (
    "SELECT CAST(SERVERPROPERTY('IsFullTextInstalled') AS INT) AS installed"
)

# The single-column, unique, non-nullable index full-text indexing keys on.
# The primary key is preferred; any other qualifying unique index will do.
MSSQL_KEY_INDEX_SQL = """
    SELECT i.name AS index_name, c.name AS column_name
    FROM sys.indexes i
    JOIN sys.index_columns ic
      ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    JOIN sys.columns c
      ON c.object_id = ic.object_id AND c.column_id = ic.column_id
    WHERE i.object_id = OBJECT_ID(:table)
      AND i.is_unique = 1
      AND c.is_nullable = 0
      AND (SELECT COUNT(*) FROM sys.index_columns k
           WHERE k.object_id = i.object_id AND k.index_id = i.index_id) = 1
    ORDER BY i.is_primary_key DESC, i.index_id
"""

MSSQL_CATALOG_EXISTS_SQL = "SELECT 1 AS present FROM sys.fulltext_catalogs WHERE name = :name"

SQLITE_COLUMNS_SQL = "SELECT name, type, pk FROM pragma_table_info(:table)"


# --- validation and quoting ----------------------------------------------------

def validate_identifier(name: Any, kind: str = "identifier") -> str:
    """``name`` unchanged if it is a safe bare identifier, else ValueError."""
    if not isinstance(name, str) or not IDENTIFIER.match(name):
        raise ValueError(
            f"Invalid {kind} {name!r}: expected letters, digits and underscores "
            "only, not starting with a digit, at most 63 characters"
        )
    return name


def quote_identifier(name: str, db_type: DatabaseType) -> str:
    """Quote a validated identifier in the dialect's own style."""
    if db_type == DatabaseType.MYSQL:
        return f"`{name}`"
    if db_type == DatabaseType.MSSQL:
        return f"[{name}]"
    return f'"{name}"'


def like_prefix(term: str, db_type: DatabaseType) -> str:
    """
    A LIKE pattern matching values that start with ``term``.

    Wildcards inside the term are escaped so "100%" means the three
    characters, not "100 followed by anything". SQL Server also treats
    ``[`` as a wildcard, so it is escaped there too.
    """
    specials = LIKE_ESCAPE + '%_'
    if db_type == DatabaseType.MSSQL:
        specials += '['
    escaped = ''.join(LIKE_ESCAPE + ch if ch in specials else ch for ch in term)
    return escaped + '%'


def fts5_match_query(text: str, columns: Sequence[str]) -> str:
    """
    Turn free text into an FTS5 MATCH expression that cannot fail to parse.

    FTS5 has its own query language in which an unbalanced quote or a bare
    AND is a syntax error. Each whitespace-separated word is wrapped in
    double quotes, which FTS5 reads as a literal token, and the words are
    joined with FTS5's implicit AND. That mirrors ``websearch_to_tsquery``
    on PostgreSQL, where plain words are also ANDed. Restricting matches to
    ``columns`` uses the column-filter syntax; the names are validated
    identifiers.
    """
    tokens = ['"' + word.replace('"', '""') + '"' for word in text.split()]
    filter_prefix = '{' + ' '.join(columns) + '} : ' if columns else ''
    return filter_prefix + ' '.join(tokens)


def _check_search_args(table, columns, query, limit, offset):
    table = validate_identifier(table, "table name")
    columns = [validate_identifier(c, "column name") for c in columns]
    if not columns:
        raise ValueError("At least one column is required")
    if not isinstance(query, str):
        raise TypeError(f"query must be a string, got {type(query).__name__}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(f"offset must be a non-negative integer, got {offset!r}")
    return table, columns


def _index_name(table: str, name: Optional[str]) -> str:
    return validate_identifier(name if name is not None else f"{table}_fts", "index name")


# --- PostgreSQL SQL ------------------------------------------------------------
#
# Module-level so the async backend's PostgreSQL branch shares them with the
# sync manager byte for byte.

def pg_document_expression(columns: Sequence[str], config: str) -> str:
    """
    The tsvector expression a PostgreSQL index and its queries share.

    The two-argument ``to_tsvector`` is IMMUTABLE, which an index expression
    has to be; the one-argument form depends on a session setting and is not.
    Columns are cast to text and NULLs blanked so any column type can take
    part and one NULL does not blank the whole document.
    """
    parts = ' || \' \' || '.join(
        f'coalesce({quote_identifier(c, DatabaseType.POSTGRESQL)}::text, \'\')'
        for c in columns
    )
    return f"to_tsvector('{config}', {parts})"


def pg_create_index_sql(table: str, columns: Sequence[str], name: str, config: str) -> List[str]:
    q = lambda n: quote_identifier(n, DatabaseType.POSTGRESQL)
    expr = pg_document_expression(columns, config)
    return [f"CREATE INDEX {q(name)} ON {q(table)} USING GIN ({expr})"]


def pg_drop_index_sql(name: str) -> List[str]:
    return [f"DROP INDEX IF EXISTS {quote_identifier(name, DatabaseType.POSTGRESQL)}"]


def pg_search_sql(table: str, columns: Sequence[str], config: str) -> str:
    q = lambda n: quote_identifier(n, DatabaseType.POSTGRESQL)
    expr = pg_document_expression(columns, config)
    tsquery = f"websearch_to_tsquery('{config}', :query)"
    return (
        f'SELECT t.*, ts_rank({expr}, {tsquery}) AS "rank" '
        f"FROM {q(table)} t "
        f"WHERE {expr} @@ {tsquery} "
        f'ORDER BY "rank" DESC '
        f"LIMIT :limit OFFSET :offset"
    )


def pg_suggest_sql(table: str, column: str, fuzzy: bool) -> str:
    """
    Completion query. With pg_trgm, prefix matches come first and trigram
    neighbours follow by similarity, so a typo still finds its target while
    a plain prefix keeps the ordering users expect from an autocomplete.
    """
    q = lambda n: quote_identifier(n, DatabaseType.POSTGRESQL)
    col = f"{q(column)}::text"
    prefix = f"{col} ILIKE :prefix ESCAPE '{LIKE_ESCAPE}'"
    if not fuzzy:
        return (
            f"SELECT DISTINCT {col} AS suggestion FROM {q(table)} "
            f"WHERE {prefix} ORDER BY suggestion LIMIT :limit"
        )
    return (
        f"SELECT DISTINCT {col} AS suggestion, ({prefix}) AS is_prefix, "
        f"similarity({col}, :term) AS score "
        f"FROM {q(table)} "
        f"WHERE {prefix} OR similarity({col}, :term) >= :threshold "
        f"ORDER BY is_prefix DESC, score DESC, suggestion LIMIT :limit"
    )


# --- SQLite SQL ----------------------------------------------------------------

def sqlite_create_index_sql(table: str, columns: Sequence[str], name: str, key: str) -> List[str]:
    """
    External-content FTS5 table plus the three triggers that keep it current.

    ``key`` is the base table's INTEGER PRIMARY KEY (a rowid alias) or
    ``rowid`` itself; FTS5 external content joins on it. Deleting from an
    external-content table requires the old column values, so the DELETE and
    UPDATE triggers hand them over from ``old``. The final statement indexes
    the rows already in the table.
    """
    q = lambda n: quote_identifier(n, DatabaseType.SQLITE)
    fts = q(name)
    cols = ', '.join(q(c) for c in columns)
    new_values = ', '.join(f'new.{q(c)}' for c in columns)
    old_values = ', '.join(f'old.{q(c)}' for c in columns)
    key_ref = 'rowid' if key == 'rowid' else q(key)
    options = f"content='{table}'"
    if key != 'rowid':
        options += f", content_rowid='{key}'"

    insert_new = f"INSERT INTO {fts}(rowid, {cols}) VALUES (new.{key_ref}, {new_values});"
    delete_old = (
        f"INSERT INTO {fts}({fts}, rowid, {cols}) "
        f"VALUES ('delete', old.{key_ref}, {old_values});"
    )
    return [
        f"CREATE VIRTUAL TABLE {fts} USING fts5({cols}, {options})",
        f"CREATE TRIGGER {q(name + '_ai')} AFTER INSERT ON {q(table)} BEGIN {insert_new} END",
        f"CREATE TRIGGER {q(name + '_ad')} AFTER DELETE ON {q(table)} BEGIN {delete_old} END",
        f"CREATE TRIGGER {q(name + '_au')} AFTER UPDATE ON {q(table)} BEGIN "
        f"{delete_old} {insert_new} END",
        f"INSERT INTO {fts}({fts}) VALUES ('rebuild')",
    ]


def sqlite_drop_index_sql(name: str) -> List[str]:
    q = lambda n: quote_identifier(n, DatabaseType.SQLITE)
    return [
        f"DROP TRIGGER IF EXISTS {q(name + '_ai')}",
        f"DROP TRIGGER IF EXISTS {q(name + '_ad')}",
        f"DROP TRIGGER IF EXISTS {q(name + '_au')}",
        f"DROP TABLE IF EXISTS {q(name)}",
    ]


def sqlite_search_sql(table: str, name: str, key: str) -> str:
    q = lambda n: quote_identifier(n, DatabaseType.SQLITE)
    fts = q(name)
    key_ref = 'rowid' if key == 'rowid' else q(key)
    # bm25() is lower-is-better; negate so rank reads like the other
    # backends. The ORDER BY repeats the call rather than naming the alias
    # because FTS5 tables also expose a hidden column called rank.
    return (
        f'SELECT t.*, -bm25({fts}) AS "rank" '
        f"FROM {fts} JOIN {q(table)} t ON t.{key_ref} = {fts}.rowid "
        f"WHERE {fts} MATCH :query "
        f"ORDER BY bm25({fts}) LIMIT :limit OFFSET :offset"
    )


# --- MySQL SQL -----------------------------------------------------------------

def mysql_create_index_sql(table: str, columns: Sequence[str], name: str) -> List[str]:
    q = lambda n: quote_identifier(n, DatabaseType.MYSQL)
    cols = ', '.join(q(c) for c in columns)
    return [f"CREATE FULLTEXT INDEX {q(name)} ON {q(table)} ({cols})"]


def mysql_drop_index_sql(table: str, name: str) -> List[str]:
    q = lambda n: quote_identifier(n, DatabaseType.MYSQL)
    return [f"DROP INDEX {q(name)} ON {q(table)}"]


def mysql_search_sql(table: str, columns: Sequence[str]) -> str:
    q = lambda n: quote_identifier(n, DatabaseType.MYSQL)
    cols = ', '.join(f't.{q(c)}' for c in columns)
    match = f"MATCH ({cols}) AGAINST (:query IN NATURAL LANGUAGE MODE)"
    # rank is a reserved word from MySQL 8.0.2, hence the backticks.
    return (
        f"SELECT t.*, {match} AS `rank` FROM {q(table)} t "
        f"WHERE {match} ORDER BY `rank` DESC LIMIT :limit OFFSET :offset"
    )


# --- SQL Server SQL ------------------------------------------------------------

def mssql_create_index_sql(table: str, columns: Sequence[str], name: str,
                           key_index: str, create_catalog: bool) -> List[str]:
    """
    One catalog per index, named after it, so ``drop_index`` can remove
    both without touching catalogs other code created.
    """
    q = lambda n: quote_identifier(n, DatabaseType.MSSQL)
    cols = ', '.join(q(c) for c in columns)
    statements = []
    if create_catalog:
        statements.append(f"CREATE FULLTEXT CATALOG {q(name)}")
    statements.append(
        f"CREATE FULLTEXT INDEX ON {q(table)} ({cols}) "
        f"KEY INDEX {q(key_index)} ON {q(name)} WITH CHANGE_TRACKING AUTO"
    )
    return statements


def mssql_drop_index_sql(table: str, name: str, drop_catalog: bool) -> List[str]:
    q = lambda n: quote_identifier(n, DatabaseType.MSSQL)
    statements = [f"DROP FULLTEXT INDEX ON {q(table)}"]
    if drop_catalog:
        statements.append(f"DROP FULLTEXT CATALOG {q(name)}")
    return statements


def mssql_search_sql(table: str, columns: Sequence[str], key_column: str) -> str:
    q = lambda n: quote_identifier(n, DatabaseType.MSSQL)
    cols = ', '.join(q(c) for c in columns)
    return (
        f"SELECT t.*, ft.[RANK] AS [rank] "
        f"FROM FREETEXTTABLE({q(table)}, ({cols}), :query) AS ft "
        f"JOIN {q(table)} AS t ON t.{q(key_column)} = ft.[KEY] "
        f"ORDER BY ft.[RANK] DESC "
        f"OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY"
    )


def like_suggest_sql(table: str, column: str, db_type: DatabaseType) -> str:
    """Prefix completion for the backends without a fuzzy operator."""
    q = lambda n: quote_identifier(n, db_type)
    where = f"WHERE {q(column)} LIKE :prefix ESCAPE '{LIKE_ESCAPE}'"
    if db_type == DatabaseType.MSSQL:
        return (
            f"SELECT DISTINCT TOP (:limit) {q(column)} AS suggestion "
            f"FROM {q(table)} {where} ORDER BY suggestion"
        )
    return (
        f"SELECT DISTINCT {q(column)} AS suggestion FROM {q(table)} {where} "
        f"ORDER BY suggestion LIMIT :limit"
    )


# --- sync API ------------------------------------------------------------------

class FullTextSearch:
    """
    Full-text search helpers bound to a ``DatabaseManager``.

    Reached as ``db.fts``; not normally constructed directly.

        db.fts.create_index("articles", ["title", "body"])
        hits = db.fts.search("articles", ["title", "body"], "postgres indexing")
        words = db.fts.suggest("articles", "title", "postg")

    ``config`` names the PostgreSQL text search configuration used for
    stemming and stop words (``'english'`` by default; ``'simple'`` for
    none). ``similarity_threshold`` is the minimum pg_trgm similarity a
    fuzzy suggestion needs. Neither affects the other backends.
    """

    def __init__(self, db):
        self._db = db
        self.config = 'english'
        self.similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD
        self._trgm_installed: Optional[bool] = None
        self._mssql_fulltext_installed: Optional[bool] = None
        self._mssql_keys: Dict[str, Tuple[str, str]] = {}

    @property
    def _db_type(self) -> DatabaseType:
        return self._db.config.database_type

    def _config(self) -> str:
        return validate_identifier(self.config, "text search configuration")

    # -- index management --------------------------------------------------

    def create_index(self, table: str, columns: Sequence[str], name: Optional[str] = None) -> None:
        """
        Build a full-text index over ``columns`` of ``table``.

        ``name`` defaults to ``<table>_fts``. Rows already in the table are
        indexed, and later writes are picked up automatically on every
        backend (SQLite via triggers this creates). SQL Server populates the
        index in the background, so rows may take a moment to become
        searchable there.
        """
        table = validate_identifier(table, "table name")
        columns = [validate_identifier(c, "column name") for c in columns]
        if not columns:
            raise ValueError("At least one column is required")
        name = _index_name(table, name)
        db_type = self._db_type

        if db_type == DatabaseType.POSTGRESQL:
            self._run_ddl(pg_create_index_sql(table, columns, name, self._config()))
        elif db_type == DatabaseType.SQLITE:
            key = self._sqlite_key(table)
            self._run_ddl(sqlite_create_index_sql(table, columns, name, key))
        elif db_type == DatabaseType.MYSQL:
            self._run_ddl(mysql_create_index_sql(table, columns, name))
        elif db_type == DatabaseType.MSSQL:
            self._require_mssql_fulltext()
            key_index, _ = self._mssql_key(table)
            has_catalog = self._db.fetch_one(MSSQL_CATALOG_EXISTS_SQL, {"name": name}) is not None
            self._run_mssql_ddl(
                mssql_create_index_sql(table, columns, name, key_index, not has_catalog))
        else:
            raise NotImplementedError(f"Full-text search is not supported on {db_type}")

    def drop_index(self, table: str, name: Optional[str] = None) -> None:
        """Remove the index ``create_index`` built, and its triggers or catalog."""
        table = validate_identifier(table, "table name")
        name = _index_name(table, name)
        db_type = self._db_type

        if db_type == DatabaseType.POSTGRESQL:
            self._run_ddl(pg_drop_index_sql(name))
        elif db_type == DatabaseType.SQLITE:
            self._run_ddl(sqlite_drop_index_sql(name))
        elif db_type == DatabaseType.MYSQL:
            self._run_ddl(mysql_drop_index_sql(table, name))
        elif db_type == DatabaseType.MSSQL:
            has_catalog = self._db.fetch_one(MSSQL_CATALOG_EXISTS_SQL, {"name": name}) is not None
            self._run_mssql_ddl(mssql_drop_index_sql(table, name, has_catalog))
        else:
            raise NotImplementedError(f"Full-text search is not supported on {db_type}")

    # -- querying ------------------------------------------------------------

    def search(self, table: str, columns: Sequence[str], query: str,
               limit: int = 20, offset: int = 0, name: Optional[str] = None) -> List[Dict]:
        """
        Rows of ``table`` matching ``query`` in ``columns``, best first.

        Each row carries the table's columns plus ``rank``, higher meaning a
        better match. Words are ANDed on every backend except MySQL, whose
        natural-language mode ranks rows matching any word. ``name`` is the
        index name given to ``create_index``, needed on SQLite to find the
        FTS table; the other backends ignore it. An empty query matches
        nothing.
        """
        table, columns = _check_search_args(table, columns, query, limit, offset)
        if not query.split():
            return []
        db_type = self._db_type
        params: Dict[str, Any] = {"query": query, "limit": limit, "offset": offset}

        if db_type == DatabaseType.POSTGRESQL:
            sql = pg_search_sql(table, columns, self._config())
        elif db_type == DatabaseType.SQLITE:
            fts_name = _index_name(table, name)
            sql = sqlite_search_sql(table, fts_name, self._sqlite_key(table))
            params["query"] = fts5_match_query(query, columns)
        elif db_type == DatabaseType.MYSQL:
            sql = mysql_search_sql(table, columns)
        elif db_type == DatabaseType.MSSQL:
            _, key_column = self._mssql_key(table)
            sql = mssql_search_sql(table, columns, key_column)
        else:
            raise NotImplementedError(f"Full-text search is not supported on {db_type}")

        return self._db.execute(sql, params)

    def suggest(self, table: str, column: str, term: str, limit: int = 10,
                install: bool = False) -> List[str]:
        """
        Distinct values of ``column`` that complete ``term``, best first.

        On PostgreSQL with the pg_trgm extension present, values similar to
        the term (typos, transpositions) are returned after the prefix
        matches, ordered by similarity. ``install=True`` creates the
        extension if it is missing, which needs the appropriate privilege;
        by default an absent extension just means prefix matching, as on the
        other backends.
        """
        table = validate_identifier(table, "table name")
        column = validate_identifier(column, "column name")
        if not isinstance(term, str):
            raise TypeError(f"term must be a string, got {type(term).__name__}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        if not term.strip():
            return []
        db_type = self._db_type
        params: Dict[str, Any] = {"prefix": like_prefix(term, db_type), "limit": limit}

        if db_type == DatabaseType.POSTGRESQL:
            fuzzy = self._pg_trgm_available(install)
            sql = pg_suggest_sql(table, column, fuzzy)
            if fuzzy:
                params.update(term=term, threshold=self.similarity_threshold)
        else:
            sql = like_suggest_sql(table, column, db_type)

        return [row["suggestion"] for row in self._db.execute(sql, params)]

    # -- backend details -------------------------------------------------------

    def _run_ddl(self, statements: List[str]) -> None:
        """Run DDL as one unit so a failure part-way leaves nothing behind."""
        with self._db.transaction():
            for sql in statements:
                self._db.execute(sql)

    def _run_mssql_ddl(self, statements: List[str]) -> None:
        """
        SQL Server refuses full-text DDL inside a user transaction, and
        pyodbc opens one implicitly on every statement, so these run with
        autocommit switched on for the duration.
        """
        if self._db._in_transaction:
            raise RuntimeError(
                "SQL Server full-text index DDL cannot run inside a transaction; "
                "call it outside `with db.transaction()`"
            )
        with self._db._borrow() as conn:
            conn.autocommit = True
            try:
                for sql in statements:
                    cursor = conn.cursor()
                    cursor.execute(sql)
                    cursor.close()
            finally:
                conn.autocommit = False

    def _sqlite_key(self, table: str) -> str:
        """
        The column FTS5 external content joins on: the table's INTEGER
        PRIMARY KEY when it has one (an alias for rowid, so the FTS rowid
        matches it), otherwise the implicit rowid.
        """
        rows = self._db.execute(SQLITE_COLUMNS_SQL, {"table": table})
        if not rows:
            raise ValueError(f"Table {table!r} does not exist")
        keys = [r for r in rows if r["pk"]]
        if len(keys) == 1 and (keys[0]["type"] or '').strip().upper() == 'INTEGER':
            return keys[0]["name"]
        return 'rowid'

    def _require_mssql_fulltext(self) -> None:
        if self._mssql_fulltext_installed is None:
            row = self._db.fetch_one(MSSQL_FULLTEXT_INSTALLED_SQL)
            self._mssql_fulltext_installed = bool(row and row.get("installed"))
        if not self._mssql_fulltext_installed:
            raise RuntimeError(
                "SQL Server full-text search is not installed on this instance "
                "(SERVERPROPERTY('IsFullTextInstalled') = 0). Add the Full-Text "
                "and Semantic Extractions for Search feature to the instance."
            )

    def _mssql_key(self, table: str) -> Tuple[str, str]:
        """(index name, column name) of the table's full-text key, cached."""
        key = self._mssql_keys.get(table)
        if key is None:
            row = self._db.fetch_one(MSSQL_KEY_INDEX_SQL, {"table": table})
            if row is None:
                raise ValueError(
                    f"SQL Server full-text indexing needs a single-column, unique, "
                    f"non-nullable key index on {table}; none was found"
                )
            key = (row["index_name"], row["column_name"])
            self._mssql_keys[table] = key
        return key

    def _pg_trgm_available(self, install: bool) -> bool:
        if install and not self._trgm_installed:
            self._db.execute(PG_TRGM_INSTALL_SQL)
            self._trgm_installed = True
        if self._trgm_installed is None:
            self._trgm_installed = self._db.fetch_one(PG_TRGM_INSTALLED_SQL) is not None
        return self._trgm_installed


# --- async PostgreSQL API --------------------------------------------------------

class AsyncFullTextSearch:
    """
    The PostgreSQL branch of ``FullTextSearch`` for ``AsyncPostgresBackend``,
    reached as ``backend.fts``. Same SQL, awaited.
    """

    def __init__(self, backend):
        self._backend = backend
        self.config = 'english'
        self.similarity_threshold = DEFAULT_SIMILARITY_THRESHOLD
        self._trgm_installed: Optional[bool] = None

    def _config(self) -> str:
        return validate_identifier(self.config, "text search configuration")

    async def create_index(self, table: str, columns: Sequence[str], name: Optional[str] = None) -> None:
        """Build a GIN index over the tsvector of ``columns``; see FullTextSearch."""
        table = validate_identifier(table, "table name")
        columns = [validate_identifier(c, "column name") for c in columns]
        if not columns:
            raise ValueError("At least one column is required")
        name = _index_name(table, name)
        async with self._backend.transaction():
            for sql in pg_create_index_sql(table, columns, name, self._config()):
                await self._backend.execute(sql)

    async def drop_index(self, table: str, name: Optional[str] = None) -> None:
        """Drop the index ``create_index`` built."""
        table = validate_identifier(table, "table name")
        name = _index_name(table, name)
        for sql in pg_drop_index_sql(name):
            await self._backend.execute(sql)

    async def search(self, table: str, columns: Sequence[str], query: str,
                     limit: int = 20, offset: int = 0, name: Optional[str] = None) -> List[Dict]:
        """Matching rows plus ``rank``, best first; see FullTextSearch.search."""
        table, columns = _check_search_args(table, columns, query, limit, offset)
        if not query.split():
            return []
        return await self._backend.execute(
            pg_search_sql(table, columns, self._config()),
            {"query": query, "limit": limit, "offset": offset},
        )

    async def suggest(self, table: str, column: str, term: str, limit: int = 10,
                      install: bool = False) -> List[str]:
        """Completions of ``term`` from ``column``; see FullTextSearch.suggest."""
        table = validate_identifier(table, "table name")
        column = validate_identifier(column, "column name")
        if not isinstance(term, str):
            raise TypeError(f"term must be a string, got {type(term).__name__}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        if not term.strip():
            return []
        fuzzy = await self._pg_trgm_available(install)
        params: Dict[str, Any] = {
            "prefix": like_prefix(term, DatabaseType.POSTGRESQL), "limit": limit}
        if fuzzy:
            params.update(term=term, threshold=self.similarity_threshold)
        rows = await self._backend.execute(pg_suggest_sql(table, column, fuzzy), params)
        return [row["suggestion"] for row in rows]

    async def _pg_trgm_available(self, install: bool) -> bool:
        if install and not self._trgm_installed:
            await self._backend.execute(PG_TRGM_INSTALL_SQL)
            self._trgm_installed = True
        if self._trgm_installed is None:
            rows = await self._backend.execute(PG_TRGM_INSTALLED_SQL)
            self._trgm_installed = bool(rows)
        return self._trgm_installed
