# Testing with NexusQL

`nexusql.testing` is for the test suites of applications built on NexusQL.
It has three parts: factories that describe a table's row shape once,
snapshots that capture tables and put them back or report what changed, and
`truncate`, which empties tables with the right statement for each backend.
A pytest plugin wraps them in fixtures.

Nothing in `nexusql.testing` imports pytest, so the factories and snapshots
also work from unittest or from a script seeding a development database.
The fixtures live in `nexusql.testing.pytest_plugin` and need pytest.

```python
from nexusql.testing import Factory, Sequence, SubFactory, snapshot, truncate
# or, for the main names:
from nexusql import Factory, Sequence, snapshot, truncate
```

## Factories

A `Factory` pairs a table with defaults for its columns. A default is one
of:

- a plain value, used as-is;
- a `Sequence("user{n}@example.com")`, which produces `user1@...`,
  `user2@...` and so on, one per row, so UNIQUE columns never collide. It
  also takes a callable: `Sequence(lambda n: f"C{n:04d}")`, and a `start`;
- a `SubFactory(parent_factory, **parent_overrides)` for a foreign key: on
  `create` it creates a parent row and stores the parent's key;
- any other callable, called with no arguments per row: `datetime.now`,
  `uuid.uuid4`, a lambda.

```python
from datetime import datetime
from nexusql.testing import Factory, Sequence, SubFactory

accounts = Factory("accounts", name=Sequence("Account {n}"))
users = Factory(
    "users",
    email=Sequence("user{n}@example.com"),
    name="Test User",
    active=True,
    created_at=datetime.now,
    account_id=SubFactory(accounts),
)
```

`build(**overrides)` returns the row as a dict without touching a database.
`create(db, **overrides)` inserts it and returns the row including the
generated primary key, read back through `execute(..., return_id=True)`:

```python
row = users.create(db, name="Alice")
# {'email': 'user1@example.com', 'name': 'Alice', 'active': True,
#  'created_at': datetime(...), 'account_id': 1, 'id': 1}

rows = users.create_batch(db, 5)     # five rows, five emails, five accounts
```

Overrides replace the default for that row and are resolved the same way,
so an override may itself be a callable or a Sequence. An overridden
Sequence does not advance.

The primary key column is `id` unless the factory says otherwise:
`Factory("tags", pk="tag_id", label="x")`. A row whose key column already
holds a non-None value (`users.create(db, id=42)`) is inserted with that
key instead of a generated one.

`build()` leaves a `SubFactory` column as None, since there is no parent row
and so no key to point at; pass the foreign key explicitly when building.

Column names are written into the INSERT as given, matching the rest of the
library; quote them yourself if they need it.

## Snapshots

`snapshot(db, tables)` reads the named tables into memory. Tables are given
in insert order, parents before the tables that reference them; that is the
order `restore` re-inserts in, and the reverse of the order it deletes in.

```python
from nexusql.testing import snapshot

snap = snapshot(db, ["accounts", "users"])

... the code under test runs ...

diff = snap.diff(db)
assert not diff["accounts"]
assert set(diff["users"].added) == {new_user_id}
assert diff["users"].changed[7][1]["name"] == "renamed"   # (before, after)

snap.restore(db)
```

`diff(db)` returns one `TableDiff` per table with `added`, `removed` and
`changed` dicts keyed by primary key; `changed` maps a key to a
`(before, after)` pair. A `TableDiff` is falsy when nothing differs. Rows
are matched on the `id` column by default; pass `pk="code"` for every table
or `pk={"users": "user_id"}` per table.

`restore(db)` truncates the tables (children first) and re-inserts the
snapshotted rows with their original keys, inside one transaction (joining
yours if one is open). Afterwards the generated-key counters are moved past
the restored keys where the database does not do that itself: PostgreSQL
sequences through `setval`, SQL Server identity columns by inserting under
`IDENTITY_INSERT`. SQLite and MySQL track the maximum on their own.

## truncate

```python
from nexusql.testing import truncate

truncate(db, ["accounts", "users"])                       # keys restart at 1
truncate(db, ["accounts", "users"], reset_identity=False)  # keys keep counting
```

Tables are given in insert order and emptied in reverse. Each backend gets
its own statement:

| Backend    | reset_identity=True                                               | reset_identity=False |
|------------|-------------------------------------------------------------------|----------------------|
| PostgreSQL | `TRUNCATE a, b RESTART IDENTITY CASCADE`                          | `TRUNCATE .. CONTINUE IDENTITY CASCADE` |
| MySQL      | `TRUNCATE TABLE`; a referenced table: `DELETE` + `ALTER TABLE .. AUTO_INCREMENT = 1` | `DELETE FROM` |
| SQL Server | `TRUNCATE TABLE`; a referenced table: `DELETE` + `DBCC CHECKIDENT (.., RESEED, 0)` | `DELETE FROM` |
| SQLite     | `DELETE FROM` + remove the table's `sqlite_sequence` row           | `DELETE FROM` |

Two things to know. PostgreSQL's `CASCADE` also empties tables that
reference the listed ones through foreign keys, whether or not they were
listed. MySQL's `TRUNCATE` and `ALTER TABLE` commit implicitly, so a
truncate on MySQL cannot be undone by rolling back.

Whether a table is referenced is read from the catalog rather than found out
by letting `TRUNCATE` fail: `execute()` rolls the connection back on any
error, which inside a caller's transaction would discard everything done so
far.

## pytest fixtures

Installing nexusql registers the plugin through the `pytest11` entry point,
so the fixtures are available in any pytest run without a conftest line.
From a source checkout that is not installed, load it with
`-p nexusql.testing.pytest_plugin` or
`pytest_plugins = ["nexusql.testing.pytest_plugin"]` in the root conftest
(not both; pytest registers a plugin module once).

| Fixture          | Gives                                                                 |
|------------------|-----------------------------------------------------------------------|
| `nexusql_sqlite` | A connected `DatabaseManager` on a fresh SQLite file under `tmp_path` |
| `nexusql_url`    | The URL `nexusql_db` connects to (override it to change the default) |
| `nexusql_db`     | A connected `DatabaseManager` on `nexusql_url`                        |
| `nexusql_tx`     | `nexusql_db`, with the test inside one transaction rolled back after  |

`nexusql_db` picks its URL from, in order, the `--nexusql-url` command line
option, the `NEXUSQL_TEST_URL` environment variable, and a SQLite file under
`tmp_path`. A server URL that does not answer skips the test; a SQLite file
that cannot be opened fails it, since that is a broken setup rather than an
absent service.

```bash
pytest                                                    # SQLite in tmp_path
NEXUSQL_TEST_URL=postgresql://u:p@localhost/app_test pytest
pytest --nexusql-url mysql://u:p@localhost/app_test
```

```python
users = Factory("users", email=Sequence("user{n}@example.com"))

@pytest.fixture
def schema(nexusql_db):
    nexusql_db.execute("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, email TEXT)")

def test_signup(schema, nexusql_tx):
    users.create(nexusql_tx)
    assert nexusql_tx.fetch_one("SELECT COUNT(*) AS n FROM users") == {"n": 1}
    # rolled back at teardown: the next test sees an empty table
```

`nexusql_tx` rolls back by raising a private exception inside
`db.transaction()` at teardown, so it undoes exactly what the manager's own
transaction would. That has limits per backend. PostgreSQL and SQL Server
roll DDL back along with the data. MySQL commits implicitly on any DDL and on
`TRUNCATE`, so those stay. Python's sqlite3 only opens a transaction at the
first `INSERT`/`UPDATE`/`DELETE`, so a `CREATE TABLE` issued before any write
on SQLite is committed on the spot. Create schema in a fixture that uses
`nexusql_db`, as above, and keep the test body to DML.

Because the test shares one pinned connection for its duration, a second
`DatabaseManager` opened inside the test does not see the test's rows, and
on SQLite it may find the file locked.
