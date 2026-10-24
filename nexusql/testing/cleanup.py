"""
Empty tables between tests with the statement each dialect wants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Union

from ..interfaces import DatabaseType

if TYPE_CHECKING:
    from ..manager import DatabaseManager


def truncate(db: 'DatabaseManager', tables: Union[str, Iterable[str]],
             reset_identity: bool = True) -> None:
    """
    Remove every row from ``tables`` and, by default, restart their generated
    keys at 1.

    ``tables`` is given in insert order (parents before the tables that
    reference them), the same order ``snapshot`` and ``Snapshot.restore``
    take; deletion walks it in reverse so children go first.

    Per backend:

    - PostgreSQL: one ``TRUNCATE .. RESTART IDENTITY CASCADE`` (``CONTINUE
      IDENTITY`` when ``reset_identity`` is False). CASCADE also empties
      tables that reference these through foreign keys, including ones not
      listed here.
    - MySQL: ``TRUNCATE TABLE``, which always resets AUTO_INCREMENT. A table
      another table references cannot be truncated, so it gets ``DELETE``
      plus ``ALTER TABLE .. AUTO_INCREMENT = 1`` instead. Both statements
      commit implicitly, so this cannot be rolled back on MySQL.
    - SQL Server: ``TRUNCATE TABLE``; a referenced table gets ``DELETE`` plus
      ``DBCC CHECKIDENT (.., RESEED, 0)`` when it has an identity column.
    - SQLite: ``DELETE FROM``, and the table's ``sqlite_sequence`` row is
      removed when that catalog exists (it is created by the first
      AUTOINCREMENT table in the file).

    With ``reset_identity=False`` every backend uses plain ``DELETE`` except
    PostgreSQL, whose TRUNCATE can keep the sequence where it is.
    """
    names = _names(tables)
    if not names:
        return
    dialect = db.config.database_type

    if dialect is DatabaseType.POSTGRESQL:
        identity = "RESTART IDENTITY" if reset_identity else "CONTINUE IDENTITY"
        db.execute(f"TRUNCATE {', '.join(reversed(names))} {identity} CASCADE")
        return

    sqlite_sequence = (dialect is DatabaseType.SQLITE and reset_identity
                       and db.table_exists("sqlite_sequence"))

    for table in reversed(names):
        if not reset_identity:
            db.execute(f"DELETE FROM {table}")
        elif dialect is DatabaseType.MYSQL:
            if _mysql_is_referenced(db, table):
                db.execute(f"DELETE FROM {table}")
                db.execute(f"ALTER TABLE {table} AUTO_INCREMENT = 1")
            else:
                db.execute(f"TRUNCATE TABLE {table}")
        elif dialect is DatabaseType.MSSQL:
            if _mssql_is_referenced(db, table):
                db.execute(f"DELETE FROM {table}")
                if mssql_has_identity(db, table):
                    db.execute(f"DBCC CHECKIDENT ('{table}', RESEED, 0)")
            else:
                db.execute(f"TRUNCATE TABLE {table}")
        else:
            db.execute(f"DELETE FROM {table}")
            if sqlite_sequence:
                db.execute("DELETE FROM sqlite_sequence WHERE name = :name",
                           {"name": table})


def _names(tables: Union[str, Iterable[str]]) -> List[str]:
    if isinstance(tables, str):
        return [tables]
    return list(tables)


# The catalog is consulted rather than trying TRUNCATE and catching the
# failure: execute() rolls the connection back on any error, which inside a
# caller's transaction would silently discard everything done so far.

def _mysql_is_referenced(db: 'DatabaseManager', table: str) -> bool:
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE REFERENCED_TABLE_NAME = :table AND CONSTRAINT_SCHEMA = DATABASE()",
        {"table": table})
    return bool(row and row["n"])


def _mssql_is_referenced(db: 'DatabaseManager', table: str) -> bool:
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM sys.foreign_keys "
        "WHERE referenced_object_id = OBJECT_ID(:table)",
        {"table": table})
    return bool(row and row["n"])


def mssql_has_identity(db: 'DatabaseManager', table: str) -> bool:
    """True if a SQL Server table has an identity column."""
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM sys.identity_columns "
        "WHERE object_id = OBJECT_ID(:table)",
        {"table": table})
    return bool(row and row["n"])
