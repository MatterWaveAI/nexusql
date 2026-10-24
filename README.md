# NexusQL

Multi-database abstraction layer with unified API for PostgreSQL, MySQL, SQLite, and MSSQL.

## Features

- ✅ **Unified API** - Same code works across 4 databases (PostgreSQL, MySQL, SQLite, MSSQL)
- ✅ **Named Parameters** - Safe from SQL injection with `:param` syntax
- ✅ **Auto-migrations** - Versioned schema management with automatic translation
- ✅ **SQL Translation** - Write PostgreSQL canonical syntax, automatically translates to all databases
- ✅ **Type Safety** - Full typing support with Python 3.10+
- ✅ **Async Ready** - Async/await compatible API
- ✅ **Production Ready** - Comprehensive test coverage

## Installation

```bash
# SQLite only (no additional dependencies)
pip install nexusql

# With PostgreSQL support (sync and async, both via psycopg)
pip install nexusql[postgresql]

# With MySQL support
pip install nexusql[mysql]

# With MSSQL support
pip install nexusql[mssql]

# With all database drivers
pip install nexusql[all]

# For development (includes all drivers and testing tools)
pip install nexusql[dev]
```

## Quick Start

```python
from nexusql import DatabaseManager

# SQLite (no server needed)
db = DatabaseManager("sqlite:///app.db")

# PostgreSQL
db = DatabaseManager("postgresql://user:pass@localhost:5432/mydb")

# MySQL
db = DatabaseManager("mysql://user:pass@localhost:3306/mydb")

# MSSQL
db = DatabaseManager("mssql+pyodbc://user:pass@localhost:1433/mydb")

# Initialize with schema migrations
await db.initialize(apply_schema=True)

# Execute queries with named parameters (safe from SQL injection)
users = db.execute(
    "SELECT * FROM users WHERE email = :email",
    {"email": "user@example.com"}
)

# Fetch single row
user = db.fetch_one(
    "SELECT * FROM users WHERE id = :id",
    {"id": 123}
)

# Fetch all rows
all_users = db.execute(
    "SELECT * FROM users WHERE active = :active",
    {"active": True}
)

# Execute INSERT/UPDATE/DELETE
db.execute(
    "INSERT INTO users (email, name) VALUES (:email, :name)",
    {"email": "new@example.com", "name": "John Doe"}
)
```

## Transactions

Use `transaction()` as a context manager. It commits when the block finishes
and rolls back if the block raises:

```python
with db.transaction():
    db.execute("UPDATE accounts SET balance = balance - :amt WHERE id = :id",
               {"amt": 100, "id": 1})
    db.execute("UPDATE accounts SET balance = balance + :amt WHERE id = :id",
               {"amt": 100, "id": 2})
```

Nesting joins the outermost transaction rather than opening a second one, so
a helper that opens a transaction can safely be called from inside another.

The transaction is scoped to the calling thread, so two threads can run
independent transactions through the same manager.

### Upgrading from 0.1.x

0.3.0 is the first release of these changes. (They also went out as 0.2.0;
0.3.0 is the same breaking change, with corrected package metadata.)

PostgreSQL now uses psycopg (psycopg3) for both the sync and async paths,
replacing psycopg2. `nexusql[postgresql]` installs the one driver both paths
use:

```bash
# 0.1.x
pip install nexusql[postgresql]      # psycopg2-binary, sync only

# 0.3.0
pip install nexusql[postgresql]      # psycopg, sync and async
```

Query code is unaffected: `:name` parameters and PostgreSQL-canonical SQL work
as before. Code reaching past the manager into `psycopg2` directly will need
updating, since the connections it hands out are now psycopg connections.

`fetch_all` has been removed. It returned exactly what `execute` returns for a
SELECT, so callers move to `execute`:

```python
# 0.1.x
rows = db.fetch_all("SELECT * FROM users WHERE active = :active", {"active": True})

# 0.3.0
rows = db.execute("SELECT * FROM users WHERE active = :active", {"active": True})
```

`fetch_one` stays, since `Optional[Dict]` is a genuinely different shape from a
list, and it gains an async form, `fetch_one_async`.

The string protocol for transactions has been removed. `execute("BEGIN")` and
its counterparts now raise `RuntimeError` naming the replacement, rather than
being passed to the driver where they would open a transaction the manager
could not track:

```python
# 0.1.x
db.execute("BEGIN")
db.execute("INSERT ...")
db.execute("COMMIT")

# 0.3.0
with db.transaction():
    db.execute("INSERT ...")
```

## Connection Pooling

Server backends are pooled, so concurrent callers get their own connections
instead of serializing through one:

```python
db = DatabaseManager(
    "postgresql://user:pass@localhost/mydb",
    pool_min=1,         # connections opened when connecting
    pool_max=10,        # ceiling on concurrent connections
    pool_timeout=30,    # seconds to wait for a free connection
    connect_timeout=10, # seconds to wait for the pool to open
)
```

PostgreSQL uses psycopg's `ConnectionPool`. MySQL and MSSQL use a
built-in pool, since their drivers ship none. Pooled connections are
health-checked on checkout, so a connection dropped by a server restart or an
idle timeout is replaced rather than handed to the caller.

SQLite is deliberately not pooled: its connections are bound to the thread
that created them, and the target is a local file with no network round-trip
to overlap.

`connect()` waits for the pool to open, so an unreachable server is reported
there (returning `False`) rather than surfacing later on an unrelated query.
It is separate from `pool_timeout` so a slow checkout budget does not make
startup failures slow to detect.

Set `pool_max=1` for the single-connection behavior of earlier versions.
Exhausting the pool raises `PoolTimeout` after `pool_timeout` seconds rather
than blocking indefinitely.

## SQL Translation

NexusQL uses PostgreSQL as the canonical SQL syntax and automatically translates to other databases:

```python
# Write once in PostgreSQL syntax
create_table_sql = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
)
"""

# Works on all databases! NexusQL translates:
# - SERIAL → INTEGER AUTOINCREMENT (SQLite)
# - SERIAL → INT AUTO_INCREMENT (MySQL)
# - SERIAL → INT IDENTITY(1,1) (MSSQL)
# - BOOLEAN → INTEGER (SQLite), TINYINT(1) (MySQL), BIT (MSSQL)
# - JSONB → TEXT (SQLite), JSON (MySQL), NVARCHAR(MAX) (MSSQL)
# - NOW() → CURRENT_TIMESTAMP (SQLite), GETDATE() (MSSQL)

db.execute(create_table_sql)
```

## Migrations

NexusQL includes a built-in migration system:

```python
from nexusql import DatabaseManager
from pathlib import Path

db = DatabaseManager("sqlite:///app.db")

# Run system migrations + app-specific migrations
await db.initialize(
    apply_schema=True,
    app_migration_paths=[
        "path/to/your/migrations"
    ]
)
```

Migration files follow the pattern `V001__description.sql`:

```sql
-- V001__create_users_table.sql
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
```

### Rolling back

A migration can be undone when it ships a down script, either as a sibling
file or as a `-- down` section at the end of the migration itself:

```
migrations/
  V001__create_users_table.sql
  V001__create_users_table.down.sql   # sibling file
  V002__add_status.sql                # ...or a "-- down" section inside
```

```sql
-- V002__add_status.sql
ALTER TABLE users ADD COLUMN status VARCHAR(20);

-- down
ALTER TABLE users DROP COLUMN status;
```

```python
from nexusql import MigrationRunner

runner = MigrationRunner(db, Path("path/to/your/migrations"))

await runner.plan_rollback(steps=2)        # what would run, newest first
await runner.rollback()                    # undo the newest migration
await runner.rollback_to("V001__create_users_table")  # undo everything after V001
await runner.applied()                     # MigrationRecord list
await runner.pending()                     # files not yet applied
```

Rollback refuses up front, before touching anything, when any migration in
the plan has no down script. A down script is checksummed when the migration
is applied; changing it afterwards warns at rollback time and the current
script runs. Changing the up script still blocks, as before. See
[docs/MIGRATIONS.md](docs/MIGRATIONS.md) for the details and per-backend
transaction behaviour.

## Named Parameters

NexusQL enforces named parameters for security and clarity:

```python
# ✅ Good - Named parameters
db.execute(
    "SELECT * FROM users WHERE email = :email AND status = :status",
    {"email": "user@example.com", "status": "active"}
)

# ❌ Bad - Don't do string concatenation
# db.execute(f"SELECT * FROM users WHERE email = '{email}'")  # SQL injection risk!
```

## Connection Management

```python
# Context manager (auto-connect and disconnect)
with DatabaseManager("sqlite:///app.db") as db:
    users = db.execute("SELECT * FROM users")

# Manual connection management
db = DatabaseManager("postgresql://user:pass@localhost:5432/mydb")
db.connect()
try:
    users = db.execute("SELECT * FROM users")
finally:
    db.disconnect()

# Async close
await db.close()
```

## Backup and Restore

Two layers, same four calls on every backend:

```python
# Native: the database's own format. SQLite uses the online backup API;
# PostgreSQL runs pg_dump/pg_restore (custom format) and MySQL
# mysqldump/mysql, with the password passed through the environment and
# never on the command line; SQL Server runs BACKUP/RESTORE DATABASE,
# where the path is on the server.
db.backup("backups/app.dump")
db.restore("backups/app.dump", replace=True)   # drop existing objects first

# Portable: JSON lines plus a manifest, no external binary, loads into any
# backend whose tables exist. Bytes, datetimes, Decimal and UUID survive.
db.export_tables("backups/2024-05-06", tables=["users", "orders"])
db.import_tables("backups/2024-05-06", truncate=True)
```

`backup(tables=[...])` narrows a PostgreSQL or MySQL dump; SQLite and SQL
Server backups are whole-database and reject it. A missing client binary
raises `BackupToolNotFound` naming it. `import_tables` runs one transaction
per table and re-syncs PostgreSQL sequences so the next plain INSERT gets a
fresh key. Point-in-time recovery is a server-side concern (WAL archiving,
binlogs, log backups) and is not provided. Details, per-backend behaviour
and limits: [docs/BACKUP_RECOVERY.md](docs/BACKUP_RECOVERY.md).

## Async Backend

The sync `DatabaseManager` blocks: calling it from inside `async def` code
(FastAPI routes, SSE streams) stalls the whole event loop. For async
applications use `AsyncPostgresBackend` — async end to end, with a connection
pool and connection checking on checkout. It uses the same driver as the sync
path, so the `postgresql` extra covers both.

```python
from nexusql import AsyncPostgresBackend

db = AsyncPostgresBackend(
    "postgresql://user:pass@localhost:5432/mydb",
    min_pool_size=1,
    max_pool_size=10,
)
# False, not an exception, when the server is unreachable (after
# connect_timeout seconds, default 10), matching DatabaseManager.connect().
await db.connect()

# Named parameters work exactly like the sync manager: :name becomes
# psycopg's %(name)s on both paths, with :: casts never read as parameters.
result = await db.fetch_one(
    "SELECT id FROM users WHERE email = :email", {"email": "a@b.c"})
if result.success:
    print(result.data[0])

# Writes outside an explicit transaction are durable immediately. execute
# returns the rows (a list of dicts) for SELECT and INSERT .. RETURNING.
created = await db.execute(
    "INSERT INTO users (email) VALUES (:email) RETURNING id",
    {"email": "a@b.c"})

# Or let return_id=True find the primary key and return just its value,
# the same call the sync manager offers on every backend.
user_id = await db.execute(
    "INSERT INTO users (email) VALUES (:email)", {"email": "b@c.d"},
    return_id=True)

# Explicit transaction (commits on clean exit, rolls back on error)
async with db.transaction():
    await db.execute("INSERT INTO users (email) VALUES (:email)",
                     {"email": "x@y.z"})

# execute("BEGIN") raises RuntimeError, as on the sync manager: a BEGIN sent
# through execute would run on whichever pooled connection it borrowed and be
# committed as that statement returned. transaction() is the supported way.

print(await db.health_check())
await db.disconnect()

```

Result conventions mirror the sync manager: `fetch_one`/`execute`/
`execute_async` raise on query errors, since a loud failure beats silently
missing rows. `execute_script` returns `QueryResult` with `success=False` on
failure.

The package still imports (sync-only) when the extra is not installed;
`nexusql.PSYCOPG_AVAILABLE` tells you which world you are in.

## Sharding

`ShardedDatabaseManager` fronts several databases with the same calls as
`DatabaseManager`, so code written against one database can be pointed at
many. A router reads the shard key out of each statement's params:

```python
from nexusql import ShardedDatabaseManager, KeyRouter, HashRouter, RangeRouter

db = ShardedDatabaseManager(
    {"eu": "postgresql://.../eu", "us": "postgresql://.../us", "ap": "sqlite:///ap.db"},
    router=KeyRouter("region", {"eu": "eu", "us": "us", "ap": "ap"}),
)
# Alternatives: HashRouter("user_id", ["eu", "us", "ap"]) for a stable
# hash, RangeRouter("user_id", [(1000, "eu"), (2000, "us"), (None, "ap")])
# for key bands, or any callable (params) -> shard name.

with db:
    db.execute("CREATE TABLE IF NOT EXISTS users (...)", all_shards=True)

    # Routed by the shard key
    db.execute("INSERT INTO users (region, name) VALUES (:region, :name)",
               {"region": "eu", "name": "Ada"})

    # Unrouted read: fans out to every shard, rows concatenated in shard
    # order, then ordered and capped client-side
    rows = db.execute("SELECT region, name FROM users",
                      order_by=[("name", "asc")], limit=20)

    # Transactions span one shard, chosen by the first statement or shard=
    with db.transaction():
        db.execute("UPDATE users SET name = :n WHERE region = :region AND id = :id",
                   {"n": "Ada L.", "region": "eu", "id": 1})
```

`shard=` on any call overrides the router. A write that cannot be routed
raises rather than fanning out; `all_shards=True` opts it in. A `LIMIT` in
fanned-out SQL applies per shard, so use `limit=` for a global cap. There
are no cross-shard transactions and no distributed joins. `connect()`
returns `False`, with every shard disconnected again, if any shard fails.
See [docs/SHARDING.md](docs/SHARDING.md) for the routing order and the
consistency model.
## Change Data Capture

`db.cdc` installs AFTER INSERT / UPDATE / DELETE triggers on a table that
record every changed row in `<table>__changes`: the operation, the primary
key as text, and the old and new rows as JSON. It works on all four backends
with nothing but DDL rights; the trigger DDL is written per dialect rather
than translated.

```python
db.cdc.enable("orders")                       # pk defaults to "id"

db.execute("UPDATE orders SET status = :s WHERE id = :id",
           {"s": "shipped", "id": 42})

for change in db.cdc.changes("orders", since_id=0, limit=500):
    change["op"]        # 'I', 'U' or 'D'
    change["pk"]        # '42'
    change["old_row"]   # dict, or None for an insert
    change["new_row"]   # dict, or None for a delete

db.cdc.purge("orders", before_id=change["change_id"])
db.cdc.disable("orders")   # stop capturing, keep the history
db.cdc.drop("orders")      # remove the triggers and the history
```

`enable` is idempotent and is also how a schema change is picked up: the
triggers embed the column list, so run it again after an `ALTER TABLE`.
Triggers run synchronously inside each write, so they add latency to the
captured table; log-based capture (logical replication, binlog) is out of
scope. `AsyncPostgresBackend.cdc` offers the same calls as coroutines. See
[docs/CHANGE_DATA_CAPTURE.md](docs/CHANGE_DATA_CAPTURE.md) for the per-backend
DDL and the trade-offs.
## Full-Text Search

`db.fts` builds a full-text index, runs ranked searches and offers
completions with one API on every backend: a GIN `tsvector` index on
PostgreSQL, an FTS5 table kept current by triggers on SQLite, a `FULLTEXT`
index on MySQL, a full-text catalog and index on SQL Server.

```python
db.fts.create_index("articles", ["title", "body"])

# The table's rows plus a `rank` column, best first. The query string is
# user input and only ever reaches the database as a bound parameter.
hits = db.fts.search("articles", ["title", "body"], "postgres indexing", limit=10)

# Prefix completions; with pg_trgm installed on PostgreSQL, fuzzy ones too.
words = db.fts.suggest("articles", "title", "postg")

db.fts.drop_index("articles")
```

`AsyncPostgresBackend.fts` offers the same methods, awaited. Per-backend
behaviour and limitations are in [docs/FULL_TEXT_SEARCH.md](docs/FULL_TEXT_SEARCH.md).

## Checking Table Existence

```python
if db.table_exists("users"):
    print("Users table exists")
else:
    db.execute("""
        CREATE TABLE users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) NOT NULL
        )
    """)
```

## Testing Helpers

`nexusql.testing` gives application test suites data factories, table
snapshots and a dialect-aware `truncate`, plus pytest fixtures that ship as
a plugin (no conftest line needed once nexusql is installed):

```python
from nexusql.testing import Factory, Sequence, SubFactory, snapshot, truncate

accounts = Factory("accounts", name=Sequence("Account {n}"))
users = Factory("users", email=Sequence("user{n}@example.com"),
                active=True, account_id=SubFactory(accounts))

def test_rename(nexusql_tx):                 # the test runs in one transaction,
    row = users.create(nexusql_tx)           # rolled back at teardown
    snap = snapshot(nexusql_tx, ["accounts", "users"])

    rename_user(nexusql_tx, row["id"], "Alice")

    diff = snap.diff(nexusql_tx)
    assert set(diff["users"].changed) == {row["id"]}
    snap.restore(nexusql_tx)                 # truncate, re-insert, fix the key counters

truncate(db, ["accounts", "users"])          # TRUNCATE .. RESTART IDENTITY on
                                             # PostgreSQL, DELETE + sqlite_sequence
                                             # on SQLite, and so on
```

`nexusql_db` connects to `--nexusql-url`, else `$NEXUSQL_TEST_URL`, else a
SQLite file in `tmp_path`, and skips when a server is unreachable;
`nexusql_sqlite` is always a fresh SQLite file. Importing `nexusql` or
`nexusql.testing` does not import pytest. See [docs/TESTING.md](docs/TESTING.md).

## Advanced Features

### Running SQL Scripts

```python
# Execute multiple statements at once
script = """
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id),
    name VARCHAR(255) NOT NULL
);
"""

result = await db.execute_script(script)
if result.success:
    print("Script executed successfully")
```

### Custom Migration Paths

```python
# Run migrations from multiple directories
await db.initialize(
    apply_schema=True,
    app_migration_paths=[
        "migrations/core",
        "migrations/features",
        "migrations/plugins"
    ]
)
```

## Supported Data Types

| PostgreSQL (Canonical) | SQLite | MySQL | MSSQL |
|------------------------|--------|-------|-------|
| SERIAL | INTEGER AUTOINCREMENT | INT AUTO_INCREMENT | INT IDENTITY(1,1) |
| BOOLEAN | INTEGER | TINYINT(1) | BIT |
| VARCHAR(n) | TEXT | VARCHAR(n) | NVARCHAR(n) |
| TEXT | TEXT | TEXT | NVARCHAR(MAX) |
| JSONB | TEXT | JSON | NVARCHAR(MAX) |
| JSON | TEXT | JSON | NVARCHAR(MAX) |
| UUID | TEXT | CHAR(36) | UNIQUEIDENTIFIER |
| TIMESTAMP | TEXT | TIMESTAMP | DATETIME2 |

## Supported Functions

| PostgreSQL (Canonical) | SQLite | MySQL | MSSQL |
|------------------------|--------|-------|-------|
| NOW() | CURRENT_TIMESTAMP | NOW() | GETDATE() |
| gen_random_uuid() | hex(randomblob(16)) | UUID() | NEWID() |
| CURRENT_TIMESTAMP | CURRENT_TIMESTAMP | CURRENT_TIMESTAMP | GETDATE() |

## API Reference

### DatabaseManager

#### `__init__(database_url_or_config)`
Initialize with database URL string or ConnectionConfig object.

#### `connect() -> bool`
Connect to the database.

#### `disconnect()`
Disconnect from the database.

#### `async initialize(apply_schema=True, app_migration_paths=None) -> bool`
Initialize database and optionally apply migrations.

#### `execute(query, params=None, return_id=False) -> List[Dict] | Any`
Runs a statement and returns its rows (an empty list for writes). With
`return_id=True` the statement must be an INSERT and the generated key of
the new row is returned instead (None when no row was inserted). The
driver-specific part is handled per backend: SQLite `lastrowid`, PostgreSQL
`RETURNING <primary key>` (looked up in the catalog once per table), MySQL
`LAST_INSERT_ID()`, MSSQL `SCOPE_IDENTITY()` in the same batch. An INSERT
that already has a RETURNING (or MSSQL OUTPUT) clause returns the first
column of its first row. On PostgreSQL the table needs a single-column
primary key; otherwise write the RETURNING clause yourself.

```python
user_id = db.execute(
    "INSERT INTO users (email) VALUES (:email)", {"email": "a@b.c"},
    return_id=True)
```
Execute query with named parameters. Returns list of dicts for SELECT, empty list for INSERT/UPDATE/DELETE.

#### `fetch_one(query, params=None) -> Optional[Dict]`
Fetch single row with named parameters. `None` means the query succeeded and matched no rows; errors raise.

#### `async fetch_one_async(query, params=None) -> Optional[Dict]`
Same as `fetch_one`, run in a worker thread for server backends so it does not block the event loop.

#### `table_exists(table_name) -> bool`
Check if table exists.

#### `fts` (property)
Full-text search helpers: `fts.create_index(table, columns, name=None)`, `fts.drop_index(table, name=None)`, `fts.search(table, columns, query, limit=20, offset=0, name=None) -> List[Dict]` and `fts.suggest(table, column, term, limit=10, install=False) -> List[str]`. See [docs/FULL_TEXT_SEARCH.md](docs/FULL_TEXT_SEARCH.md).

#### `async execute_script(script) -> QueryResult`
Execute multiple SQL statements.

#### `backup(path, tables=None) -> str`
Native backup to `path` (SQLite online backup API, `pg_dump`, `mysqldump`, `BACKUP DATABASE`). See [docs/BACKUP_RECOVERY.md](docs/BACKUP_RECOVERY.md).

#### `restore(path, *, replace=False)`
Restore a `backup()` file. `replace` drops existing objects first (`pg_restore --clean --if-exists`, SQL Server `WITH REPLACE`).

#### `export_tables(path, tables=None) -> dict`
Portable JSON-lines export under the directory `path`; returns the manifest.

#### `import_tables(path, truncate=False, tables=None) -> dict`
Load an export with parameterised INSERTs, one transaction per table; returns rows inserted per table.

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=nexusql --cov-report=html

# Run specific test file
pytest tests/unit/test_database_manager.py -v

# Integration tests run against each backend whose URL is set and skip the rest
export TEST_POSTGRESQL_URL="postgresql://testuser:testpass@localhost:15432/nexusql_test"
pytest tests/integration
```

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new features
4. Ensure all tests pass
5. Submit a pull request

## License

MIT

## Credits

NexusQL was extracted from [ia_modules](https://github.com/matterwaveai/ia_modules) to provide a standalone, reusable database abstraction layer.
