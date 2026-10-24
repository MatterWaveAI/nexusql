# Change Data Capture

Status: implemented, exposed as `db.cdc` on `DatabaseManager` and
`backend.cdc` on `AsyncPostgresBackend`. Module: `nexusql/cdc.py`.

## What it does

`db.cdc.enable(table)` installs AFTER INSERT / UPDATE / DELETE triggers on a
table. Each changed row appends one row to `<table>__changes`:

| column | meaning |
|--------|---------|
| `change_id` | auto-increment, the paging cursor |
| `op` | `'I'`, `'U'` or `'D'` |
| `pk` | the row's primary-key value, as text (the new value on an update) |
| `old_row` | the row before the change as JSON text; NULL for an insert |
| `new_row` | the row after the change as JSON text; NULL for a delete |
| `changed_at` | the database's `CURRENT_TIMESTAMP` when the trigger fired |

`db.cdc.changes(table)` reads those rows back with `old_row` / `new_row`
decoded to dicts.

```python
db.cdc.enable("orders")                 # pk defaults to "id"

db.execute("UPDATE orders SET status = :s WHERE id = :id",
           {"s": "shipped", "id": 42})

for change in db.cdc.changes("orders", since_id=last_seen, limit=500):
    change["op"]        # 'U'
    change["pk"]        # '42'
    change["old_row"]   # {'id': 42, 'status': 'paid', ...}
    change["new_row"]   # {'id': 42, 'status': 'shipped', ...}
    last_seen = change["change_id"]

db.cdc.purge("orders", before_id=last_seen)   # drop what has been consumed
db.cdc.disable("orders")                      # stop capturing, keep history
db.cdc.drop("orders")                         # remove triggers and history
```

## API

| call | effect |
|------|--------|
| `enable(table, pk="id")` | create `<table>__changes` if missing, (re)install the triggers |
| `disable(table)` | drop the triggers (and the PostgreSQL function); keep the changes table |
| `drop(table)` | drop the triggers and the changes table |
| `is_enabled(table)` | True when all three triggers exist |
| `changes(table, since_id=0, limit=1000)` | rows with `change_id > since_id`, oldest first |
| `purge(table, before_id)` | delete rows with `change_id < before_id` |

`enable` is idempotent: running it again replaces the triggers and leaves the
changes table and its rows alone. That is also how a schema change is picked
up (see below). `enable` raises `ValueError` for a name that is not a plain
identifier, for a table that does not exist, and for a `pk` that is not one
of its columns; nothing is created in those cases.

`AsyncPostgresBackend.cdc` offers the same six operations as coroutines.

## How each backend does it

Trigger syntax differs far too much between the engines for the regex
translator in `manager.py`, so `nexusql/cdc.py` writes each dialect's DDL in
full and runs it without translation. The column list is read from the
catalog at `enable` time.

| | changes table | row serialisation | triggers |
|---|---|---|---|
| PostgreSQL | `BIGSERIAL`, `TEXT` | one plpgsql function using `row_to_json(OLD/NEW)::text`, branching on `TG_OP` | three `FOR EACH ROW EXECUTE FUNCTION` triggers (PostgreSQL 11+) |
| SQLite | `INTEGER PRIMARY KEY AUTOINCREMENT`, `TEXT` | `json_object('col', NEW."col", ...)` from `pragma_table_info` | three row triggers |
| MySQL | `BIGINT AUTO_INCREMENT`, `LONGTEXT` | `JSON_OBJECT('col', NEW.`col`, ...)` | three single-statement row triggers (no `DELIMITER` needed) |
| SQL Server | `BIGINT IDENTITY`, `NVARCHAR(MAX)` | `(SELECT n.[col], ... FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES)` | three statement triggers over `inserted` / `deleted` |

Only the PostgreSQL function reads the row dynamically; the other three bake
the column list into the trigger body.

Derived names are `<table>__changes`, `<table>__cdc_i`, `<table>__cdc_u`,
`<table>__cdc_d` and, on PostgreSQL, `<table>__cdc_fn()`. Table names are
limited to 54 characters so these fit PostgreSQL's 63-byte identifier limit.

Identifiers (table and pk) must match `[A-Za-z_][A-Za-z0-9_]*`. They are
quoted in each engine's own style and are the only user-supplied text that
is interpolated into SQL; `since_id`, `limit` and `before_id` are bound
parameters. Schema-qualified names are not accepted; the table is looked up
in the connection's default schema (`current_schema()`, `DATABASE()`,
`SCHEMA_NAME()`), and the name is matched exactly as given, so on
PostgreSQL pass it in the case the catalog stores it.

The DDL runs inside `db.transaction()`. On PostgreSQL and SQLite, whose DDL
is transactional, a failure part-way leaves nothing behind. MySQL commits
each DDL statement implicitly, so there a failure can leave the changes table
without triggers; running `enable` again completes it.

## Trade-offs and limits

**Synchronous triggers add write latency.** Every insert, update and delete
on a captured table also writes a row (and serialises the row to JSON) in the
same transaction. The change rows roll back with the write, which is what
makes them trustworthy, but the cost lands on the writer. Capture only the
tables whose history you need.

**Payloads are JSON text.** `old_row` / `new_row` are whatever the engine's
JSON serialiser produces, so types are flattened: timestamps become strings,
PostgreSQL arrays become JSON arrays, decimals become numbers, and SQLite's
`json_object()` refuses BLOB values. The changes table grows with every
write; `purge` what consumers have processed.

**No schema evolution handling.** The SQLite, MySQL and SQL Server triggers
embed the column list that existed when `enable` ran. After an `ALTER TABLE`,
run `enable(table)` again; the triggers are regenerated and the history kept.
Renaming or dropping a captured column without re-enabling makes the next
write fail inside the trigger on those three engines. PostgreSQL's
`row_to_json` follows the table, but re-running `enable` there is harmless.

**Set-based triggers on SQL Server.** The UPDATE trigger pairs `inserted`
and `deleted` by primary key. A statement that changes the key column itself
produces change rows with `old_row` NULL for the re-keyed rows. SQL Server
also refuses `OUTPUT` without `INTO` on a table that has triggers;
`execute(..., return_id=True)` is unaffected because it uses
`SCOPE_IDENTITY()`.

**`changed_at` is the server's clock in the server's notion of
`CURRENT_TIMESTAMP`**: UTC on SQLite, the session time zone on PostgreSQL
and MySQL, the server's local time on SQL Server. Order and page by
`change_id`, not by `changed_at`.

**Privileges.** `enable` needs rights to create tables and triggers (and
functions on PostgreSQL). MySQL additionally requires the `TRIGGER`
privilege, and with binary logging on and no `SUPER` right,
`log_bin_trust_function_creators` must be set.

**Log-based CDC is out of scope.** PostgreSQL logical replication, the MySQL
binlog and SQL Server's built-in CDC avoid the write-path cost and capture
schema changes, but each needs server-side configuration, a privileged
reader and a long-lived consumer process, and none of them exists on SQLite.
Triggers are what every supported engine can do with nothing but DDL rights.
If you reach the point where write latency or volume makes triggers the
bottleneck, that is the signal to move to the engine's log reader.
