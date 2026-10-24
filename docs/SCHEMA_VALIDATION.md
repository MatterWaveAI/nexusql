# Schema introspection and validation

`nexusql/schema.py` lets an application ask what a table really looks like,
compare that with what it expects, and check a parameter dict against the
table before a query runs. All of it works on SQLite, PostgreSQL, MySQL and
SQL Server through the same calls, and the comparison understands the SQL
translator, so a spec written against the canonical PostgreSQL types is
valid on every backend.

```python
from nexusql import DatabaseManager, TableSpec, ColumnSpec, IndexSpec

db = DatabaseManager("sqlite:///app.db")
db.connect()

info = db.introspect("users")          # TableInfo
report = db.validate_schema([          # ValidationReport
    TableSpec("users", columns=[
        ColumnSpec("id", "SERIAL", primary_key=True),
        ColumnSpec("email", "VARCHAR(255)", nullable=False),
        ColumnSpec("active", "BOOLEAN"),
        ColumnSpec("profile", "JSONB"),
    ], indexes=[IndexSpec(("email",), unique=True)]),
])
if not report.ok:
    raise SystemExit(str(report))

db.check_params("users", {"email": "a@b.c", "active": True})   # or SchemaValidationError
```

## Introspection

`db.introspect(table) -> TableInfo` returns the table's columns and indexes.
`db.list_tables() -> list[str]` names the base tables in the current schema
(the current database on MySQL). A table that does not exist raises
`TableNotFoundError` (a `LookupError`).

```python
@dataclass(frozen=True)
class ColumnInfo:
    name: str
    raw_type: str          # as the dialect reports it: "character varying(255)", "nvarchar(max)", "TEXT"
    kind: str              # normalised: integer, bigint, float, decimal, text, boolean,
                           #             timestamp, date, json, uuid, bytes, other
    nullable: bool
    default: str | None    # the catalog's default expression, unparsed
    is_primary_key: bool
    is_auto_increment: bool

@dataclass(frozen=True)
class IndexInfo:
    name: str
    columns: tuple[str, ...]
    unique: bool
    primary: bool          # the index backing the primary key

@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]
    primary_key -> list[str]
    column(name) -> ColumnInfo | None     # exact, then case-insensitive
```

### Where the information comes from

| Backend | Columns | Primary key / auto-increment | Indexes |
|---|---|---|---|
| PostgreSQL | `information_schema.columns` | `table_constraints` + `key_column_usage`; `pg_get_serial_sequence()` and `is_identity` | `pg_index` + `pg_attribute` |
| MySQL | `information_schema.columns` (`COLUMN_TYPE`, `COLUMN_KEY`, `EXTRA`) | `COLUMN_KEY = 'PRI'`, `EXTRA` contains `auto_increment` | `information_schema.statistics` |
| SQL Server | `information_schema.columns` | `table_constraints` + `key_column_usage`; `COLUMNPROPERTY(.., 'IsIdentity')` | `sys.indexes` + `sys.index_columns` |
| SQLite | `pragma_table_info()` | `pk` column; a lone `INTEGER PRIMARY KEY` counts as auto-increment (it is the rowid alias) | `pragma_index_list()` + `pragma_index_info()` |

Unqualified names resolve in the current schema (`current_schema()`,
`DATABASE()`, `SCHEMA_NAME()`); `schema.table`, `"quoted"`, `[bracketed]` and
`` `backticked` `` names are understood. On PostgreSQL an unquoted name is
folded to lower case, as `CREATE TABLE` folded it.

### How raw types become kinds

The dialect's type name is mapped to one of twelve kinds. The table lists
the common spellings; anything unrecognised is `other`.

| kind | spellings |
|---|---|
| integer | int, integer, int4, smallint, int2, mediumint, tinyint, serial |
| bigint | bigint, int8, bigserial |
| float | real, float, float4, float8, double, double precision |
| decimal | numeric, decimal, money, smallmoney |
| text | text, varchar, character varying, char, nvarchar, nchar, ntext, tinytext/mediumtext/longtext, enum, set |
| boolean | boolean, bool, bit, bit(1), tinyint(1) |
| timestamp | timestamp (with/without time zone), timestamptz, datetime, datetime2, datetimeoffset, smalldatetime |
| date | date |
| json | json, jsonb |
| uuid | uuid, uniqueidentifier |
| bytes | bytea, blob, binary, varbinary, image, tinyblob/mediumblob/longblob |

Lengths, precision and MySQL's `unsigned` are ignored for the kind and kept
in `raw_type`.

## Validation

`db.validate_schema(expected, strict=False) -> ValidationReport` compares the
live schema with a list of `TableSpec`. The spec can also be given in dict
form, as a list or as a `{table_name: spec}` mapping; `columns` may be a
mapping of name to kind string:

```python
report = db.validate_schema({
    "users": {
        "columns": {
            "id": {"kind": "SERIAL", "primary_key": True},
            "email": {"kind": "text", "nullable": False},
            "active": "boolean",
        },
        "indexes": [["email"], {"columns": ["created_at", "id"], "unique": False}],
    },
})
```

`ColumnSpec.kind` accepts either a kind name or a canonical PostgreSQL type
(`SERIAL`, `VARCHAR(255)`, `JSONB`); both are normalised. `nullable`
defaults to `True`; a column marked `primary_key=True` is expected NOT NULL.
`auto_increment` is only checked when set.

The report lists, in reading order:

- `name: missing table`
- `table.col: missing column`
- `table.col: expected kind X, found Y (declared RAW)`
- `table.col: expected nullable, found NOT NULL` (and the reverse)
- `table.col: expected a primary key column`
- `table.col: expected an auto-increment column, found none` (and the reverse)
- `table: missing index on (a, b)` or `missing unique index on (a)`
- `table.col: unexpected column (RAW)`: an error with `strict=True`, otherwise a warning

`report.ok` is true when there are no errors (warnings do not count);
`report.errors`, `report.warnings`, `report.missing_tables`,
`report.missing_columns`, `report.unexpected_columns` and
`report.missing_indexes` break the findings out, and `str(report)` prints
them one per line.

### Kind equivalences

A kind mismatch is judged through the translator. On each backend a column
of the left-hand kind satisfies any of the right-hand canonical kinds,
because that is what `_translate_sql` turned those types into:

| backend | column kind | satisfies |
|---|---|---|
| SQLite | integer | integer, boolean (`BOOLEAN` becomes `INTEGER`) |
| SQLite | text | text, uuid, json, timestamp (`UUID`, `JSONB`, `TIMESTAMP`, `VARCHAR` all become `TEXT`) |
| MySQL | text declared `char(36)` | text, uuid (`UUID` becomes `CHAR(36)`; other text columns are text only) |
| SQL Server | text declared `nvarchar(max)` | text, json (`JSONB` becomes `NVARCHAR(MAX)`; `nvarchar(255)` is text only) |
| PostgreSQL | any | itself only |

`SERIAL` is an integer with auto-increment on every backend
(`AUTOINCREMENT`, `AUTO_INCREMENT`, `IDENTITY`); `BOOLEAN` is `BIT` on SQL
Server and `TINYINT(1)` on MySQL, both of which normalise to `boolean`
directly. `integer` and `bigint` are distinct kinds everywhere.

Indexes are matched on their column list, in order, not on their name: the
same canonical DDL names an index `users_email_key` on PostgreSQL and
`sqlite_autoindex_users_1` on SQLite. A unique index satisfies a non-unique
expectation and the primary key counts as a unique index on its columns, so
`IndexSpec(("id",), unique=True)` passes on SQLite where the rowid alias has
no index of its own.

## Parameter checking

`db.check_params(table, params)` compares a `:name` params dict with the
introspected columns and raises `SchemaValidationError` naming every key
that does not fit: a key that is not a column, a `None` for a NOT NULL
column, or a value whose Python type does not suit the column's kind.
Accepted types per kind:

| kind | accepted values |
|---|---|
| integer, bigint | `int` (not `bool`, except where boolean collapses into integer, i.e. SQLite) |
| float, decimal | `int`, `float`, `Decimal` |
| text | `str` |
| boolean | `bool`, `0`, `1` |
| timestamp | `datetime`, `str` |
| date | `date`, `datetime`, `str` |
| json | `dict`, `list`, `str` |
| uuid | `UUID`, `str` |
| bytes | `bytes`, `bytearray`, `memoryview` |
| other | anything |

The same equivalences apply: a `dict` is fine for the `NVARCHAR(MAX)` column
that `JSONB` became on SQL Server, and a `UUID` for the `TEXT` column on
SQLite.

Introspection is cached per table on first use, so a warm check is a
dictionary walk with no round-trip. `db.introspect(table)` always queries
and refreshes the entry; `db.invalidate_schema_cache(table=None)` drops one
table or everything, which is what to call after running a migration.

```python
from nexusql import SchemaValidationError

try:
    db.check_params("users", {"email": None, "age": "forty", "nickname": "x"})
except SchemaValidationError as exc:
    exc.table    # "users"
    exc.keys     # ["email", "age", "nickname"]
    exc.errors   # ["email: None for NOT NULL column",
                 #  "age: str value for integer column (integer)",
                 #  "nickname: no such column"]
```

## Async backend

`AsyncPostgresBackend` has `await introspect(table)`, `await list_tables()`
and `await validate_schema(expected, strict=False)` running the same
PostgreSQL catalog queries and producing the same dataclasses.
`check_params` is not offered there; it is a sync-manager convenience built
on the per-table cache.

## Limitations

- SQLite reports a `TEXT PRIMARY KEY` as NOT NULL even though the engine
  would accept a NULL; the canonical dialect never has a nullable key.
- On SQLite a lone `INTEGER PRIMARY KEY` is always reported as
  auto-increment, since it is the rowid alias, whereas on PostgreSQL only
  `SERIAL` and identity columns are; a spec with `auto_increment=False` on
  such a column will fail on SQLite only.
- Expression indexes and partial indexes are reported only by the columns
  they reference; a purely expression-based index is omitted.
- Defaults are the catalog's expression text (`nextval(...)`, `((1))`,
  `CURRENT_TIMESTAMP`) and are not normalised or validated.
- `check_params` reasons about Python types, not values: a 20-character
  string passes for `VARCHAR(10)`.
