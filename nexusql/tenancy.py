"""
Row-level security and multi-tenancy helpers.

A multi-tenant application stores every tenant's rows in the same tables and
must never let one tenant read or write another's. The usual answer is a
``WHERE tenant_id = ?`` on every query, which is one forgotten clause away
from a data leak. These helpers move the tenant into the database session,
where a policy or a view applies it to every statement:

- ``DatabaseManager.tenant(tenant_id)`` sets the tenant for one transaction.
- ``DatabaseManager.enable_row_security(table)`` makes the database filter
  the table on that value.
- ``DatabaseManager.scoped(table)`` names what to query: the table itself
  where the server enforces the filter, a filtered view elsewhere.

What is enforced differs by backend, and the difference matters:

PostgreSQL
    Real row-level security. ``ENABLE`` and ``FORCE ROW LEVEL SECURITY`` are
    set on the table and one policy covering SELECT, INSERT, UPDATE and
    DELETE compares the tenant column to ``current_setting('app.tenant_id')``.
    Every statement against the table is filtered server-side, whatever
    client issued it. Superusers always bypass row-level security; the table
    owner is subject to it only because FORCE is on. With no tenant set the
    policy sees NULL and matches nothing.

MySQL, SQL Server, SQLite
    These engines have no equivalent the manager can set up with one call
    (SQL Server's security policies need a predicate function per table;
    MySQL and SQLite have none). The helper creates a view named
    ``<table>_scoped`` that filters on the session value. Queries through the
    view are tenant-scoped; queries against the table are not. Nothing stops
    a client with table access from reading everything, so on these backends
    the view is a convenience filter, not a security boundary.

The tenant value is sent as text on every backend and the database converts
it to the column's type. On PostgreSQL the policy casts the setting to the
tenant column's declared type, so an indexed integer or uuid column is still
compared as that type and the index remains usable.

The value lives on the connection, so ``tenant()`` always runs inside a
transaction: the manager pins one connection for the duration, and on
PostgreSQL ``set_config(..., is_local => true)`` reverts the setting when
that transaction ends. MySQL user variables and SQL Server session context
are session-wide, so for those the previous value is written back when the
block exits, before the connection returns to the pool.
"""

import logging
import re
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .interfaces import DatabaseType

logger = logging.getLogger(__name__)


# Name of the PostgreSQL setting the policy reads. Custom settings need a
# two-part name; the ``app.`` prefix keeps it clear of server parameters.
PG_TENANT_SETTING = 'app.tenant_id'

# One policy per table, replaced on every enable_row_security() call.
TENANT_POLICY_NAME = 'nexusql_tenant_isolation'

SCOPED_SUFFIX = '_scoped'

# MySQL views may not reference user variables directly, so the view reads
# the variable through a stored function instead. DETERMINISTIC is what lets
# the function be created when binary logging is on without SUPER; the
# variable cannot change while one statement runs, so within a statement the
# label is accurate.
MYSQL_TENANT_FUNCTION = 'nexusql_tenant_id'
MYSQL_TENANT_FUNCTION_SQL = (
    f"CREATE FUNCTION {MYSQL_TENANT_FUNCTION}() RETURNS VARCHAR(255) "
    f"DETERMINISTIC NO SQL RETURN @app_tenant_id"
)
MYSQL_TENANT_FUNCTION_EXISTS_SQL = """
    SELECT COUNT(*) AS n FROM information_schema.routines
    WHERE routine_schema = DATABASE()
      AND routine_type = 'FUNCTION'
      AND routine_name = :name
"""

# How each backend stores the tenant for the current connection.
SET_TENANT_SQL = {
    DatabaseType.POSTGRESQL: f"SELECT set_config('{PG_TENANT_SETTING}', :tenant_id, true)",
    DatabaseType.MYSQL: "SET @app_tenant_id = :tenant_id",
    DatabaseType.MSSQL: "EXEC sp_set_session_context N'tenant_id', :tenant_id",
}

# How a policy or view reads that value back.
TENANT_EXPRESSION = {
    DatabaseType.POSTGRESQL: f"NULLIF(current_setting('{PG_TENANT_SETTING}', true), '')",
    DatabaseType.MYSQL: f"{MYSQL_TENANT_FUNCTION}()",
    DatabaseType.MSSQL: "CAST(SESSION_CONTEXT(N'tenant_id') AS NVARCHAR(128))",
    DatabaseType.SQLITE: "app_tenant_id()",
}

# Declared type of the tenant column, so the policy can cast the text
# setting to it. Resolved through regclass so quoting and schema prefixes
# are read the same way the table name is read in DDL.
PG_COLUMN_TYPE_SQL = """
    SELECT format_type(a.atttypid, a.atttypmod) AS column_type
    FROM pg_attribute a
    WHERE a.attrelid = :table::regclass
      AND a.attname = :column
      AND NOT a.attisdropped
"""

# A bare or double-quoted identifier, optionally schema-qualified. Table and
# column names are spliced into DDL, so anything else is refused outright.
_PART = r'(?:"[^"]+"|[A-Za-z_][\w$]*)'
_QUALIFIED = re.compile(rf'^{_PART}(?:\.{_PART})?$')
_SIMPLE = re.compile(rf'^{_PART}$')


def check_identifier(name: str, *, qualified: bool = False) -> str:
    """Return ``name`` if it is a plain SQL identifier, else raise ValueError."""
    pattern = _QUALIFIED if qualified else _SIMPLE
    if not isinstance(name, str) or not pattern.match(name.strip()):
        raise ValueError(
            f"{name!r} is not a plain SQL identifier; row security helpers "
            f"accept bare or double-quoted names only"
        )
    return name.strip()


def unquote(identifier: str) -> str:
    """Catalog form of an identifier: quotes removed, bare names case-folded."""
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1]
    return identifier.lower()


def scoped_name(db_type: DatabaseType, table: str) -> str:
    """
    Name to query for tenant-scoped rows.

    On PostgreSQL the table itself, since the policy filters it. Elsewhere
    the ``<table>_scoped`` view. A schema prefix stays on the view and a
    quoted name keeps the suffix inside the quotes.
    """
    table = check_identifier(table, qualified=True)
    if db_type == DatabaseType.POSTGRESQL:
        return table
    schema, _, bare = table.rpartition('.')
    if bare.startswith('"'):
        view = f'"{bare[1:-1]}{SCOPED_SUFFIX}"'
    else:
        view = f"{bare}{SCOPED_SUFFIX}"
    return f"{schema}.{view}" if schema else view


def tenant_value(tenant_id: Any) -> str:
    """The tenant as the text the session stores."""
    if tenant_id is None:
        raise ValueError("tenant_id must not be None")
    return str(tenant_id)


def row_security_statements(db_type: DatabaseType, table: str,
                            tenant_column: str = "tenant_id",
                            column_type: Optional[str] = None) -> List[str]:
    """
    DDL that scopes ``table`` to the session's tenant, as separate statements.

    PostgreSQL gets row-level security on the table itself. ``column_type``
    is the tenant column's declared type; when given, the setting is cast to
    it so the comparison stays index-friendly, otherwise the column is
    compared as text. The other backends get a ``<table>_scoped`` view.
    """
    table = check_identifier(table, qualified=True)
    tenant_column = check_identifier(tenant_column)
    reader = TENANT_EXPRESSION.get(db_type)
    if reader is None:
        raise ValueError(f"Row security is not supported on {db_type}")

    if db_type == DatabaseType.POSTGRESQL:
        if column_type:
            predicate = f"{tenant_column} = {reader}::{column_type}"
        else:
            predicate = f"{tenant_column}::text = {reader}"
        return [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS {TENANT_POLICY_NAME} ON {table}",
            f"CREATE POLICY {TENANT_POLICY_NAME} ON {table} FOR ALL "
            f"USING ({predicate}) WITH CHECK ({predicate})",
        ]

    view = scoped_name(db_type, table)
    select = f"SELECT * FROM {table} WHERE {tenant_column} = {reader}"
    if db_type == DatabaseType.MYSQL:
        return [f"CREATE OR REPLACE VIEW {view} AS {select}"]
    if db_type == DatabaseType.MSSQL:
        return [f"CREATE OR ALTER VIEW {view} AS {select}"]
    return [f"DROP VIEW IF EXISTS {view}", f"CREATE VIEW {view} AS {select}"]


def disable_row_security_statements(db_type: DatabaseType, table: str) -> List[str]:
    """DDL that undoes ``row_security_statements`` for ``table``."""
    table = check_identifier(table, qualified=True)
    if db_type == DatabaseType.POSTGRESQL:
        return [
            f"DROP POLICY IF EXISTS {TENANT_POLICY_NAME} ON {table}",
            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
        ]
    if db_type in (DatabaseType.MYSQL, DatabaseType.MSSQL, DatabaseType.SQLITE):
        return [f"DROP VIEW IF EXISTS {scoped_name(db_type, table)}"]
    raise ValueError(f"Row security is not supported on {db_type}")


def column_type_from_row(row: Optional[Dict], table: str, column: str) -> str:
    """Read the PG_COLUMN_TYPE_SQL result, or explain which column is missing."""
    if not row or not row.get('column_type'):
        raise ValueError(f"column {column} not found on table {table}")
    return row['column_type']


# ---------------------------------------------------------------------------
# Sync manager operations
# ---------------------------------------------------------------------------
#
# These take the manager rather than living on it so manager.py gains only
# thin forwarding methods. They reach for the manager's connection-handling
# internals (_borrow, _execute_raw, _state) on purpose: the tenant has to be
# set on the very connection the transaction pinned.

def _run_statement(db, sql: str, params: Dict[str, Any]) -> None:
    """
    Run a statement on the pinned connection, ignoring any result.

    Bypasses execute(): on SQL Server the EXEC path there fetches a result
    set, and sp_set_session_context does not produce one.
    """
    with db._borrow() as conn:
        cursor = db._execute_raw(sql, params, conn)
        try:
            cursor.close()
        except Exception:
            pass


def _restore_value(db_type: DatabaseType, previous: Optional[str]) -> Optional[str]:
    """Value that clears the tenant when there was no outer one."""
    if previous is not None:
        return previous
    # set_config() takes text, so PostgreSQL clears with an empty string,
    # which the policy's NULLIF turns back into "no tenant".
    return '' if db_type == DatabaseType.POSTGRESQL else None


@contextmanager
def tenant_scope(db, tenant_id: Any):
    """
    Set the session tenant for the duration of one transaction.

    Opens a transaction if none is open, so the setting is tied to a pinned
    connection and gone before that connection returns to the pool. Inside
    an existing transaction the block joins it and the previous tenant is
    written back on exit, since the transaction end that would revert it
    has not happened yet.
    """
    value = tenant_value(tenant_id)
    db_type = db.config.database_type
    joined = db._in_transaction

    # Setting a session variable needs the transaction's scope, not a
    # savepoint of its own, so a nested block joins the one already open.
    with db.transaction(nested="join"):
        previous = getattr(db._state, 'tenant_id', None)
        db._state.tenant_id = value
        try:
            if db_type in SET_TENANT_SQL:
                _run_statement(db, SET_TENANT_SQL[db_type], {"tenant_id": value})
            yield db
        finally:
            db._state.tenant_id = previous
            # PostgreSQL's local setting dies with the transaction, so only
            # a joined block has to put the outer value back. MySQL and SQL
            # Server keep session state across transactions and must always
            # be reset.
            session_scoped = db_type in (DatabaseType.MYSQL, DatabaseType.MSSQL)
            if db_type in SET_TENANT_SQL and (joined or session_scoped):
                try:
                    _run_statement(db, SET_TENANT_SQL[db_type],
                                   {"tenant_id": _restore_value(db_type, previous)})
                except Exception:
                    # Reached after the block already failed: the rollback
                    # undoes a local setting, and a dead connection is
                    # dropped by the pool's checkout validation.
                    logger.debug("could not restore previous tenant", exc_info=True)


def enable_row_security(db, table: str, tenant_column: str = "tenant_id") -> None:
    """Apply ``row_security_statements`` through the manager, in one transaction."""
    db_type = db.config.database_type
    table = check_identifier(table, qualified=True)
    tenant_column = check_identifier(tenant_column)

    with db.transaction():
        column_type = None
        if db_type == DatabaseType.POSTGRESQL:
            row = db.fetch_one(PG_COLUMN_TYPE_SQL,
                               {"table": table, "column": unquote(tenant_column)})
            column_type = column_type_from_row(row, table, tenant_column)
        elif db_type == DatabaseType.MYSQL:
            found = db.fetch_one(MYSQL_TENANT_FUNCTION_EXISTS_SQL,
                                 {"name": MYSQL_TENANT_FUNCTION})
            if not found or not found.get('n'):
                db.execute(MYSQL_TENANT_FUNCTION_SQL)

        for statement in row_security_statements(db_type, table, tenant_column, column_type):
            db.execute(statement)


def disable_row_security(db, table: str) -> None:
    """Apply ``disable_row_security_statements`` through the manager."""
    db_type = db.config.database_type
    with db.transaction():
        for statement in disable_row_security_statements(db_type, table):
            db.execute(statement)
