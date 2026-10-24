"""
Async PostgreSQL backend, implemented on psycopg's async API.

The sync ``DatabaseManager`` blocks: called from inside ``async def`` code
(FastAPI routes, SSE streams) a blocking query stalls the event loop. This
backend is async end to end.

It uses the same driver as the sync path. psycopg supports both styles, so
there is one PostgreSQL dependency rather than two, and both paths share the
``%(name)s`` parameter style. That removes the separate ``:name`` to ``$n``
conversion the previous asyncpg implementation needed.

Design notes
------------
- Pooling: ``psycopg_pool.AsyncConnectionPool``, created by ``connect()`` and
  closed by ``disconnect()``. The pool checks connections on checkout, so a
  connection dropped by a server restart or an idle timeout is replaced
  rather than handed to the caller.
- Commit semantics mirror the sync path: a statement run outside an explicit
  transaction is committed as soon as it succeeds. Inside
  ``async with backend.transaction():`` the block owns the commit.
- Failed queries: a statement that fails leaves the session in an aborted
  transaction state, so the connection is rolled back before it returns to
  the pool. One bad query must not poison a pooled connection.

Requires the ``postgresql`` extra: ``pip install nexusql[postgresql]``
"""

import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from .interfaces import (
    ConnectionConfig,
    DatabaseInterface,
    DatabaseType,
    QueryResult,
    create_error_result,
    create_query_result,
)
from .manager import (
    DatabaseManager,
    PARAM_TOKEN,
    PG_PRIMARY_KEY_SQL,
    first_value,
    insert_target_table,
    is_insert_statement,
    reject_transaction_statement,
    single_primary_key,
    with_returning,
)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool, PoolTimeout
    # A stubbed-out sys.modules entry imports cleanly but is unusable, so
    # check the module actually carries the API before claiming support.
    PSYCOPG_AVAILABLE = hasattr(psycopg, 'AsyncConnection')
except ImportError:  # pragma: no cover - exercised only without the extra
    psycopg = None  # type: ignore[assignment]
    PSYCOPG_AVAILABLE = False

logger = logging.getLogger(__name__)


def _translate_sql_for_postgres(sql: str) -> str:
    """
    Translate SQLite-isms to PostgreSQL syntax.

    PostgreSQL is the canonical source dialect, so only SQLite-specific
    constructs need rewriting. Delegates to the sync manager's translator,
    which does not use instance state, so both paths translate identically.
    """
    return DatabaseManager._translate_sql(_PG_STUB, sql)


def _split_sql_statements(script: str) -> List[str]:
    """
    Split a SQL script into statements.

    Delegates to ``DatabaseManager._split_sql_statements``, which does not
    use instance state, so the same splitter backs both paths.
    """
    return DatabaseManager._split_sql_statements(None, script)  # type: ignore[arg-type]


def _has_sql_content(statement: str) -> bool:
    """True if the statement contains anything besides comments/whitespace."""
    test = re.sub(r'--[^\n]*', '', statement)
    test = re.sub(r'/\*.*?\*/', '', test, flags=re.DOTALL)
    return bool(test.strip())


class _PgStub:
    """Carries just enough config for the sync translator to target PostgreSQL."""

    def __init__(self):
        self.config = ConnectionConfig(
            database_type=DatabaseType.POSTGRESQL,
            database_url="postgresql://",
        )


_PG_STUB = _PgStub()


class AsyncPostgresBackend(DatabaseInterface):
    """
    Async PostgreSQL backend with connection pooling.

    Satisfies the ``DatabaseInterface`` ABC (connect / disconnect /
    fetch_all / fetch_one / table_exists / health_check) and adds
    ``execute`` / ``execute_async`` / ``execute_script`` / ``transaction``
    with the same call conventions as the sync ``DatabaseManager``.

    Result conventions match the sync manager: ``fetch_one`` / ``fetch_all``
    / ``execute`` / ``execute_async`` raise on query errors, since a loud
    failure beats silently missing rows. ``execute_script`` returns a
    ``QueryResult`` with ``success=False`` on failure.

    Note that ``fetch_all`` is kept here although the sync manager dropped
    it: this class implements ``DatabaseInterface``, which declares it.
    """

    def __init__(
        self,
        connection_string: str,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
    ):
        """
        Args:
            connection_string: PostgreSQL URL
            min_pool_size: Connections opened eagerly by connect()
            max_pool_size: Ceiling on concurrent connections
            timeout: Seconds to wait for a free connection before raising
            connect_timeout: Seconds connect() waits for the pool to open
                before reporting the server unreachable
        """
        if not connection_string.lstrip().lower().startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError(
                "AsyncPostgresBackend requires a PostgreSQL URL, got: "
                f"{connection_string!r}"
            )
        super().__init__(connection_string, DatabaseType.POSTGRESQL)
        self.config = ConnectionConfig.from_url(connection_string)
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._pool = None
        self._pinned_conn = None
        self._depth = 0
        # Primary-key column per table, for execute(..., return_id=True).
        self._pk_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _require_driver(self) -> None:
        if not PSYCOPG_AVAILABLE:
            raise RuntimeError(
                "psycopg is not installed. Install with: "
                "pip install nexusql[postgresql]"
            )

    async def connect(self) -> bool:
        """
        Create the connection pool. Safe to call repeatedly.

        Returns False, rather than raising, when the server cannot be
        reached within ``connect_timeout`` seconds: the same contract as
        ``DatabaseManager.connect``, so an unreachable database is reported
        here and not on the first query.
        """
        self._require_driver()
        if self._pool is not None:
            return True

        pool = AsyncConnectionPool(
            self.connection_string,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            timeout=self._timeout,
            kwargs={"row_factory": dict_row},
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        try:
            await pool.open(wait=True, timeout=self._connect_timeout)
        except Exception as exc:
            logger.error(f"Failed to connect to database: {exc}")
            try:
                await pool.close()
            except Exception:
                pass
            return False

        self._pool = pool
        self.is_connected = True
        logger.info("AsyncPostgresBackend pool created")
        return True

    async def disconnect(self) -> None:
        """Close the pool and every connection in it."""
        if self._pool is None:
            return
        pool, self._pool = self._pool, None
        self._pinned_conn = None
        self._depth = 0
        self.is_connected = False
        try:
            await pool.close()
        except Exception:
            logger.debug("pool close failed", exc_info=True)
        logger.info("AsyncPostgresBackend pool closed")

    @property
    def pool(self):
        """The underlying pool, for callers that need driver-level access."""
        if self._pool is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._pool

    async def __aenter__(self) -> "AsyncPostgresBackend":
        if not await self.connect():
            # connect() logged the cause; a context manager cannot hand
            # back a backend with no pool.
            raise ConnectionError("AsyncPostgresBackend could not connect")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _ensure_pool(self):
        if self._pool is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._pool

    @asynccontextmanager
    async def _borrow(self):
        """
        Yield a connection for one operation.

        Inside a transaction this is the pinned connection, which is not
        returned to the pool until the transaction ends. Outside one, a
        connection is checked out and released when the operation finishes.
        """
        if self._pinned_conn is not None:
            yield self._pinned_conn
            return

        pool = self._ensure_pool()
        try:
            conn = await pool.getconn(timeout=self._timeout)
        except PoolTimeout as exc:
            raise ConnectionError(str(exc)) from exc

        try:
            yield conn
        except Exception:
            # A failed statement aborts the session's transaction; roll back
            # so the connection is clean for whoever gets it next.
            try:
                await conn.rollback()
            except Exception:
                pass
            raise
        finally:
            await pool.putconn(conn)

    def _prepare(self, query: str, params: Optional[Dict[str, Any]]):
        """
        Translate to the PostgreSQL dialect and rewrite ``:name`` parameters
        to psycopg's ``%(name)s``; the params dict passes through unchanged.

        Delegates to the sync manager's converter so both paths read ``:name``
        and ``::cast`` identically.
        """
        translated = _translate_sql_for_postgres(query)
        return DatabaseManager._convert_params(_PG_STUB, translated, params)

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        return_id: bool = False,
    ) -> Any:
        """
        Execute a query, returning rows for statements that produce them.

        Writes outside an explicit transaction commit as soon as they
        succeed, matching the sync manager.

        With ``return_id=True`` the statement must be an INSERT and the
        generated primary-key value of the new row is returned (None when
        no row was inserted), as ``DatabaseManager.execute`` does. An INSERT
        that already has a RETURNING clause yields its first column; otherwise
        the table's single-column primary key is looked up and returned.
        """
        reject_transaction_statement(query)

        if return_id:
            return await self._execute_return_id(query, params)

        sql, args = self._prepare(query, params)

        async with self._borrow() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                rows = await cur.fetchall() if cur.description else []

            if self._pinned_conn is None:
                await conn.commit()

            return [dict(r) for r in rows]

    async def execute_async(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
        return_id: bool = False,
    ) -> Any:
        """Alias for ``execute``; this backend is async throughout."""
        return await self.execute(query, params, return_id=return_id)

    async def _execute_return_id(
        self, query: str, params: Optional[Dict[str, Any]]
    ) -> Any:
        """Run an INSERT and return the key generated for the new row."""
        if not is_insert_statement(query):
            raise ValueError(
                "return_id=True only applies to INSERT statements, got: "
                f"{query.strip()[:80]!r}"
            )

        if re.search(r'\bRETURNING\b', query, re.IGNORECASE) is None:
            table = insert_target_table(query)
            async with self._borrow() as conn:
                column = await self._primary_key(table, conn)
            query = with_returning(query, column)

        sql, args = self._prepare(query, params)
        async with self._borrow() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                row = await cur.fetchone()

            if self._pinned_conn is None:
                await conn.commit()

            return first_value(row) if row else None

    async def _primary_key(self, table: str, conn) -> str:
        """Single primary-key column of ``table``, cached per table name."""
        column = self._pk_cache.get(table)
        if column is None:
            async with conn.cursor() as cur:
                await cur.execute(PG_PRIMARY_KEY_SQL, {"table": table})
                rows = await cur.fetchall()
            column = single_primary_key(table, [first_value(r) for r in rows])
            self._pk_cache[table] = column
        return column

    async def fetch_one(
        self, query: str, params: Optional[Dict[str, Any]] = None
    ) -> QueryResult:
        """Fetch the first row, as a QueryResult."""
        try:
            rows = await self.execute(query, params)
        except Exception as exc:
            logger.error(f"fetch_one failed: {exc}")
            raise
        return create_query_result(True, data=rows[:1])

    async def fetch_all(
        self, query: str, params: Optional[Dict[str, Any]] = None
    ) -> QueryResult:
        """Fetch every row, as a QueryResult."""
        try:
            rows = await self.execute(query, params)
        except Exception as exc:
            logger.error(f"fetch_all failed: {exc}")
            raise
        return create_query_result(True, data=rows)

    async def table_exists(self, table_name: str) -> bool:
        """Check whether a table exists in the current schema."""
        try:
            rows = await self.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name = %(table_name)s",
                {"table_name": table_name},
            )
            return bool(rows)
        except Exception as exc:
            logger.error(f"table_exists failed: {exc}")
            return False

    async def execute_script(self, script: str) -> QueryResult:
        """
        Execute a multi-statement SQL script.

        Statements run one at a time inside a single transaction, so a
        failure part-way leaves nothing half-applied.
        """
        try:
            translated = _translate_sql_for_postgres(script)
            statements = [
                s for s in _split_sql_statements(translated)
                if _has_sql_content(s)
            ]

            async with self.transaction():
                for statement in statements:
                    await self.execute(statement)

            return create_query_result(True, data=[])
        except Exception as exc:
            logger.error(f"Script execution failed: {exc}")
            return create_error_result(str(exc))

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        """
        Run a block inside a database transaction.

        Commits on clean exit, rolls back if the block raises. The connection
        is pinned for the duration, so every statement in the block runs on
        the same one. Nested use joins the outermost transaction rather than
        opening a second.

            async with backend.transaction():
                await backend.execute("INSERT INTO ...")
        """
        if self._pinned_conn is not None:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return

        pool = self._ensure_pool()
        try:
            conn = await pool.getconn(timeout=self._timeout)
        except PoolTimeout as exc:
            raise ConnectionError(str(exc)) from exc

        self._pinned_conn = conn
        self._depth = 1
        try:
            yield self
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._pinned_conn = None
            self._depth = 0
            await pool.putconn(conn)

    async def health_check(self) -> bool:
        """True if a trivial query succeeds."""
        try:
            await self.execute("SELECT 1 AS health_check")
            return True
        except Exception:
            return False
