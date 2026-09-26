"""
Connection pooling.

``DatabaseManager`` holds one connection, so concurrent callers serialize
through it. These pools let the manager hand out several connections instead,
which is what makes concurrent use from worker threads actually concurrent.

Three implementations sit behind one interface:

- ``PostgresPool`` wraps psycopg's ``ConnectionPool``
- ``GenericPool`` is a thread-safe pool for drivers that ship none of their
  own, which is the case for pymysql and pyodbc
- ``NullPool`` always returns the same connection, for SQLite

SQLite is deliberately unpooled. Its connections are bound to the thread that
created them, and the target is a local file with no network round-trip to
overlap, so a pool would add breakage without adding throughput.

Nothing here parses or rewrites SQL. PostgreSQL remains the single source
dialect and all translation stays in ``DatabaseManager._translate_sql``.
"""

import logging
import queue
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class PoolTimeout(Exception):
    """Raised when no connection became available within the timeout."""


class ConnectionPool(ABC):
    """
    A source of database connections.

    Callers must return every connection they acquire, normally through
    ``DatabaseManager``'s internal acquire/release helpers rather than
    directly.
    """

    @abstractmethod
    def acquire(self, timeout: Optional[float] = None) -> Any:
        """Check out a connection, waiting up to ``timeout`` seconds."""

    @abstractmethod
    def release(self, conn: Any, *, discard: bool = False) -> None:
        """
        Return a connection to the pool.

        ``discard=True`` destroys it instead, for a connection known to be
        broken or left in an unusable state.
        """

    @abstractmethod
    def close(self) -> None:
        """Close every connection the pool holds."""

    @property
    @abstractmethod
    def size(self) -> int:
        """Number of connections currently open, idle plus checked out."""


class NullPool(ConnectionPool):
    """
    Pool of exactly one connection, for SQLite.

    ``acquire`` always returns the same connection and ``release`` does
    nothing, so the manager can use one code path for every provider without
    special-casing SQLite at each call site.
    """

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._conn: Optional[Any] = None
        self._lock = threading.Lock()

    def acquire(self, timeout: Optional[float] = None) -> Any:
        with self._lock:
            if self._conn is None:
                self._conn = self._factory()
            return self._conn

    def release(self, conn: Any, *, discard: bool = False) -> None:
        if discard:
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @property
    def size(self) -> int:
        return 1 if self._conn is not None else 0


class GenericPool(ConnectionPool):
    """
    Thread-safe connection pool for drivers without one of their own.

    Used for MySQL and MSSQL. Idle connections are kept in a LIFO stack so
    that connections opened for a demand spike age out once it passes,
    rather than being kept warm indefinitely.

    ``validate`` is called on checkout. A connection that fails validation is
    discarded and replaced, which is what keeps a server restart or an idle
    timeout from handing the caller a dead socket.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
        validate: Optional[Callable[[Any], bool]] = None,
    ):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        if min_size < 0 or min_size > max_size:
            raise ValueError("min_size must be between 0 and max_size")

        self._factory = factory
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout
        self._validate = validate

        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._open = 0          # total connections in existence
        self._closed = False

        for _ in range(min_size):
            self._idle.put(self._open_connection())

    def _open_connection(self) -> Any:
        conn = self._factory()
        with self._lock:
            self._open += 1
        return conn

    def _destroy(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._open -= 1

    def _is_usable(self, conn: Any) -> bool:
        if self._validate is None:
            return True
        try:
            return self._validate(conn)
        except Exception:
            return False

    def acquire(self, timeout: Optional[float] = None) -> Any:
        if self._closed:
            raise RuntimeError("Pool is closed")

        deadline = timeout if timeout is not None else self._timeout

        while True:
            conn = None
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                with self._lock:
                    room = self._open < self._max_size
                if room:
                    return self._open_connection()
                # At capacity: wait for someone to return one.
                try:
                    conn = self._idle.get(timeout=deadline)
                except queue.Empty:
                    raise PoolTimeout(
                        f"No connection available within {deadline}s "
                        f"(max_size={self._max_size})"
                    )

            if self._is_usable(conn):
                return conn

            # Dead on checkout: drop it and try again.
            logger.debug("Discarding unusable pooled connection")
            self._destroy(conn)

    def release(self, conn: Any, *, discard: bool = False) -> None:
        if discard or self._closed:
            self._destroy(conn)
            return
        self._idle.put(conn)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._destroy(self._idle.get_nowait())
            except queue.Empty:
                break

    @property
    def size(self) -> int:
        with self._lock:
            return self._open

    @property
    def idle(self) -> int:
        return self._idle.qsize()


class PostgresPool(ConnectionPool):
    """
    Wrapper around psycopg's ``ConnectionPool``.

    psycopg ships a tested pool, so this adapts it to the interface rather
    than reimplementing one. The pool checks connections itself via its
    ``check`` callback, so a connection broken since its last use is replaced
    rather than handed to the caller.

    Discarding works by closing the connection before returning it: the pool
    sees a closed connection and opens a replacement in its place.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        **connect_kwargs: Any,
    ):
        from psycopg_pool import ConnectionPool as _PsycopgPool

        self._timeout = timeout
        self._lock = threading.Lock()
        self._checked_out = 0
        self._pool = _PsycopgPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs=connect_kwargs or None,
            check=_PsycopgPool.check_connection,
            open=False,
            # Without this the pool keeps retrying a dead server for its
            # 300s default, so opening blocks instead of reporting the
            # failure.
            reconnect_timeout=connect_timeout,
        )
        # Wait for the first connections rather than opening in the
        # background, so an unreachable server fails here instead of
        # surfacing later on an unrelated query.
        try:
            self._pool.open(wait=True, timeout=connect_timeout)
        except Exception:
            # Leave nothing running behind a failed open.
            try:
                self._pool.close()
            except Exception:
                pass
            raise

    def acquire(self, timeout: Optional[float] = None) -> Any:
        from psycopg_pool import PoolTimeout as _PsycopgPoolTimeout

        try:
            conn = self._pool.getconn(timeout=timeout or self._timeout)
        except _PsycopgPoolTimeout as exc:
            raise PoolTimeout(str(exc)) from exc

        with self._lock:
            self._checked_out += 1
        return conn

    def release(self, conn: Any, *, discard: bool = False) -> None:
        with self._lock:
            self._checked_out -= 1
        if discard:
            # Closing before handing it back makes the pool replace it.
            try:
                conn.close()
            except Exception:
                pass
        try:
            self._pool.putconn(conn)
        except Exception:
            logger.debug("putconn failed; connection dropped", exc_info=True)

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:
            logger.debug("pool close failed", exc_info=True)

    @property
    def size(self) -> int:
        with self._lock:
            return self._checked_out
