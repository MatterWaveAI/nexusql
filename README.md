# NexusQL

Multi-database abstraction layer with unified API for PostgreSQL, MySQL, SQLite, and MSSQL.

## Features

- ✅ **Unified API** - Same code works across 4 databases (PostgreSQL, MySQL, SQLite, MSSQL)
- ✅ **Named Parameters** - Safe from SQL injection with `:param` syntax
- ✅ **Auto-migrations** - Versioned schema management with automatic translation
- ✅ **SQL Translation** - Write PostgreSQL canonical syntax, automatically translates to all databases
- ✅ **Type Safety** - Full typing support with Python 3.10+
- ✅ **Async Ready** - Async/await compatible API
- ✅ **Multi-Tenancy** - `tenant()` + `enable_row_security()`: real row-level security on PostgreSQL, scoped views elsewhere
- ✅ **Streaming Results** - Walk large result sets in fixed-size chunks with `stream()`
- ✅ **Encryption & Compliance** - Application-side field encryption, key rotation, blind indexes, masking and GDPR erasure
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

# Application-side field encryption (AES-256-GCM via cryptography)
pip install nexusql[crypto]

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

### Nested transactions and savepoints

A `transaction()` block opened inside another becomes a savepoint. If the
inner block raises, only its own statements are undone; the exception then
propagates to the outer block, which decides whether to carry on or let the
whole transaction roll back. The outermost block commits and rolls back
exactly as before.

```python
with db.transaction():
    db.execute("INSERT INTO orders (id, total) VALUES (:id, :total)",
               {"id": 7, "total": 99})
    try:
        with db.transaction():
            db.execute("INSERT INTO audit (order_id) VALUES (:id)", {"id": 7})
            db.execute("INSERT INTO audit (order_id) VALUES (:id)", {"id": 7})
    except IntegrityError:
        pass          # the audit rows are gone; the order survives
    db.execute("UPDATE orders SET status = 'open' WHERE id = :id", {"id": 7})
# commit: orders has the row, audit has nothing
```

Savepoints nest to any depth; each level is named after its depth
(`sp_2`, `sp_3`, ...). On SQLite, PostgreSQL and MySQL the manager issues
`SAVEPOINT` / `RELEASE SAVEPOINT` / `ROLLBACK TO SAVEPOINT`; on SQL Server
`SAVE TRANSACTION` / `ROLLBACK TRANSACTION name`, which has no release step.
Two dialect details are handled for you: SQLite would otherwise commit on
release when the nested block is the first statement of the outer block, so
the manager opens the outer transaction explicitly first; and SQL Server
refuses `SAVE TRANSACTION` with no transaction active, so the manager starts
the implicit one when the savepoint is the first statement.

In earlier releases a nested block simply joined the outer transaction, and
a failed statement inside it rolled back everything. That behaviour is still
available with `nested="join"`:

```python
with db.transaction():
    with db.transaction(nested="join"):   # no savepoint; one transaction
        ...
```

Inside a savepoint a failed statement no longer rolls back the connection;
the savepoint is rolled back instead and the outer block's work is kept. In
the outermost block (and in `nested="join"` blocks) a failed statement rolls
the connection back, as it always has.

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

## Row-Level Security

Multi-tenant tables keep every tenant's rows together; the tenant filter
belongs in the database session, not in every query. `enable_row_security`
scopes a table to the tenant set by `tenant()`, and `scoped()` names what to
query:

```python
db.enable_row_security("orders")             # once; tenant_column="tenant_id"

with db.tenant(request.tenant_id):           # per request, one transaction
    rows = db.execute(f"SELECT * FROM {db.scoped('orders')} WHERE status = :s",
                      {"s": "open"})
```

What that enforces differs by backend, and the difference matters:

- **PostgreSQL**: `ENABLE` + `FORCE ROW LEVEL SECURITY` and a policy for
  SELECT/INSERT/UPDATE/DELETE on `current_setting('app.tenant_id')`. The
  server filters the table for every client; a cross-tenant INSERT is
  refused. Superusers always bypass row-level security and table owners
  bypass it unless FORCE is on (it is), so connect the application as an
  ordinary role. `scoped()` returns the table itself.
- **MySQL, SQL Server, SQLite**: a `<table>_scoped` view filtering on the
  session value (`@app_tenant_id` via a stored function,
  `SESSION_CONTEXT(N'tenant_id')`, `app_tenant_id()`). Queries through the
  view are tenant-scoped; queries against the table are not. A convenience
  filter, not a security boundary.

`tenant()` opens a transaction if none is open, so the value lives on the one
pinned connection and is gone before it returns to the pool; nested blocks
restore the outer tenant. With no tenant set, the policy and views match
nothing. `AsyncPostgresBackend` offers the same calls. The full security
boundary per backend is in [docs/ROW_LEVEL_SECURITY.md](docs/ROW_LEVEL_SECURITY.md).
## Streaming Results

`execute()` returns every row as a list. For a result too large to hold,
`stream()` yields one dict at a time, fetching `chunk_size` rows per
round-trip, so memory is bounded by the chunk rather than the table:

```python
for row in db.stream("SELECT * FROM events WHERE day = :day ORDER BY id",
                     {"day": day}, chunk_size=1000):
    export(row)

# Or take each fetched batch as a list
for batch in db.stream_chunks("SELECT id, payload FROM events", chunk_size=500):
    index(batch)
```

The stream holds one connection from the first row until it ends, and hands
it back on every exit: when the rows run out, when you `break` out of the
loop (closing the generator), or when the query fails, in which case the
connection is rolled back first. Nothing runs until the first iteration.

On PostgreSQL this is a server-side (named) cursor, so the server sends rows
per fetch instead of the driver buffering the whole result; MySQL uses
pymysql's unbuffered cursor; SQLite and SQL Server use `fetchmany` on a plain
cursor. Inside `transaction()` the stream uses the pinned connection and
commits nothing; finish the stream before the block ends, since a stream
must not outlive its transaction.

`AsyncPostgresBackend.stream()` is the async form. An early exit releases
the connection when the generator is closed, so iterate inside
`contextlib.aclosing()` when you may `break`:

```python
async with contextlib.aclosing(db.stream("SELECT * FROM events")) as rows:
    async for row in rows:
        ...
```

[docs/STREAMING.md](docs/STREAMING.md) covers the per-backend details and
the memory profile.

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

## Query Builder

For queries assembled from optional pieces (search filters, listings with a
client-chosen sort order) `Q` builds the SQL so no value is ever concatenated
into it. It emits the same canonical PostgreSQL SQL with `:name` parameters
you would write by hand, so `execute()` translates it per backend as usual
and the builder itself knows nothing about dialects:

```python
from nexusql import Q

q = (Q.select("users").columns("id", "email")
     .where((Q.col("age") >= Q.p("min_age", 18)) & Q.col("email").like("%@example.com"))
     .order_by("-created_at").limit(10).offset(20))

q.build()
# ('SELECT id, email FROM users WHERE age >= :min_age AND email LIKE :p1 '
#  'ORDER BY created_at DESC LIMIT 10 OFFSET 20',
#  {'min_age': 18, 'p1': '%@example.com'})

rows = db.run(q)                 # same as db.execute(*q.build())
row = db.run_one(q)              # same as db.fetch_one(*q.build())

user_id = Q.insert("users").values(email="a@b.c", name="A").execute(db, return_id=True)
db.run(Q.update("users").set(name="B").where(Q.col("id") == user_id))
db.run(Q.delete("users").where(Q.col("id").in_([user_id])))
```

Python values always become parameters (`:p1`, `:p2`, ... or the name given
to `Q.p()`); identifiers must match `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$`
and anything else raises `QueryError` before the database is touched.
`Q.raw()` is the only escape hatch and is for SQL you typed, never for
input. Statements are immutable, so a base query can be shared and refined.
Joins, `group_by`/`having`, `distinct` and the full set of predicates are
described in [docs/QUERY_BUILDER.md](docs/QUERY_BUILDER.md).

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

# Explicit transaction (commits on clean exit, rolls back on error). A block
# nested inside another is a savepoint, as on the sync manager; pass
# nested="join" to run inside the outer transaction with no savepoint.
async with db.transaction():
    await db.execute("INSERT INTO users (email) VALUES (:email)",
                     {"email": "x@y.z"})
    try:
        async with db.transaction():
            await db.execute("INSERT INTO users (email) VALUES (:email)",
                             {"email": "x@y.z"})   # duplicate
    except psycopg.errors.UniqueViolation:
        pass                                       # first insert survives

# execute("BEGIN") raises RuntimeError, as on the sync manager: a BEGIN sent
# through execute would run on whichever pooled connection it borrowed and be
# committed as that statement returned. transaction() is the supported way.

# Row-level security, same shape as the sync manager (see docs/ROW_LEVEL_SECURITY.md)
await db.enable_row_security("orders")
async with db.tenant(tenant_id):
    rows = await db.execute(f"SELECT * FROM {db.scoped('orders')}")

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
## Encryption and Compliance

`nexusql.crypto` is an explicit toolkit for protecting personal data in the
application, so the database only ever sees ciphertext. Nothing is applied
automatically: you choose the columns, encrypt before writing and decrypt
after reading. Field encryption needs the `crypto` extra; the rest is
standard library.

```python
from nexusql import FieldEncryptor, KeyRing, blind_index, mask

enc = FieldEncryptor(os.environ["FIELD_KEY"], key_id="k1")   # AES-256-GCM
db.execute(
    "INSERT INTO users (email, email_idx) VALUES (:email, :idx)",
    {"email": enc.encrypt(email), "idx": blind_index(email, os.environ["INDEX_KEY"])},
)
row = db.fetch_one("SELECT email FROM users WHERE email_idx = :idx",
                   {"idx": blind_index(email, os.environ["INDEX_KEY"])})
print(enc.decrypt(row["email"]))
logger.info("signup %s", mask(email, "email"))               # j***@example.com
```

Tokens look like `v1:k1:<base64url>` so the key id travels with the value;
a `KeyRing` decrypts with whichever key a token names and `rotate()`
re-encrypts under the active one. Right-to-erasure requests blank columns
in place without deleting the row:

```python
removed = db.erase("users", "id = :id", {"id": 42},
                   columns=["email", "phone"], tombstone="[erased]")
```

Threat model, blind-index leakage and the rotation procedure are in
[docs/ENCRYPTION_COMPLIANCE.md](docs/ENCRYPTION_COMPLIANCE.md).
## Query Performance

Three opt-in tools, all off by default so a manager built without them is
unchanged. Full details in [docs/QUERY_PERFORMANCE.md](docs/QUERY_PERFORMANCE.md).

```python
from nexusql import DatabaseManager, QueryTiming

db = DatabaseManager(
    "postgresql://user:pass@localhost/app",
    slow_query_threshold_ms=200,       # WARNING via the nexusql logger at/over 200 ms
    on_slow_query=lambda t: print(t),  # QueryTiming(sql, params, duration_ms, backend, rows)
    log_params=False,                  # keep parameter values (often PII) out of the log
    collect_stats=True,                # aggregate count / total_ms / max_ms per statement
)
db.connect()

# The backend's own plan columns plus a normalised "plan" text column:
# EXPLAIN on PostgreSQL/MySQL, EXPLAIN QUERY PLAN on SQLite, SHOWPLAN_ALL on
# SQL Server. analyze=True runs the statement, so only SELECTs are accepted.
for row in db.explain("SELECT * FROM orders WHERE customer_id = :cid", {"cid": 7}):
    print(row["plan"])

for stat in db.query_stats():        # slowest total first
    print(stat.count, f"{stat.avg_ms:.1f} ms", stat.sql)
db.reset_query_stats()
```

`AsyncPostgresBackend` takes the same four options and offers `await
db.explain(...)`, `db.query_stats()` and `db.reset_query_stats()`.

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
## Schema Introspection and Validation

`introspect` reports a table's columns and indexes with each column's raw
dialect type and a normalised `kind` (integer, bigint, float, decimal, text,
boolean, timestamp, date, json, uuid, bytes, other). `validate_schema`
compares the live schema with a spec and `check_params` checks a params
dict against the table before a query runs:

```python
from nexusql import TableSpec, ColumnSpec, IndexSpec, SchemaValidationError

info = db.introspect("users")
info.column("email").kind        # "text" on every backend
info.primary_key                 # ["id"]
db.list_tables()                 # base tables in the current schema

report = db.validate_schema([
    TableSpec("users", columns=[
        ColumnSpec("id", "SERIAL", primary_key=True),
        ColumnSpec("email", "VARCHAR(255)", nullable=False),
        ColumnSpec("active", "BOOLEAN"),
        ColumnSpec("profile", "JSONB"),
    ], indexes=[IndexSpec(("email",), unique=True)]),
])
if not report.ok:
    print(report)
    # schema validation failed with 2 errors (1 table checked):
    #   - users.profile: missing column
    #   - users: missing unique index on (email)

try:
    db.check_params("users", {"email": None, "nickname": "x"})
except SchemaValidationError as exc:
    exc.keys   # ["email", "nickname"]
```

Specs are written against the canonical PostgreSQL types and validate on
every backend, because kinds are compared through the translator's
equivalences: the `INTEGER` that `BOOLEAN` becomes on SQLite, the
`NVARCHAR(MAX)` that `JSONB` becomes on SQL Server and the `CHAR(36)` that
`UUID` becomes on MySQL all match. Indexes match on their columns, not their
dialect-assigned names. `check_params` caches introspection per table;
`invalidate_schema_cache()` drops it after a migration. The async backend
offers `introspect`, `list_tables` and `validate_schema` for PostgreSQL. See
[docs/SCHEMA_VALIDATION.md](docs/SCHEMA_VALIDATION.md).

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

#### `__init__(database_url_or_config, ..., slow_query_threshold_ms=None, on_slow_query=None, log_params=False, collect_stats=False)`
Initialize with database URL string or ConnectionConfig object. The last
four options enable the [query performance tools](#query-performance).

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

#### `transaction(nested="savepoint")`
Context manager that commits on clean exit and rolls back if the block
raises. A block nested inside another becomes a savepoint: its own
statements are undone if it raises, the outer block's survive, and the
exception propagates. `nested="join"` runs the inner block inside the outer
transaction with no savepoint, as earlier releases did. See
[Transactions](#transactions).

#### `fetch_one(query, params=None) -> Optional[Dict]`
Fetch single row with named parameters. `None` means the query succeeded and matched no rows; errors raise.

#### `async fetch_one_async(query, params=None) -> Optional[Dict]`
Same as `fetch_one`, run in a worker thread for server backends so it does not block the event loop.

#### `run(query, return_id=False) -> List[Dict] | Any`
`execute()` for a query-builder statement: `db.run(Q.select("users"))`.

#### `run_one(query) -> Optional[Dict]`
`fetch_one()` for a query-builder statement.
#### `stream(query, params=None, chunk_size=1000) -> Iterator[Dict]`
Yields rows one at a time, fetching `chunk_size` per round-trip (a
server-side cursor on PostgreSQL). Holds one connection until the generator
is exhausted, closed, or fails; see [Streaming Results](#streaming-results).

#### `stream_chunks(query, params=None, chunk_size=1000) -> Iterator[List[Dict]]`
Same as `stream`, yielding each fetched chunk as a list.
#### `explain(query, params=None, analyze=False) -> List[Dict]`
The backend's execution plan, one row per plan line, each with the backend's
own columns plus a normalised `plan` text column. `analyze=True` executes the
statement to measure it and is accepted only for SELECT statements.

#### `query_stats() -> List[QueryStat]`
Per-statement `count`, `total_ms`, `max_ms` (and `avg_ms`), keyed by the SQL
with whitespace collapsed. Requires `collect_stats=True`.

#### `reset_query_stats()`
Clear the aggregates.

#### `table_exists(table_name) -> bool`
Check if table exists.

#### `fts` (property)
Full-text search helpers: `fts.create_index(table, columns, name=None)`, `fts.drop_index(table, name=None)`, `fts.search(table, columns, query, limit=20, offset=0, name=None) -> List[Dict]` and `fts.suggest(table, column, term, limit=10, install=False) -> List[str]`. See [docs/FULL_TEXT_SEARCH.md](docs/FULL_TEXT_SEARCH.md).
#### `list_tables() -> List[str]`
Base tables in the current schema.

#### `introspect(table) -> TableInfo`
Columns (raw type, normalised kind, nullable, default, primary key, auto-increment) and indexes. Raises `TableNotFoundError` for an absent table.

#### `validate_schema(expected, strict=False) -> ValidationReport`
Compare the live schema with a list of `TableSpec` (or dicts). `report.ok`, `report.errors`, `str(report)`.

#### `check_params(table, params) -> None`
Raise `SchemaValidationError` naming the keys that do not fit the table's columns. Cached per table; `invalidate_schema_cache(table=None)` clears it.
#### `tenant(tenant_id)` (context manager)
Set the tenant for one transaction (opened if none is open). PostgreSQL
`set_config('app.tenant_id', .., true)`, MySQL `@app_tenant_id`, SQL Server
`SESSION_CONTEXT`, SQLite `app_tenant_id()`. `current_tenant` reads it back.

#### `enable_row_security(table, tenant_column="tenant_id")` / `disable_row_security(table)`
PostgreSQL: force row-level security with a policy on `tenant_column`.
Elsewhere: create a `<table>_scoped` view. See [docs/ROW_LEVEL_SECURITY.md](docs/ROW_LEVEL_SECURITY.md).

#### `scoped(table) -> str`
The name to query: `table` on PostgreSQL, `<table>_scoped` elsewhere.
#### `erase(table, where, params=None, columns=(), tombstone=None) -> int`
Set the named columns to NULL (or `tombstone`) for the rows `where` selects,
inside a transaction, and return the row count. Identifiers are validated and
quoted; `where` is your SQL with `:name` placeholders. An empty WHERE is
refused. See [docs/ENCRYPTION_COMPLIANCE.md](docs/ENCRYPTION_COMPLIANCE.md).

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
