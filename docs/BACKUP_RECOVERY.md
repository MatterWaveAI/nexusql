# Backup and Recovery

Status: implemented. Logical backup and restore on every backend, in two
layers, behind four methods on `DatabaseManager`:

| Method | What it does | Needs |
|---|---|---|
| `backup(path, tables=None) -> str` | Native backup in the database's own format | Client binary on PATH (PostgreSQL, MySQL); a server-side path (SQL Server) |
| `restore(path, *, replace=False)` | Restore a `backup()` file | Same as above |
| `export_tables(path, tables=None) -> manifest` | Portable JSON-lines export of table rows | Nothing beyond the driver |
| `import_tables(path, truncate=False, tables=None) -> {table: rows}` | Load an export back with parameterised INSERTs | Nothing beyond the driver; the tables must exist |

Point-in-time recovery is out of scope; see the last section.

## Which layer to use

`backup()` is the faithful copy. It uses the database's own tooling, so it
carries everything that tooling does: schema, indexes, constraints,
sequences, views, and on SQL Server the whole database file set. It is the
right thing for disaster recovery of a single backend. Its formats are not
portable between backends, and the server backends need something outside
Python: the `pg_dump`/`pg_restore` or `mysqldump`/`mysql` binaries, or for
SQL Server a path the server process can write.

`export_tables()` is the portable copy. It needs nothing but the driver and
writes plain JSON, so the same export loads into any backend whose tables
have the same columns. It carries rows only: run your migrations on the
target first. It is the right thing for moving data between backends,
seeding a test database, or taking a snapshot where no client tools are
installed.

## Native backup per backend

### SQLite

`backup()` uses `sqlite3.Connection.backup`, SQLite's online backup API.
The copy is page-consistent even if the manager keeps writing during the
backup, which a plain file copy of a database with a hot journal or WAL
file would not be. The target is a complete, independent SQLite database
file.

`restore()` opens the backup file and backs it up *into* the live
connection. The alternative, replacing the database file on disk, is only
safe when nothing has it open, and the manager itself does. Copying through
the backup API keeps every handle valid and lets SQLite serialise against
other readers. It overwrites the whole database, as any restore does, and
refuses to run inside an open `transaction()`.

`tables` is rejected for SQLite: the backup API copies the whole file. Use
`export_tables()` for a subset.

### PostgreSQL

`backup()` runs `pg_dump --format=custom` and `restore()` runs
`pg_restore`. Host, port, user and database from the URL are passed as
`--host`, `--port`, `--username` and `--dbname`. The password is never on
the command line: it is placed in `PGPASSWORD` in the child's environment,
and `--no-password` stops the tools from prompting. The rest of the
environment is inherited, so `PGSSLMODE` and the other libpq variables keep
working.

`tables=[...]` becomes one `--table=` flag per name, including
schema-qualified names. `replace=True` adds `--clean --if-exists`, dropping
each object before recreating it; without it `pg_restore` fails on objects
that already exist, which is the safer default for a database you did not
mean to overwrite.

If the binary is not on PATH, `BackupToolNotFound` is raised naming it. A
non-zero exit raises `BackupError` with the tool's stderr.

### MySQL

`backup()` runs `mysqldump --single-transaction --protocol=TCP` with
`--host`, `--port`, `--user` and `--result-file`, followed by the database
and any `tables`. `--single-transaction` takes a consistent InnoDB snapshot
without locking the tables. `restore()` pipes the dump into `mysql` on
stdin. The password goes in `MYSQL_PWD` in the environment, never in the
argument vector.

`mysqldump` output carries `DROP TABLE IF EXISTS` for every table it
contains, so a MySQL restore always replaces the dumped tables; `replace`
has no further effect.

### SQL Server

`backup()` runs `BACKUP DATABASE [db] TO DISK = ?` and `restore()` runs
`RESTORE DATABASE [db] FROM DISK = ? WITH RECOVERY` (adding `, REPLACE`
when `replace=True`). Both are T-SQL executed by the server, so **the path
is a path on the server**, not on the machine running Python. The database
name is the one in the URL; both statements run with autocommit on because
SQL Server refuses to run them inside a transaction.

`RESTORE` needs exclusive access to the database. The manager closes its
own pool for the duration, issues the restore from a connection to
`master`, and reconnects afterwards. Other clients' sessions are left
alone: if they are connected the restore fails with "database is in use",
and taking the database to `SINGLE_USER WITH ROLLBACK IMMEDIATE` is an
operator's decision the library does not make for you. `replace=False`
(the default) lets SQL Server refuse to overwrite a database whose backup
set does not match; `replace=True` is `WITH REPLACE`.

## Portable export and import

```python
manifest = db.export_tables("backups/2024-05-06", tables=["users", "orders"])
# later, on any backend whose tables exist:
loaded = db.import_tables("backups/2024-05-06", truncate=True)
```

`export_tables` writes a directory:

```
backups/2024-05-06/
    manifest.json
    users.jsonl
    orders.jsonl
```

`manifest.json` records the format and its version, the nexusql version
that wrote it, the dialect and current schema it was read from, when, and
for each table its name, file, column list and row count. Each `.jsonl`
line is a JSON array in manifest column order. Values JSON cannot carry are
written as one-key objects: `{"$bytes": "<base64>"}`, `{"$datetime":
"<ISO 8601>"}`, `{"$date": ...}`, `{"$time": ...}`, `{"$decimal": "12.50"}`
and `{"$uuid": "..."}`. The decoder turns them back into `bytes`,
`datetime` (with its offset if it had one), `date`, `time`, `Decimal` and
`UUID`.

With `tables=None` the tables are discovered from the dialect's catalog:
`sqlite_master` (excluding SQLite's own `sqlite_*` tables, which the engine
rebuilds) or `information_schema.tables` filtered to base tables in the
current schema (`current_schema()`, `DATABASE()`, `SCHEMA_NAME()`).

`import_tables` loads each table in its own transaction with parameterised
INSERTs, batched through `executemany`. A failure leaves the tables before
it loaded and the failing one untouched. `truncate=True` issues `DELETE
FROM` inside that same transaction (not `TRUNCATE`, which is not
transactional everywhere), so a failed import keeps the old rows. Imported
rows keep their keys, so appending onto rows that share them fails with a
uniqueness error; `truncate=True` is the usual choice.

Two per-backend details are handled so that a plain INSERT after an import
still gets a fresh key. On PostgreSQL the sequence behind every `SERIAL`
or identity column is set to the imported maximum with `setval`, since
explicit key values do not advance it. On SQL Server `SET IDENTITY_INSERT
ON` is switched on for a table with an identity column and off again
afterwards, inside the same transaction. MySQL and SQLite advance their
counters on explicit inserts without help.

Limits worth knowing:

- The export is read table by table, each with its own statement, so it is
  not one snapshot across tables. `backup()` is the consistent copy.
- Each table is read with one `SELECT *` and held in memory while its file
  is written, so the largest table bounds memory use.
- On PostgreSQL, `jsonb` values come back as dicts and lists and are
  re-wrapped as `jsonb` on import. PostgreSQL array columns also arrive as
  lists and would be written back as JSON; the portable layer does not
  support them.
- Values written by one backend and read by another are only as portable
  as the column types: a SQLite `TIMESTAMP` is text, so a SQLite export
  loads its timestamps into PostgreSQL as strings the server must parse.

## Errors

- `BackupToolNotFound` (a `BackupError`): a client binary the native path
  needs is not on PATH. The message names the binary.
- `BackupError`: the tool exited non-zero (its stderr is in the message), a
  manifest is not a nexusql export or is newer than this version reads, a
  row disagrees with the manifest's column count, or a restore was
  attempted inside a transaction.
- `FileNotFoundError`: the backup file or export directory is missing.
- `ValueError`: `tables` was passed to a whole-database backend.

## Point-in-time recovery is out of scope

Every backup described here is a logical snapshot: the state at one moment.
Rolling forward to a moment *between* snapshots, or to just before a bad
statement, needs the server's own log stream and is configured on the
server, not from a client library:

- PostgreSQL: continuous WAL archiving (`archive_mode`, `archive_command`)
  with a base backup from `pg_basebackup`, then `recovery_target_time`.
- MySQL: binary logging (`log_bin`) and replay with `mysqlbinlog
  --stop-datetime`.
- SQL Server: the FULL recovery model with `BACKUP LOG` and `RESTORE ...
  WITH STOPAT`.
- SQLite has no log stream to replay; schedule `backup()` as often as the
  recovery point objective demands.

Take the backups from here, keep the log stream on the server.
