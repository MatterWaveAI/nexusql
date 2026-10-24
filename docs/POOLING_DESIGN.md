# Connection Pooling Design

Status: implemented, released in 0.3.0 (first published as 0.2.0). This is the design record;
it describes the drivers as they were before the change (psycopg2, asyncpg), which 0.3.0
replaced with psycopg 3 throughout. The README is the current reference.

## Problem

`DatabaseManager` holds exactly one connection (`self._connection`) and one
transaction flag (`self._in_transaction`). Every concurrent caller serializes
through that single connection. `execute_async` thread-offloads server queries
so the event loop keeps running, but ten concurrent awaits still execute ten
queries one after another.

The existing `AsyncPostgresBackend` solves this with a real asyncpg pool, but
only for PostgreSQL, and as a second class with its own duplicated SQL
translation, parameter binding, and statement splitting.

Goal: pooling for every server provider, behind one interface, with SQLite
explicitly excluded.

## Scope

In scope: PostgreSQL, MySQL, MSSQL.

Out of scope: SQLite. `sqlite3` connections are thread-bound (using one from
another thread raises `ProgrammingError`), and the target is a local file with
no network round-trip. SQLite keeps the current single-connection path. This is
a permanent design decision, not a deferred task.

## Driver landscape

| Provider | Native pool | Decision |
|---|---|---|
| PostgreSQL | `psycopg2.pool.ThreadedConnectionPool` (verified present) | Wrap the native pool |
| MySQL | none in `pymysql` | Generic pool |
| MSSQL | none in `pyodbc`; ODBC driver pools opaquely below Python | Generic pool |
| SQLite | n/a | No pool |

## Architecture

### 1. The `ConnectionPool` interface

One abstraction, three implementations. Provider differences live in the
implementation, never in `DatabaseManager`.

```python
class ConnectionPool(ABC):
    @abstractmethod
    def acquire(self, timeout: float | None = None) -> Any: ...

    @abstractmethod
    def release(self, conn: Any, *, discard: bool = False) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def size(self) -> int: ...
```

`discard=True` means the connection is known-bad and must be destroyed rather
than returned to the idle set. This is how a dead socket leaves the pool.

Implementations:

- **`Psycopg2Pool`** — wraps `ThreadedConnectionPool`. `acquire` is `getconn`,
  `release` is `putconn(conn, close=discard)`. Battle-tested code we do not
  reimplement.
- **`GenericPool`** — one thread-safe implementation shared by MySQL and MSSQL.
  A `queue.LifoQueue` of idle connections, a factory callable that opens a new
  one, `max_size` cap, `acquire` timeout, and a liveness check on checkout.
  LIFO rather than FIFO so idle connections beyond steady-state demand age out
  instead of being kept warm pointlessly.
- **`NullPool`** — SQLite. `acquire` always returns the same connection,
  `release` is a no-op. Lets the manager use one code path without special-casing
  SQLite at every call site.

### 2. Liveness on checkout

A pooled connection can be dead before you use it (server restart, idle
timeout, failover). Every `acquire` validates before handing the connection
over:

- Postgres: `conn.closed` is a cheap attribute check; fall back to `SELECT 1`
  if the connection has been idle past a threshold.
- MySQL: `conn.ping(reconnect=True)` — pymysql has this built in.
- MSSQL: `SELECT 1` on a cursor.

A failed check discards the connection and opens a replacement. This subsumes
the reconnect-with-backoff logic from `AsyncPostgresBackend._run_with_retry`:
that code retried *after* a failure, this prevents most of those failures. Retry
is still kept for the case where a connection dies mid-statement.

### 3. Transaction scoping: thread-local pinning

This is the part that actually breaks the current design, and it is provider
independent.

With a pool, `self._in_transaction = True` is meaningless — *which* connection
is in a transaction? Prior art converges on one principle: a shared pool handle
and a transaction scope are different things.

- **SQLAlchemy** — `Engine` holds the pool, `Connection` is a checkout,
  transactions live on the `Connection`. Engine-level implicit execution was
  removed in 2.0 precisely because ownership was ambiguous.
- **Go `database/sql`** — `DB` is the pool, `Tx` is a separate type from
  `db.Begin()`. The type system forbids running transactional statements on
  the pool.
- **Django** — `django.db.connections` is a thread-local registry; each thread
  owns its connection and its transaction state.
- **HikariCP** — pure pool, transactions are entirely the caller's problem.

We take Django's model, because it preserves the existing call surface, and
add SQLAlchemy's explicit scope as the recommended forward path.

```python
class _State(threading.local):
    conn = None          # this thread's checked-out connection
    depth = 0            # transaction nesting depth
    pinned = False       # True while a transaction owns `conn`
```

Rules:

1. Outside a transaction, each operation acquires a connection, runs, releases.
2. `execute("BEGIN")` acquires a connection and **pins** it to the calling
   thread. Subsequent statements on that thread reuse it.
3. `COMMIT` / `ROLLBACK` unpin and release back to the pool.
4. Two threads can hold independent transactions on the same manager.
5. Single-threaded callers see behavior identical to today.

The preferred API becomes an explicit scope:

```python
with db.transaction():
    db.execute("INSERT ...")
    db.execute("UPDATE ...")
```

which pins for the block and commits or rolls back on exit. This becomes the
**only** way to open a transaction.

### The `execute("BEGIN")` string protocol is removed

What it was, since it looked like SQL but was not: `"BEGIN"` was intercepted by
`execute` and never sent to the database. It set `_in_transaction`, suppressing
auto-commit; `"COMMIT"` and `"ROLLBACK"` called `conn.commit()` /
`conn.rollback()` on a connection with `autocommit = False`. Genuine DBAPI
transactions, and uniform across providers — `BEGIN TRANSACTION` worked against
MySQL even though MySQL spells it `START TRANSACTION`.

It was introduced in `3bafad4` (2025-10-28) alongside `_in_transaction` itself,
untouched by the 16 commits since, and is depended on by 12 test call sites.

It is removed in 0.2.0 for three reasons:

1. Under pooling, a manager-wide flag is meaningless — it cannot say *which*
   pooled connection holds the transaction. The mechanism has to change
   regardless.
2. Normalization was fragile. `"begin;"` matched, but `"BEGIN /* comment */"`
   did not — it fell through to the auto-commit branch and silently committed,
   the opposite of what the caller asked for.
3. Savepoints were inconsistent. `SAVE TRANSACTION` and `ROLLBACK TRANSACTION
   <name>` passed through to the database as raw SQL rather than being
   intercepted, so some transaction statements were handled by the manager and
   others were not.

#### Removal must be guarded, not silent

`execute` must still recognize `BEGIN` / `COMMIT` / `ROLLBACK` /
`START TRANSACTION` and raise:

```python
raise RuntimeError(
    "execute('BEGIN') was removed in 0.2.0. Use `with db.transaction():` instead."
)
```

Without the guard, an external caller's `execute("BEGIN")` stops being
intercepted and is sent to the driver as raw SQL. On PostgreSQL that opens a
real transaction the manager does not know about: the manager believes it is in
auto-commit, so writes are never committed and the connection returns to the
pool holding locks. Silent data loss, surfacing far from its cause.

The guard is a few lines and converts that into an immediate, self-explanatory
error. It is not a deprecation shim — the protocol does not work, it only
fails clearly.

#### Migration

All 12 call sites convert mechanically:

```python
# before
db.execute("BEGIN")
db.execute("INSERT ...")
db.execute("COMMIT")

# after
with db.transaction():
    db.execute("INSERT ...")
```

`ROLLBACK` sites collapse entirely: the context manager rolls back on
exception and commits on clean exit.

### 4. What happens to `_connection`

`_connection` is read directly in ~38 test locations, including
`tests/conftest.py:125` (`master_db._connection.autocommit = True`). It is
semi-public whether or not it was meant to be.

It becomes a property returning the calling thread's current connection —
pinned if in a transaction, otherwise a connection checked out for the
duration. `conftest.py` and existing tests keep working unchanged.

A caller mutating `_connection.autocommit` outside a transaction now affects a
*pooled* connection that later returns to the pool. To stop that leaking,
`release` resets `autocommit` to the pool's default before the connection goes
back to the idle set.

### 5. Sizing and configuration

```python
DatabaseManager(url, pool_min=1, pool_max=10, pool_timeout=30.0)
```

Defaults chosen so a single-threaded caller behaves as today: `pool_min=1`
opens one connection eagerly, and nothing else is created until concurrent
demand appears. `pool_max=1` reproduces current semantics exactly, as an escape
hatch.

## Deduplication

Four helpers are currently copy-pasted between `manager.py` and
`async_backend.py`, each carrying a comment pointing at its twin. The B-001
parameter-prefix fix presently has two homes. Extract to `nexusql/_sql.py`:

- `_PARAM_TOKEN` — the `(?<!:):(\w+)` pattern
- SQL translation per target dialect (currently `_translate_sql` and
  `_translate_sql_for_postgres`)
- `_split_sql_statements`
- write-statement / RETURNING detection (`_is_write_statement`)

Both the sync manager and the async backend import from there. This is the
"extract shared pieces first" step that was skipped when the async path was
built; doing it now is a refactor of working code rather than a precondition,
but it is the same work and the copies are still verbatim.

## What happens to `AsyncPostgresBackend`

It stays, and it gets smaller. It keeps its asyncpg-native pool — genuinely
async end to end beats thread-offloading a sync pool, and that advantage does
not disappear when the sync manager gains pooling. What it loses is the
duplicated helpers, which move to `_sql.py`.

`async_migrations.py` is the genuinely redundant piece: a second migration
subsystem parallel to `migrations.py`. It is removed. Two capabilities live
only there and must be ported into the sync runner first:

- **`verify_checksums`** — `migrations.py` already stores a `checksum` column
  and writes it on every migration, but nothing ever reads it back.
  `AsyncMigrationRunner.verify_checksums` is the only code in the repo that
  compares stored against recomputed and raises `MigrationChecksumMismatch`.
  Roughly 30 lines, provider-agnostic. Port it.
- **Advisory locks** — `pg_advisory_lock` keyed on the migrations table, so two
  processes starting simultaneously cannot both run pending migrations.
  PostgreSQL-only by nature; ports as a Postgres branch that no-ops elsewhere.
  Only matters if migrations actually run concurrently across processes.

## Invariants that must survive

Both are recent fixes with tests; the pooling work must not regress them.

- **B-001** (`tests/unit/test_param_prefix_postgres.py`) — complete `:name`
  token matching, so `:id` does not corrupt `:id_org`, and `::` casts are never
  treated as parameters. Moves to `_sql.py` unchanged.
- **B-002** (`tests/integration/test_fetch_commit_durability.py`) —
  `INSERT ... RETURNING` through `fetch_one`/`fetch_all` must commit, or the
  row is returned and then lost. Under pooling this gets sharper: an
  uncommitted write on a connection returned to the pool is not just lost, it
  holds locks. `release` must assert the connection has no open transaction
  outside an explicit pinned scope.

## Sequencing

Each step ends green, so the work can stop at any point.

1. Extract `_sql.py`; point both `manager.py` and `async_backend.py` at it.
   No behavior change. Existing tests prove it.
2. Add the `ConnectionPool` interface plus `NullPool`. Wire `DatabaseManager`
   to use it with SQLite only. Still single-connection everywhere.
3. Add thread-local state and the `transaction()` scope, with `_connection` as
   a property. Remove the `execute("BEGIN")` interception, add the guard that
   raises, and convert the 12 call sites. Still `pool_max=1`, so no concurrency
   change yet. This is the risky step: the whole transaction model changes
   while behavior stays fixed.
4. Add `Psycopg2Pool`; enable pooling for PostgreSQL. First real concurrency.
5. Add `GenericPool`; wire MySQL and MSSQL.
6. Port `verify_checksums` into `migrations.py`; decide on advisory locks;
   delete `async_migrations.py`.
7. Update README, `pyproject.toml` extras, and version to 0.2.0.

Step 3 is where the risk concentrates. Steps 1 and 2 are safe; 4 and 5 are
additive per provider.

## Testing

- Concurrency: N threads issuing simultaneous queries actually overlap.
  Currently they cannot; this is the test that proves pooling works.
- Transaction isolation: two threads holding independent transactions on one
  manager do not observe each other's uncommitted rows.
- Pool exhaustion: `pool_max` reached blocks until `pool_timeout`, then raises
  a clear error rather than hanging.
- Connection death: kill a pooled connection server-side; the next `acquire`
  discards it and returns a working one.
- No leaks: after a run, `pool.size` returns to baseline; nothing leaks on the
  error path.
- Regression: B-001 and B-002 suites must stay green throughout.

## Known pre-existing failure

`tests/unit/test_database_manager.py::TestDatabaseMigrations::test_migration_tracking`
fails on `main` today, before any of this work — verified by stashing all
current changes and re-running. It expects a `nexusql/migrations` directory the
repo does not ship. Unrelated to pooling, but it means the suite is red at the
starting line and should be fixed or deleted separately.

## Open questions

1. Advisory locks — port to the sync runner, or drop? Only matters with
   concurrent multi-process migrations.
2. Should `pool_max=1` remain the default for one release, making pooling
   opt-in, given this is already a breaking version?
3. MSSQL sits on ODBC driver-level pooling we do not control. A Python pool
   above it is layered but still gives uniform semantics. Accept, or leave
   MSSQL unpooled?
