# Migrations

NexusQL applies `.sql` files from a directory in filename order, records each
one in a migrations table, and refuses to re-run a file whose contents have
changed since it was applied. This page covers how that works and how to undo
a migration.

## Files and versions

Every `*.sql` file in the migration directory is a migration, except files
ending in `.down.sql`. The **version** of a migration is its filename without
the `.sql` extension, so `V001__create_users.sql` is recorded as
`V001__create_users` and `003_add_index.sql` as `003_add_index`. Files run
sorted by that stem, so zero-pad numbers.

Migrations are written in PostgreSQL syntax and translated for the connected
backend (see the Supported Data Types table in the README). Do not put
`BEGIN`/`COMMIT` in a migration; the runner manages transactions.

## Running

```python
from pathlib import Path
from nexusql import DatabaseManager, MigrationRunner

db = DatabaseManager("postgresql://...")
db.connect()

runner = MigrationRunner(db, Path("migrations"))
await runner.run_pending_migrations()
```

`DatabaseManager.initialize(apply_schema=True, app_migration_paths=[...])`
does the same for each listed directory.

```python
await runner.applied()   # list[MigrationRecord], oldest first
await runner.pending()   # list[Path] in the order they would run
```

Each `MigrationRecord` carries `version`, `filename`, `description`,
`applied_at`, `checksum` and `down_checksum`.

### The migrations table

Applied migrations are recorded in `ia_migrations` by default. Pass
`migrations_table="..."` to the runner to use a different table, for example
to keep several applications' migrations apart in one database. Each row
stores the version, filename, `migration_type` (`"app"` unless you pass
another), the SHA-256 of the up script and, when there is one, the SHA-256 of
the down script. A migrations table created before `down_checksum` existed
gains the column the next time a runner touches it; its older rows keep a
NULL there.

## Down scripts

A migration can be undone if it has a down script. There are two ways to
provide one.

**A sibling file**, named after the migration with `.down.sql` in place of
`.sql`:

```
migrations/
  001_users.sql
  001_users.down.sql
```

**A `-- down` section** inside the migration. Everything after a line
consisting of exactly `-- down` (case-insensitive, surrounding whitespace
allowed) is the down script and does not run on the way up:

```sql
-- 002_posts.sql
CREATE TABLE posts (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL
);

-- down
DROP TABLE posts;
```

When both exist for one migration the sibling file is used and a warning is
logged, so the stray section is noticed rather than silently ignored.

A down script is written in the same PostgreSQL syntax as the up script and
may hold several statements. Order them to reverse the up script: drop the
table that references another before the one it references.

## Rolling back

```python
await runner.rollback()                 # the newest applied migration
await runner.rollback(steps=2)          # the two newest, newest first
await runner.rollback_to("001_users")   # everything applied after 001_users
```

`rollback_to` accepts the recorded version or an unambiguous numeric prefix of
it (`"001"` for `001_users`, `"V002"` for `V002__posts`). The named migration
itself stays applied. An unknown or ambiguous version raises
`MigrationRollbackError` without changing anything.

Both methods return the list of versions that were rolled back. Migrations
roll back newest first, and only migrations of the runner's `migration_type`
are considered.

### Planning first

```python
for step in await runner.plan_rollback(steps=2):
    print(step.version, step.down_source, step.down_path, step.down_changed)
```

`plan_rollback(steps=None, to=None)` returns the `RollbackStep`s a rollback
would run, in order, without running them. With neither argument it plans one
step. Each step reports:

| Field | Meaning |
|-------|---------|
| `version`, `filename` | the migration as recorded |
| `down_source` | `"file"`, `"marker"`, or `None` when there is no down script |
| `down_path` | the file the down script will be read from |
| `recorded_down_checksum` | SHA-256 of the down script when the migration was applied (`None` for older rows) |
| `current_down_checksum` | SHA-256 of the down script on disk now |
| `has_down`, `down_changed` | convenience properties over the above |

`rollback()` and `rollback_to()` call `plan_rollback()` themselves, so what
the plan shows is exactly what runs.

### What is refused

If any migration in the plan has no down script, `plan_rollback` raises
`MigrationRollbackError` naming it, and nothing runs. A two-step rollback
where only the newest migration has a down script therefore undoes neither,
rather than undoing one and stopping. Add a `.down.sql` file for the
migration, which works even after the fact, and run again.

A down script that fails raises `MigrationRollbackError` for that step.
Migrations already rolled back in the same call stay rolled back; the failing
one and anything older remain applied.

### Checksums

The `checksum` column covers the **up script only**: for a file with a
`-- down` section, the bytes before the marker line; for any other file, the
whole file, so rows recorded before down scripts existed still verify.
Editing an applied up script is a `MigrationChecksumMismatch` at the next
`run_pending_migrations()`, as before.

The `down_checksum` column covers the down script. Editing a down script does
not block anything: the database already reflects the up script, and a down
script is often written or corrected later. At rollback time a down script
that no longer matches its recorded checksum is logged as a warning and the
current one runs. Rows with a NULL `down_checksum` are never reported as
changed.

## Transactions and backends

Each step runs its down script and then deletes the migration's row inside
one `DatabaseManager.transaction()`, mirroring how the up path runs the
script and then inserts the row. How atomic that is depends on the backend:

- **PostgreSQL**: fully atomic. DDL is transactional, so a down script that
  fails on its second statement leaves neither the first statement's effect
  nor a removed row behind.
- **SQL Server**: DDL is transactional, so the same is expected to hold.
- **MySQL**: DDL statements commit implicitly. A failed down script leaves
  its earlier statements applied and the row in place; fix the script and
  roll back again.
- **SQLite**: the script runs through `executescript()`, which commits each
  statement as it goes, exactly as up migrations do. A failing down script
  leaves its earlier statements applied and the row in place, so the
  migration is still reported as applied and can be rolled back again once
  the script is fixed.

In every case a rollback that is refused at planning time changes nothing.
