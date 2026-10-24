"""
Trigger-based change data capture.

``db.cdc.enable(table)`` installs AFTER INSERT / UPDATE / DELETE triggers on
a table that append one row per changed row to ``<table>__changes``: the
operation, the primary-key value as text, and the old and new rows as JSON
text. ``db.cdc.changes(table)`` reads them back with the JSON decoded.

Design notes
------------
- Triggers rather than log readers. Logical replication, the binlog and SQL
  Server CDC each need server-side configuration and a privileged reader, and
  none of them exists on SQLite. Triggers work on every backend with nothing
  but DDL rights, at the cost of running synchronously inside each write.
- Each dialect's DDL is written out in full. Trigger syntax differs far too
  much between the four engines for the regex translator in ``manager.py``,
  and the translator would also damage the finished DDL (``FOR JSON`` on SQL
  Server, ``TIMESTAMP`` on SQLite), so these statements bypass it.
- Rows are serialised by the database, not by Python, so changes made by any
  client (psql, another service, a migration) are captured too. The column
  list is read once at ``enable`` time and baked into the triggers; after an
  ALTER TABLE, run ``enable`` again to regenerate them.
- Only validated identifiers are interpolated into SQL. Everything else
  travels as a bound parameter.
"""

import json
import re
from functools import partial
from typing import Any, Dict, List, Optional

from .interfaces import DatabaseType

# A bare identifier. 54 characters leaves room for the ``__changes`` and
# ``__cdc_fn`` suffixes within PostgreSQL's 63-byte identifier limit, so a
# table accepted here gets usable derived names on every backend.
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,53}$')

# Which quote character each engine uses around identifiers. MySQL only
# honours double quotes in ANSI_QUOTES mode, and SQL Server only with
# QUOTED_IDENTIFIER on, so each gets its native form.
_QUOTES = {
    DatabaseType.POSTGRESQL: ('"', '"'),
    DatabaseType.SQLITE: ('"', '"'),
    DatabaseType.MYSQL: ('`', '`'),
    DatabaseType.MSSQL: ('[', ']'),
}

OPS = ('I', 'U', 'D')


def validate_identifier(name: str, what: str = "identifier") -> str:
    """
    Return ``name`` if it is a plain identifier, else raise ValueError.

    Schema-qualified and quoted names are rejected on purpose: the derived
    trigger and table names are built from this string, and a dot or a quote
    inside it would produce names no engine accepts.
    """
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ValueError(
            f"Invalid {what} {name!r}: expected letters, digits and underscores "
            "only (not starting with a digit), at most 54 characters"
        )
    return name


def changes_table(table: str) -> str:
    """Name of the table that stores ``table``'s change rows."""
    return f"{table}__changes"


def trigger_names(table: str) -> Dict[str, str]:
    """The three trigger names for ``table``, keyed by operation."""
    return {op: f"{table}__cdc_{op.lower()}" for op in OPS}


def function_name(table: str) -> str:
    """Name of the PostgreSQL trigger function for ``table``."""
    return f"{table}__cdc_fn"


def quote(db_type: DatabaseType, name: str) -> str:
    """Quote a validated identifier in the engine's own style."""
    left, right = _QUOTES[db_type]
    return f"{left}{name}{right}"


# --- DDL generation -------------------------------------------------------------
#
# Pure functions of (dialect, table, pk, columns), so the generated text can be
# checked without a server. Each returns the statements in the order they must
# run, one statement per element: SQL Server requires CREATE TRIGGER to be
# alone in its batch, and the other drivers execute one statement per call.


def enable_statements(db_type: DatabaseType, table: str, pk: str,
                      columns: List[str]) -> List[str]:
    """
    DDL that creates the changes table (if missing) and (re)installs the
    triggers. Existing triggers are dropped first, which is what makes
    ``enable`` safe to repeat and the way to pick up a changed column list.
    """
    if pk not in columns:
        raise ValueError(
            f"Primary key column {pk!r} is not a column of {table!r} "
            f"(columns: {', '.join(columns)})"
        )
    builders = {
        DatabaseType.POSTGRESQL: _postgres_enable,
        DatabaseType.SQLITE: _sqlite_enable,
        DatabaseType.MYSQL: _mysql_enable,
        DatabaseType.MSSQL: _mssql_enable,
    }
    try:
        builder = builders[db_type]
    except KeyError:
        raise NotImplementedError(f"Change data capture is not supported on {db_type}")
    return builder(table, pk, columns)


def disable_statements(db_type: DatabaseType, table: str) -> List[str]:
    """DDL that removes the triggers (and the PostgreSQL function)."""
    q = partial(quote, db_type)
    names = trigger_names(table)

    if db_type == DatabaseType.POSTGRESQL:
        drops = [f'DROP TRIGGER IF EXISTS {q(n)} ON {q(table)}' for n in names.values()]
        drops.append(f'DROP FUNCTION IF EXISTS {q(function_name(table))}()')
        return drops

    if db_type == DatabaseType.MSSQL:
        # DROP TRIGGER IF EXISTS needs SQL Server 2016; the OBJECT_ID guard
        # works on every supported version.
        return [
            f"IF OBJECT_ID(N'{q(n)}', N'TR') IS NOT NULL DROP TRIGGER {q(n)}"
            for n in names.values()
        ]

    if db_type in (DatabaseType.SQLITE, DatabaseType.MYSQL):
        return [f'DROP TRIGGER IF EXISTS {q(n)}' for n in names.values()]

    raise NotImplementedError(f"Change data capture is not supported on {db_type}")


def drop_statements(db_type: DatabaseType, table: str) -> List[str]:
    """DDL that removes the triggers and the changes table."""
    statements = disable_statements(db_type, table)
    target = quote(db_type, changes_table(table))
    if db_type == DatabaseType.MSSQL:
        statements.append(f"IF OBJECT_ID(N'{target}', N'U') IS NOT NULL DROP TABLE {target}")
    else:
        statements.append(f'DROP TABLE IF EXISTS {target}')
    return statements


def _postgres_enable(table: str, pk: str, columns: List[str]) -> List[str]:
    q = partial(quote, DatabaseType.POSTGRESQL)
    changes = q(changes_table(table))
    fn = q(function_name(table))
    names = trigger_names(table)

    statements = [
        f"""CREATE TABLE IF NOT EXISTS {changes} (
    change_id BIGSERIAL PRIMARY KEY,
    op CHAR(1) NOT NULL CHECK (op IN ('I', 'U', 'D')),
    pk TEXT,
    old_row TEXT,
    new_row TEXT,
    changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
    ]
    statements += disable_statements(DatabaseType.POSTGRESQL, table)
    # One function serves all three triggers; TG_OP tells them apart.
    # row_to_json sees every column at execution time, so unlike the other
    # dialects this body does not embed the column list.
    statements.append(f"""CREATE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO {changes} (op, pk, old_row, new_row)
        VALUES ('I', NEW.{q(pk)}::text, NULL, row_to_json(NEW)::text);
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO {changes} (op, pk, old_row, new_row)
        VALUES ('U', NEW.{q(pk)}::text, row_to_json(OLD)::text, row_to_json(NEW)::text);
        RETURN NEW;
    ELSE
        INSERT INTO {changes} (op, pk, old_row, new_row)
        VALUES ('D', OLD.{q(pk)}::text, row_to_json(OLD)::text, NULL);
        RETURN OLD;
    END IF;
END;
$$""")
    for op, event in (('I', 'INSERT'), ('U', 'UPDATE'), ('D', 'DELETE')):
        statements.append(
            f'CREATE TRIGGER {q(names[op])} AFTER {event} ON {q(table)} '
            f'FOR EACH ROW EXECUTE FUNCTION {fn}()'
        )
    return statements


def _sqlite_enable(table: str, pk: str, columns: List[str]) -> List[str]:
    q = partial(quote, DatabaseType.SQLITE)
    changes = q(changes_table(table))
    names = trigger_names(table)

    def json_of(row: str) -> str:
        pairs = ', '.join(f"'{c}', {row}.{q(c)}" for c in columns)
        return f'json_object({pairs})'

    statements = [
        f"""CREATE TABLE IF NOT EXISTS {changes} (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT NOT NULL CHECK (op IN ('I', 'U', 'D')),
    pk TEXT,
    old_row TEXT,
    new_row TEXT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
    ]
    statements += disable_statements(DatabaseType.SQLITE, table)
    statements += [
        f"""CREATE TRIGGER {q(names['I'])} AFTER INSERT ON {q(table)} FOR EACH ROW
BEGIN
    INSERT INTO {changes} (op, pk, old_row, new_row)
    VALUES ('I', CAST(NEW.{q(pk)} AS TEXT), NULL, {json_of('NEW')});
END""",
        f"""CREATE TRIGGER {q(names['U'])} AFTER UPDATE ON {q(table)} FOR EACH ROW
BEGIN
    INSERT INTO {changes} (op, pk, old_row, new_row)
    VALUES ('U', CAST(NEW.{q(pk)} AS TEXT), {json_of('OLD')}, {json_of('NEW')});
END""",
        f"""CREATE TRIGGER {q(names['D'])} AFTER DELETE ON {q(table)} FOR EACH ROW
BEGIN
    INSERT INTO {changes} (op, pk, old_row, new_row)
    VALUES ('D', CAST(OLD.{q(pk)} AS TEXT), {json_of('OLD')}, NULL);
END""",
    ]
    return statements


def _mysql_enable(table: str, pk: str, columns: List[str]) -> List[str]:
    q = partial(quote, DatabaseType.MYSQL)
    changes = q(changes_table(table))
    names = trigger_names(table)

    def json_of(row: str) -> str:
        pairs = ', '.join(f"'{c}', {row}.{q(c)}" for c in columns)
        return f'JSON_OBJECT({pairs})'

    # LONGTEXT rather than the JSON type: the JSON type re-orders keys and
    # normalises numbers, and the other backends hand back the text as the
    # engine produced it.
    statements = [
        f"""CREATE TABLE IF NOT EXISTS {changes} (
    change_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    op CHAR(1) NOT NULL CHECK (op IN ('I', 'U', 'D')),
    pk TEXT,
    old_row LONGTEXT,
    new_row LONGTEXT,
    changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
    ]
    statements += disable_statements(DatabaseType.MYSQL, table)
    # Single-statement bodies need no BEGIN/END, so no DELIMITER handling.
    statements += [
        f"""CREATE TRIGGER {q(names['I'])} AFTER INSERT ON {q(table)} FOR EACH ROW
INSERT INTO {changes} (op, pk, old_row, new_row)
VALUES ('I', CAST(NEW.{q(pk)} AS CHAR), NULL, {json_of('NEW')})""",
        f"""CREATE TRIGGER {q(names['U'])} AFTER UPDATE ON {q(table)} FOR EACH ROW
INSERT INTO {changes} (op, pk, old_row, new_row)
VALUES ('U', CAST(NEW.{q(pk)} AS CHAR), {json_of('OLD')}, {json_of('NEW')})""",
        f"""CREATE TRIGGER {q(names['D'])} AFTER DELETE ON {q(table)} FOR EACH ROW
INSERT INTO {changes} (op, pk, old_row, new_row)
VALUES ('D', CAST(OLD.{q(pk)} AS CHAR), {json_of('OLD')}, NULL)""",
    ]
    return statements


def _mssql_enable(table: str, pk: str, columns: List[str]) -> List[str]:
    q = partial(quote, DatabaseType.MSSQL)
    changes = q(changes_table(table))
    names = trigger_names(table)

    def json_of(alias: str) -> str:
        cols = ', '.join(f'{alias}.{q(c)}' for c in columns)
        return f'(SELECT {cols} FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES)'

    statements = [
        f"""IF OBJECT_ID(N'{changes}', N'U') IS NULL
CREATE TABLE {changes} (
    change_id BIGINT IDENTITY(1,1) PRIMARY KEY,
    op CHAR(1) NOT NULL CHECK (op IN ('I', 'U', 'D')),
    pk NVARCHAR(MAX),
    old_row NVARCHAR(MAX),
    new_row NVARCHAR(MAX),
    changed_at DATETIME2 NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
    ]
    statements += disable_statements(DatabaseType.MSSQL, table)
    # SQL Server triggers are per statement, so each body is a set-based
    # SELECT over the inserted/deleted pseudo-tables. UPDATE pairs the two
    # by primary key; a LEFT JOIN keeps a row whose key was changed, with
    # no old side, rather than dropping it.
    statements += [
        f"""CREATE TRIGGER {q(names['I'])} ON {q(table)} AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO {changes} (op, pk, old_row, new_row)
    SELECT 'I', CAST(n.{q(pk)} AS NVARCHAR(MAX)), NULL, {json_of('n')}
    FROM inserted AS n;
END""",
        f"""CREATE TRIGGER {q(names['U'])} ON {q(table)} AFTER UPDATE AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO {changes} (op, pk, old_row, new_row)
    SELECT 'U', CAST(n.{q(pk)} AS NVARCHAR(MAX)),
           CASE WHEN o.{q(pk)} IS NULL THEN NULL ELSE {json_of('o')} END,
           {json_of('n')}
    FROM inserted AS n
    LEFT JOIN deleted AS o ON o.{q(pk)} = n.{q(pk)};
END""",
        f"""CREATE TRIGGER {q(names['D'])} ON {q(table)} AFTER DELETE AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO {changes} (op, pk, old_row, new_row)
    SELECT 'D', CAST(o.{q(pk)} AS NVARCHAR(MAX)), {json_of('o')}, NULL
    FROM deleted AS o;
END""",
    ]
    return statements


# --- catalog queries -----------------------------------------------------------
#
# Written in each engine's own catalog vocabulary and run through the manager,
# so ``:name`` parameters are bound the usual way. The table name is compared
# exactly as given, matching the quoted identifiers the DDL uses.

COLUMNS_SQL = {
    DatabaseType.POSTGRESQL: """
        SELECT column_name AS name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = :table
        ORDER BY ordinal_position""",
    DatabaseType.SQLITE: "SELECT name FROM pragma_table_info(:table) ORDER BY cid",
    DatabaseType.MYSQL: """
        SELECT column_name AS name FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = :table
        ORDER BY ordinal_position""",
    DatabaseType.MSSQL: """
        SELECT column_name AS name FROM information_schema.columns
        WHERE table_schema = SCHEMA_NAME() AND table_name = :table
        ORDER BY ordinal_position""",
}

TRIGGER_COUNT_SQL = {
    DatabaseType.POSTGRESQL: """
        SELECT COUNT(*) AS n FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace s ON s.oid = c.relnamespace
        WHERE s.nspname = current_schema() AND c.relname = :table
          AND NOT t.tgisinternal AND t.tgname IN (:i, :u, :d)""",
    DatabaseType.SQLITE: """
        SELECT COUNT(*) AS n FROM sqlite_master
        WHERE type = 'trigger' AND tbl_name = :table AND name IN (:i, :u, :d)""",
    DatabaseType.MYSQL: """
        SELECT COUNT(*) AS n FROM information_schema.triggers
        WHERE trigger_schema = DATABASE() AND event_object_table = :table
          AND trigger_name IN (:i, :u, :d)""",
    DatabaseType.MSSQL: """
        SELECT COUNT(*) AS n FROM sys.triggers
        WHERE parent_id = OBJECT_ID(:table) AND name IN (:i, :u, :d)""",
}

_CHANGE_COLUMNS = "change_id, op, pk, old_row, new_row, changed_at"


def changes_sql(db_type: DatabaseType, table: str) -> str:
    """The paged read of a changes table, in the engine's LIMIT idiom."""
    target = quote(db_type, changes_table(table))
    if db_type == DatabaseType.MSSQL:
        # The translator only rewrites LIMIT with a literal count, and the
        # count here is a bound parameter.
        return (f"SELECT TOP (:limit) {_CHANGE_COLUMNS} FROM {target} "
                "WHERE change_id > :since ORDER BY change_id")
    return (f"SELECT {_CHANGE_COLUMNS} FROM {target} "
            "WHERE change_id > :since ORDER BY change_id LIMIT :limit")


def purge_sql(db_type: DatabaseType, table: str) -> str:
    target = quote(db_type, changes_table(table))
    return f"DELETE FROM {target} WHERE change_id < :before"


def decode_change(row: Dict[str, Any]) -> Dict[str, Any]:
    """A changes-table row with its JSON text columns decoded to dicts."""
    out = dict(row)
    for side in ('old_row', 'new_row'):
        value = out.get(side)
        out[side] = json.loads(value) if isinstance(value, (str, bytes)) else value
    return out


def _page_args(since_id: int, limit: int) -> Dict[str, int]:
    since_id = int(since_id)
    limit = int(limit)
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    return {'since': since_id, 'limit': limit}


# --- sync API ----------------------------------------------------------------------


class ChangeDataCapture:
    """
    Trigger-based change capture for a ``DatabaseManager``; reached as
    ``db.cdc``.

        db.cdc.enable("orders")
        db.execute("UPDATE orders SET status = :s WHERE id = :id", {...})
        for change in db.cdc.changes("orders"):
            change["op"], change["pk"], change["old_row"], change["new_row"]
    """

    def __init__(self, db):
        self._db = db

    @property
    def _db_type(self) -> DatabaseType:
        return self._db.config.database_type

    def enable(self, table: str, pk: str = "id") -> None:
        """
        Create ``<table>__changes`` if needed and install the triggers.

        Safe to call again: the triggers are replaced, and the changes table
        and its rows are kept. Call it again after an ALTER TABLE so the
        triggers see the new column list.

        Raises ValueError for a malformed name, an unknown table or a ``pk``
        that is not one of its columns.
        """
        validate_identifier(table, "table name")
        validate_identifier(pk, "primary key column")
        columns = self._columns(table)
        statements = enable_statements(self._db_type, table, pk, columns)
        self._run_ddl(statements)

    def disable(self, table: str) -> None:
        """Drop the triggers; the changes table and its rows are kept."""
        validate_identifier(table, "table name")
        self._run_ddl(disable_statements(self._db_type, table))

    def drop(self, table: str) -> None:
        """Drop the triggers and the changes table."""
        validate_identifier(table, "table name")
        self._run_ddl(drop_statements(self._db_type, table))

    def is_enabled(self, table: str) -> bool:
        """True when all three capture triggers exist on ``table``."""
        validate_identifier(table, "table name")
        row = self._db.fetch_one(TRIGGER_COUNT_SQL[self._db_type],
                                 self._trigger_args(table))
        return bool(row) and int(row['n']) == len(OPS)

    def changes(self, table: str, since_id: int = 0, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Change rows with ``change_id > since_id``, oldest first, at most
        ``limit`` of them. ``old_row`` / ``new_row`` are dicts, or None for
        the side an INSERT or DELETE does not have.

        Consumers page by remembering the last ``change_id`` they handled.
        """
        validate_identifier(table, "table name")
        rows = self._db.execute(changes_sql(self._db_type, table),
                                _page_args(since_id, limit))
        return [decode_change(r) for r in rows]

    def purge(self, table: str, before_id: int) -> None:
        """Delete change rows with ``change_id < before_id``."""
        validate_identifier(table, "table name")
        self._db.execute(purge_sql(self._db_type, table), {'before': int(before_id)})

    # -- internals --

    def _trigger_args(self, table: str) -> Dict[str, str]:
        names = trigger_names(table)
        return {'table': table, 'i': names['I'], 'u': names['U'], 'd': names['D']}

    def _columns(self, table: str) -> List[str]:
        rows = self._db.execute(COLUMNS_SQL[self._db_type], {'table': table})
        columns = [r['name'] for r in rows]
        if not columns:
            raise ValueError(f"Table {table!r} does not exist or has no columns")
        return columns

    def _run_ddl(self, statements: List[str]) -> None:
        """
        Run finished DDL on one connection, bypassing the translator.

        The statements are already in the target dialect; translation would
        rewrite ``FOR JSON`` and ``TEXT`` on SQL Server and ``TIMESTAMP`` on
        SQLite. Running inside ``transaction()`` keeps PostgreSQL and SQLite
        all-or-nothing (their DDL is transactional) and reuses one connection
        for the whole sequence everywhere else.
        """
        with self._db.transaction():
            with self._db._borrow() as conn:
                for statement in statements:
                    cursor = conn.cursor()
                    try:
                        cursor.execute(statement)
                    finally:
                        cursor.close()


# --- async API ---------------------------------------------------------------------


class AsyncChangeDataCapture:
    """
    The same operations for ``AsyncPostgresBackend``, reached as
    ``backend.cdc``. PostgreSQL only, like the backend itself.
    """

    def __init__(self, backend):
        self._backend = backend

    async def enable(self, table: str, pk: str = "id") -> None:
        validate_identifier(table, "table name")
        validate_identifier(pk, "primary key column")
        rows = await self._backend.execute(COLUMNS_SQL[DatabaseType.POSTGRESQL],
                                           {'table': table})
        columns = [r['name'] for r in rows]
        if not columns:
            raise ValueError(f"Table {table!r} does not exist or has no columns")
        await self._run_ddl(enable_statements(DatabaseType.POSTGRESQL, table, pk, columns))

    async def disable(self, table: str) -> None:
        validate_identifier(table, "table name")
        await self._run_ddl(disable_statements(DatabaseType.POSTGRESQL, table))

    async def drop(self, table: str) -> None:
        validate_identifier(table, "table name")
        await self._run_ddl(drop_statements(DatabaseType.POSTGRESQL, table))

    async def is_enabled(self, table: str) -> bool:
        validate_identifier(table, "table name")
        names = trigger_names(table)
        rows = await self._backend.execute(
            TRIGGER_COUNT_SQL[DatabaseType.POSTGRESQL],
            {'table': table, 'i': names['I'], 'u': names['U'], 'd': names['D']})
        return bool(rows) and int(rows[0]['n']) == len(OPS)

    async def changes(self, table: str, since_id: int = 0,
                      limit: int = 1000) -> List[Dict[str, Any]]:
        validate_identifier(table, "table name")
        rows = await self._backend.execute(changes_sql(DatabaseType.POSTGRESQL, table),
                                           _page_args(since_id, limit))
        return [decode_change(r) for r in rows]

    async def purge(self, table: str, before_id: int) -> None:
        validate_identifier(table, "table name")
        await self._backend.execute(purge_sql(DatabaseType.POSTGRESQL, table),
                                    {'before': int(before_id)})

    async def _run_ddl(self, statements: List[str]) -> None:
        # The backend's translator leaves this DDL alone (it only rewrites
        # SQLite-isms), and with no params the ``:name`` rewrite is skipped,
        # so the statements can go through execute() unchanged.
        async with self._backend.transaction():
            for statement in statements:
                await self._backend.execute(statement)
