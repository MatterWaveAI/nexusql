"""
Savepoints for nested ``transaction()`` blocks.

A ``with db.transaction():`` block opened inside another used to join the
outer transaction, so a failure in the inner block could only be undone by
rolling back everything. With a savepoint the inner block's statements can
be undone on their own and the outer block decides whether to carry on.

The statements differ by dialect. SQLite, PostgreSQL and MySQL share the SQL
standard's ``SAVEPOINT`` / ``RELEASE SAVEPOINT`` / ``ROLLBACK TO SAVEPOINT``.
SQL Server spells them ``SAVE TRANSACTION`` / ``ROLLBACK TRANSACTION name``
and has no release: a savepoint simply disappears when the transaction ends.

Both the sync manager and the async PostgreSQL backend build their statement
lists here so the two paths cannot drift apart.
"""

from dataclasses import dataclass, field
from typing import List

from .interfaces import DatabaseType

# How a nested transaction() block relates to the one enclosing it.
NESTED_MODES = ("savepoint", "join")

# pyodbc in manual-commit mode runs SQL Server with IMPLICIT_TRANSACTIONS ON:
# a transaction starts when one of a fixed list of statements (INSERT, SELECT
# from a table, DELETE, ...) runs while none is active. SAVE TRANSACTION is
# not on that list and fails with "no active transaction" when the nested
# block is the first thing the outer block does. An explicit BEGIN
# TRANSACTION would be worse: in implicit mode it starts two transactions,
# and the driver's single COMMIT then leaves one open. Assigning a variable
# with SELECT only counts on a brand-new connection; after the first commit
# or rollback it no longer opens anything (verified against SQL Server 2022).
# An INSERT into a table variable starts exactly one transaction every time,
# returns no result set, and touches no real table.
MSSQL_ENSURE_TRANSACTION = (
    "IF @@TRANCOUNT = 0 BEGIN DECLARE @nexusql_tx TABLE (x INT); "
    "INSERT INTO @nexusql_tx VALUES (1); END"
)


def validate_nested(nested: str) -> str:
    """Return ``nested`` if it names a known mode, else raise ValueError."""
    if nested not in NESTED_MODES:
        raise ValueError(
            f"transaction(nested={nested!r}): expected one of {NESTED_MODES}"
        )
    return nested


def savepoint_name(depth: int) -> str:
    """Name of the savepoint for a block nested at ``depth`` (outermost is 1)."""
    return f"sp_{depth}"


@dataclass(frozen=True)
class SavepointSQL:
    """
    Statements that drive one savepoint, in the target dialect.

    ``begin`` runs on entry to the nested block, ``release`` on clean exit,
    and ``rollback`` when the block raises (followed by ``release``, so the
    name can be reused). Any list may be empty where the dialect has no
    such statement.
    """

    name: str
    begin: List[str] = field(default_factory=list)
    release: List[str] = field(default_factory=list)
    rollback: List[str] = field(default_factory=list)


def savepoint_sql(db_type: DatabaseType, name: str, *,
                  transaction_open: bool = True) -> SavepointSQL:
    """
    Build the savepoint statements for ``db_type``.

    ``transaction_open`` says whether the pinned connection already has a
    transaction in progress. SQLite needs to know: a SAVEPOINT issued outside
    a transaction opens one, and releasing that savepoint then commits, which
    would hand the outer block's work to the database before the outer block
    has decided anything. psycopg and pymysql always have a transaction open
    on a non-autocommit connection, and SQL Server is handled inside the SQL.
    """
    if db_type == DatabaseType.MSSQL:
        return SavepointSQL(
            name=name,
            begin=[MSSQL_ENSURE_TRANSACTION, f"SAVE TRANSACTION {name}"],
            release=[],
            rollback=[f"ROLLBACK TRANSACTION {name}"],
        )

    begin = [f"SAVEPOINT {name}"]
    if db_type == DatabaseType.SQLITE and not transaction_open:
        begin.insert(0, "BEGIN")
    return SavepointSQL(
        name=name,
        begin=begin,
        release=[f"RELEASE SAVEPOINT {name}"],
        rollback=[f"ROLLBACK TO SAVEPOINT {name}"],
    )


def transaction_open(db_type: DatabaseType, conn) -> bool:
    """
    Whether ``conn`` has a transaction in progress.

    Only SQLite can answer (``sqlite3.Connection.in_transaction``) and only
    SQLite needs to; see ``savepoint_sql``.
    """
    if db_type == DatabaseType.SQLITE:
        return bool(getattr(conn, 'in_transaction', True))
    return True


def run_statements(conn, statements: List[str]) -> None:
    """Run each statement on a fresh cursor of a DB-API connection."""
    for sql in statements:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
        finally:
            cursor.close()


async def run_statements_async(conn, statements: List[str]) -> None:
    """Async twin of ``run_statements`` for a psycopg AsyncConnection."""
    for sql in statements:
        async with conn.cursor() as cursor:
            await cursor.execute(sql)
