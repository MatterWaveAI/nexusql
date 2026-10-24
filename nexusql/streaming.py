"""
Streaming result sets.

``execute()`` fetches every row before it returns, so a SELECT over a large
table costs memory in proportion to the table. ``stream()`` walks the result
in fixed-size chunks through ``cursor.fetchmany``, so memory is bounded by
``chunk_size`` however many rows match.

The price is a connection held for the life of the iterator. It is borrowed
on the first ``next()`` and handed back when the generator ends: exhausted,
closed early (``break`` out of the loop, or ``close()``), or killed by an
error. On error the connection is rolled back before release, as
``execute()`` does, so a pooled connection is never returned mid-transaction.

Per backend:

- PostgreSQL uses a named (server-side) cursor. psycopg's default cursor
  receives the whole result set from the server before the first row is
  available, which would defeat the point; a named cursor fetches one chunk
  per round-trip. Named cursors live inside a transaction block, so one is
  opened for the stream and committed when it ends. Inside
  ``db.transaction()`` the pinned connection and its transaction are reused
  and the stream commits nothing.
- MySQL uses pymysql's unbuffered ``SSDictCursor``. Closing it early makes
  the driver read the rest of the result off the socket (discarded, not
  stored), because the MySQL protocol has no way to abandon a result set
  short of dropping the connection.
- SQLite and SQL Server use a plain cursor; both drivers already hand rows
  over as they are read.

The implementation lives here rather than in the manager so that each class
only gains thin ``stream`` / ``stream_chunks`` methods.
"""

import logging
import uuid
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

from .interfaces import DatabaseType

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 1000


def _check_chunk_size(chunk_size: int) -> None:
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size!r}")


def _cursor_name() -> str:
    # Server-side cursor names are per connection; a pooled connection may
    # already have one open in another stream, so make each name unique.
    return f"nexusql_stream_{uuid.uuid4().hex}"


def _row_converter(db_type: DatabaseType, cursor) -> Callable[[Any], Dict[str, Any]]:
    """Turn a driver row into a dict, as execute() does for each driver."""
    if db_type == DatabaseType.MSSQL:
        # pyodbc rows are tuples; the column names live on the cursor.
        columns = [column[0] for column in cursor.description]
        return lambda row: dict(zip(columns, row))
    # psycopg (dict_row), pymysql (dict cursors) and sqlite3.Row all accept dict().
    return dict


# ---------------------------------------------------------------------------
# Sync manager
# ---------------------------------------------------------------------------

def _open_cursor(db_type: DatabaseType, conn, in_transaction: bool):
    if db_type == DatabaseType.POSTGRESQL:
        # DECLARE CURSOR is only allowed inside a transaction block. psycopg
        # opens one implicitly before the first statement unless the
        # connection has been switched to autocommit, in which case it has
        # to be opened here; commit() still closes it either way.
        if getattr(conn, "autocommit", False) and not in_transaction:
            conn.execute("BEGIN")
        return conn.cursor(name=_cursor_name())

    if db_type == DatabaseType.MYSQL:
        import pymysql.cursors
        return conn.cursor(pymysql.cursors.SSDictCursor)

    return conn.cursor()


def _finish(conn, cursor, failed: bool, in_transaction: bool) -> None:
    """
    Release what the stream held on the connection.

    The cursor is closed before any commit because SQLite refuses to commit
    while a statement is still in progress. A failed stream is rolled back so
    the connection is clean for the next caller; on PostgreSQL a failed
    statement leaves the session in an aborted transaction state and every
    later statement would fail too. A clean stream outside an explicit
    transaction commits, which ends the transaction the server-side cursor
    needed and lets the connection go back to the pool idle.
    """
    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            logger.debug("stream cursor close failed", exc_info=True)

    if failed:
        try:
            conn.rollback()
        except Exception:
            pass
    elif not in_transaction:
        conn.commit()


def stream_chunks(
    manager,
    query: str,
    params: Optional[Dict[str, Any]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[List[Dict[str, Any]]]:
    """
    Lists of up to ``chunk_size`` rows, in result order.

    Argument errors raise here, at the call; the query itself runs on the
    first ``next()``, which is also when the connection is borrowed.
    """
    _check_chunk_size(chunk_size)
    return _chunks(manager, query, params, chunk_size)


def stream_rows(
    manager,
    query: str,
    params: Optional[Dict[str, Any]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[Dict[str, Any]]:
    """One dict per row; ``chunk_size`` only sets how many are fetched per round-trip."""
    return _flatten(stream_chunks(manager, query, params, chunk_size))


def _chunks(manager, query, params, chunk_size):
    with manager._borrow() as conn:
        db_type = manager.config.database_type
        in_transaction = manager._in_transaction
        sql, args = manager._convert_params(manager._translate_sql(query), params)

        cursor = None
        failed = False
        try:
            cursor = _open_cursor(db_type, conn, in_transaction)
            if args:
                cursor.execute(sql, args)
            else:
                cursor.execute(sql)

            # A statement with no result set (a write without RETURNING)
            # streams nothing; pyodbc would raise on fetchmany otherwise.
            if cursor.description is not None:
                to_dict = _row_converter(db_type, cursor)
                while True:
                    rows = cursor.fetchmany(chunk_size)
                    if not rows:
                        break
                    yield [to_dict(row) for row in rows]
        except Exception:
            # GeneratorExit is not an Exception: an early break is a clean
            # end, and the finally below commits rather than rolls back.
            failed = True
            raise
        finally:
            _finish(conn, cursor, failed, in_transaction)


def _flatten(chunks):
    # A generator expression would not close the chunk generator when the
    # consumer stops early, and it is that close which releases the connection.
    try:
        for chunk in chunks:
            yield from chunk
    finally:
        chunks.close()


# ---------------------------------------------------------------------------
# Async PostgreSQL backend
# ---------------------------------------------------------------------------

def stream_chunks_async(
    backend,
    query: str,
    params: Optional[Dict[str, Any]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> AsyncIterator[List[Dict[str, Any]]]:
    """
    Async counterpart of ``stream_chunks`` on psycopg's AsyncServerCursor.

    Exhaustion and errors release the connection on their own. An early exit
    releases it when the generator is closed, so ``break`` out of the loop
    from inside ``contextlib.aclosing(...)`` or call ``aclose()``; otherwise
    the release waits for the event loop's generator finalizer.
    """
    _check_chunk_size(chunk_size)
    return _achunks(backend, query, params, chunk_size)


def stream_rows_async(
    backend,
    query: str,
    params: Optional[Dict[str, Any]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> AsyncIterator[Dict[str, Any]]:
    """One dict per row; see ``stream_chunks_async`` for the release rules."""
    return _aflatten(stream_chunks_async(backend, query, params, chunk_size))


async def _achunks(backend, query, params, chunk_size):
    sql, args = backend._prepare(query, params)

    async with backend._borrow() as conn:
        pinned = backend._pinned_conn is not None
        if conn.autocommit and not pinned:
            await conn.execute("BEGIN")

        cursor = conn.cursor(name=_cursor_name())
        failed = False
        try:
            await cursor.execute(sql, args)
            while True:
                rows = await cursor.fetchmany(chunk_size)
                if not rows:
                    break
                yield [dict(row) for row in rows]
        except Exception:
            # _borrow rolls an unpinned connection back on the way out, and
            # transaction() does the same for a pinned one, so only the
            # commit decision is made here.
            failed = True
            raise
        finally:
            try:
                await cursor.close()
            except Exception:
                logger.debug("stream cursor close failed", exc_info=True)
            if not failed and not pinned:
                await conn.commit()


async def _aflatten(chunks):
    try:
        async for chunk in chunks:
            for row in chunk:
                yield row
    finally:
        await chunks.aclose()
