# Full-Text Search

`db.fts` builds full-text indexes, runs ranked searches and offers
completions with one API on SQLite, PostgreSQL, MySQL and SQL Server. The
engines differ a great deal underneath; this page says what each one does
and where the behaviour cannot be made identical.

```python
db.fts.create_index("articles", ["title", "body"])

hits = db.fts.search("articles", ["title", "body"], "postgres indexing", limit=10)
for hit in hits:
    print(hit["rank"], hit["title"])          # the row's columns plus rank

words = db.fts.suggest("articles", "title", "postg")   # ["PostgreSQL ...", ...]

db.fts.drop_index("articles")
```

The async backend exposes the same methods, awaited, on
`AsyncPostgresBackend.fts`.

## API

### `create_index(table, columns, name=None)`

Builds a full-text index over `columns` of `table`. `name` defaults to
`<table>_fts` and is what `drop_index` (and, on SQLite, `search`) use to find
the objects again. Rows already in the table are indexed; later writes are
picked up automatically.

### `drop_index(table, name=None)`

Reverses `create_index`: the index, and on SQLite the triggers and FTS
table, on SQL Server the catalog.

### `search(table, columns, query, limit=20, offset=0, name=None) -> List[Dict]`

Rows of `table` matching `query`, best first. Each row is the table's
columns plus `rank`; higher is a better match on every backend. Words are
ANDed on every backend except MySQL (see below). An empty or
whitespace-only query returns `[]`. `columns` should be the columns the
index was built over. `name` is only consulted on SQLite, to find the FTS
table.

### `suggest(table, column, term, limit=10, install=False) -> List[str]`

Distinct values of `column` that complete `term`, for autocompletion. All
backends match case-insensitively on the prefix. On PostgreSQL with the
`pg_trgm` extension present, values *similar* to the term (typos,
transposed letters) follow the prefix matches, ordered by similarity.
`install=True` runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` first, which
needs the right privilege; by default an absent extension just means prefix
matching.

### Settings

`db.fts.config` is the PostgreSQL text search configuration (`'english'` by
default; use `'simple'` for no stemming or stop words). It is baked into the
index expression, so change it before `create_index` and keep it the same
for `search`. `db.fts.similarity_threshold` (default `0.3`) is the minimum
`pg_trgm` similarity for a fuzzy suggestion. Neither affects the other
backends.

## Per backend

| | Index | Search | Suggest |
|---|---|---|---|
| PostgreSQL | GIN expression index on `to_tsvector(config, coalesce(col::text,'') || ' ' || ...)` | `websearch_to_tsquery` + `ts_rank` | `ILIKE` prefix; with `pg_trgm`, plus `similarity()` |
| SQLite | FTS5 external-content table + 3 triggers | `MATCH` on quoted tokens, `-bm25()` as rank | `LIKE` prefix |
| MySQL | `FULLTEXT INDEX` | `MATCH .. AGAINST (.. IN NATURAL LANGUAGE MODE)` | `LIKE` prefix |
| SQL Server | `FULLTEXT CATALOG` + `FULLTEXT INDEX` | `FREETEXTTABLE` joined on the key | `LIKE` prefix |

### PostgreSQL

The index is an expression index rather than a generated `tsvector`
column, so the table itself is not altered, `search()` returns the row
exactly as stored, and `drop_index` is one statement. `search()` rebuilds
the identical expression so the planner can use the index. Columns are cast
to text and NULLs blanked, so any column type can take part and one NULL
does not blank the whole document. The two-argument `to_tsvector` is used
because the one-argument form depends on a session setting and cannot be
indexed.

`websearch_to_tsquery` accepts anything a user might type: quoted phrases,
`-word` for exclusion, `or`. Nothing it is given can raise a syntax error.

### SQLite

`create_index` makes an FTS5 virtual table with `content=<table>`, so the
base table stays the single source of truth and the FTS table holds only the
index, and three `AFTER INSERT / UPDATE / DELETE` triggers keep it in step.
The FTS rowid is the table's `INTEGER PRIMARY KEY` when it has one (an
alias for the rowid), otherwise the implicit rowid; a `WITHOUT ROWID` table
cannot be indexed this way.

FTS5 has its own query language in which an unbalanced quote or a bare
`AND` is an error, so the search string is split on whitespace and each word
is wrapped in double quotes, which FTS5 reads as a literal token. Words are
implicitly ANDed, matching `websearch_to_tsquery`. Operators and prefix
wildcards are therefore not available through `search()` on SQLite.

`bm25()` returns lower-is-better scores; `search()` negates them so `rank`
reads the same way as on the other backends.

### MySQL

`FULLTEXT` indexes need `CHAR`, `VARCHAR` or `TEXT` columns. Natural
language mode ranks rows matching *any* of the words, unlike the AND
semantics elsewhere, and InnoDB ignores words shorter than
`innodb_ft_min_token_size` (3 by default) and its stop-word list. Use the
same column list in `create_index` and `search`: `MATCH` requires a column
list that exactly matches some `FULLTEXT` index.

### SQL Server

Full-text search is an optional instance component. `create_index` checks
`SERVERPROPERTY('IsFullTextInstalled')` and raises `RuntimeError` when it
is `0`. The table needs a single-column, unique, non-nullable key index
(normally the primary key); `create_index` finds it in `sys.indexes` and
raises `ValueError` otherwise. A catalog named after the index is created
when missing and dropped with it by `drop_index`.

Full-text DDL cannot run inside a user transaction on SQL Server, so these
statements run with autocommit on, and calling `create_index` /
`drop_index` inside `with db.transaction():` raises `RuntimeError`. Index
population is asynchronous: rows written just now may take a moment to
become searchable, and `search()` right after `create_index` may briefly
return nothing. `suggest()` on SQL Server also escapes `[`, which its `LIKE`
treats as a wildcard.

## Safety

The search term and suggestion prefix are user input and only ever reach
the database as bound parameters; nothing a user types is interpolated into
SQL. Table, column, index and configuration names are validated against
`^[A-Za-z_][A-Za-z0-9_]{0,62}$` (`ValueError` otherwise) and quoted in the
dialect's style. Schema-qualified names are not accepted; tables resolve in
the connection's default schema or search path.

Because identifiers are quoted, they must match the stored name exactly: a
PostgreSQL table created as `CREATE TABLE Articles` without quotes is stored
as `articles`, and that is the name to pass.

`LIKE` wildcards inside a suggestion prefix are escaped, so
`suggest(t, "title", "100%")` completes the three characters `100%`, not
"anything starting with 100".
