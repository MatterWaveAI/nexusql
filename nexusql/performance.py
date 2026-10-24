"""
Query performance tools: slow-query logging, EXPLAIN, and per-statement stats.

Three small pieces that share one idea, timing and inspecting the statements
a manager runs without changing how they run:

- ``QueryMonitor`` sits behind ``DatabaseManager`` and ``AsyncPostgresBackend``.
  Each query method is wrapped by ``timed`` / ``timed_async``, which takes one
  ``perf_counter`` pair around the call and hands the result to the monitor.
  When no monitor is configured the wrapper is a single attribute check, so a
  manager built without these options pays nothing.
- ``explain`` turns a statement into the backend's own plan request and
  returns the backend's rows plus a normalised ``plan`` text column, so a
  caller can print one thing on every backend.
- Per-statement stats aggregate behind a lock, keyed by the SQL with its
  whitespace collapsed, so the same query written across different lines
  counts as one.

Slow-query logging deliberately leaves parameter values out unless asked:
they tend to hold the very data (emails, names, tokens) a log must not keep.
"""

import functools
import logging
import re
import threading
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .interfaces import DatabaseType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timing records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueryTiming:
    """
    One timed statement, as handed to the ``on_slow_query`` callback.

    ``sql`` and ``params`` are exactly what the caller passed, before dialect
    translation. ``rows`` is the number of rows the call returned, or None
    when that is not known: a failed statement, or ``execute(...,
    return_id=True)`` which returns a key rather than rows.
    """
    sql: str
    params: Optional[Dict[str, Any]]
    duration_ms: float
    backend: str
    rows: Optional[int]


@dataclass(frozen=True)
class QueryStat:
    """Aggregate for one normalised SQL string, from ``query_stats()``."""
    sql: str
    count: int
    total_ms: float
    max_ms: float

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


def normalise_sql(sql: str) -> str:
    """Collapse runs of whitespace so formatting differences share a stat."""
    return ' '.join(sql.split())


def row_count(result: Any) -> Optional[int]:
    """
    Rows a query method returned, across the shapes the two backends use.

    Lists of rows and ``QueryResult.data`` count directly; a single row
    (``fetch_one``) is 1 and its None is 0. Anything else, such as the key
    from ``return_id=True``, has no row count.
    """
    if result is None:
        return 0
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        return 1
    data = getattr(result, 'data', None)
    if isinstance(data, list):
        return len(data)
    return None


class QueryMonitor:
    """
    Timing sink shared by the sync manager and the async backend.

    Holds the slow-query settings and, when ``collect_stats`` is on, the
    per-statement aggregates. ``record`` is safe to call from any thread:
    the stats dict is guarded by a lock, and the log call and callback run
    inline in the calling thread.
    """

    def __init__(
        self,
        backend: str,
        slow_query_threshold_ms: Optional[float] = None,
        on_slow_query: Optional[Callable[[QueryTiming], None]] = None,
        log_params: bool = False,
        collect_stats: bool = False,
    ):
        if slow_query_threshold_ms is not None and slow_query_threshold_ms < 0:
            raise ValueError("slow_query_threshold_ms must be >= 0 or None")
        self.backend = backend
        self.slow_query_threshold_ms = slow_query_threshold_ms
        self.on_slow_query = on_slow_query
        self.log_params = log_params
        self.collect_stats = collect_stats
        self._lock = threading.Lock()
        # sql -> [count, total_ms, max_ms]; built into QueryStat on read so
        # the hot path only touches three numbers.
        self._stats: Dict[str, List[float]] = {}

    @classmethod
    def for_options(
        cls,
        backend: str,
        slow_query_threshold_ms: Optional[float],
        on_slow_query: Optional[Callable[[QueryTiming], None]],
        log_params: bool,
        collect_stats: bool,
    ) -> Optional["QueryMonitor"]:
        """A monitor when any option asks for one, else None so timing is skipped."""
        if slow_query_threshold_ms is None and not collect_stats:
            return None
        return cls(backend, slow_query_threshold_ms, on_slow_query, log_params,
                   collect_stats)

    def record(self, sql: str, params: Optional[Dict[str, Any]],
               duration_ms: float, rows: Optional[int]) -> None:
        """Account for one finished statement."""
        if self.collect_stats:
            key = normalise_sql(sql)
            with self._lock:
                entry = self._stats.get(key)
                if entry is None:
                    self._stats[key] = [1, duration_ms, duration_ms]
                else:
                    entry[0] += 1
                    entry[1] += duration_ms
                    if duration_ms > entry[2]:
                        entry[2] = duration_ms

        threshold = self.slow_query_threshold_ms
        if threshold is None or duration_ms < threshold:
            return

        if self.log_params:
            logger.warning(
                "Slow query (%.1f ms, %s): %s params=%r",
                duration_ms, self.backend, normalise_sql(sql), params,
            )
        else:
            logger.warning(
                "Slow query (%.1f ms, %s): %s",
                duration_ms, self.backend, normalise_sql(sql),
            )

        if self.on_slow_query is not None:
            self.on_slow_query(QueryTiming(
                sql=sql, params=params, duration_ms=duration_ms,
                backend=self.backend, rows=rows,
            ))

    def query_stats(self) -> List[QueryStat]:
        """Aggregates so far, slowest total first."""
        with self._lock:
            stats = [
                QueryStat(sql=sql, count=int(c), total_ms=t, max_ms=m)
                for sql, (c, t, m) in self._stats.items()
            ]
        stats.sort(key=lambda s: s.total_ms, reverse=True)
        return stats

    def reset_query_stats(self) -> None:
        with self._lock:
            self._stats.clear()


def _params_from_call(args: tuple, kwargs: dict) -> Optional[Dict[str, Any]]:
    """The ``params`` argument of an execute / fetch_one call, however passed."""
    if 'params' in kwargs:
        return kwargs['params']
    return args[0] if args else None


def timed(method):
    """
    Wrap a sync query method so its duration reaches ``self._monitor``.

    A failed statement is still recorded (with ``rows=None``): the statement
    that fails by running into a timeout is the slow query most worth seeing.
    """
    @functools.wraps(method)
    def wrapper(self, query, *args, **kwargs):
        monitor = getattr(self, '_monitor', None)
        if monitor is None:
            return method(self, query, *args, **kwargs)
        started = perf_counter()
        try:
            result = method(self, query, *args, **kwargs)
        except BaseException:
            monitor.record(query, _params_from_call(args, kwargs),
                           (perf_counter() - started) * 1000.0, None)
            raise
        monitor.record(query, _params_from_call(args, kwargs),
                       (perf_counter() - started) * 1000.0, row_count(result))
        return result
    return wrapper


def timed_async(method):
    """Async counterpart of ``timed`` for the async backend's ``execute``."""
    @functools.wraps(method)
    async def wrapper(self, query, *args, **kwargs):
        monitor = getattr(self, '_monitor', None)
        if monitor is None:
            return await method(self, query, *args, **kwargs)
        started = perf_counter()
        try:
            result = await method(self, query, *args, **kwargs)
        except BaseException:
            monitor.record(query, _params_from_call(args, kwargs),
                           (perf_counter() - started) * 1000.0, None)
            raise
        monitor.record(query, _params_from_call(args, kwargs),
                       (perf_counter() - started) * 1000.0, row_count(result))
        return result
    return wrapper


# ---------------------------------------------------------------------------
# EXPLAIN
# ---------------------------------------------------------------------------

# Keywords that make a statement unsafe to run for analysis. A plain
# ``EXPLAIN`` only plans, but ``analyze=True`` executes, and the effects of
# an executed write (rows, sequences, DDL) cannot be undone the same way on
# every backend, so analysis is limited to statements that only read.
_WRITE_KEYWORDS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE)\b',
    re.IGNORECASE,
)
_COMMENT = re.compile(r'--[^\n]*|/\*.*?\*/', re.DOTALL)


def check_analyzable(query: str) -> None:
    """
    Raise ValueError unless ``query`` is a read-only SELECT (optionally led
    by a WITH clause that itself only reads).
    """
    bare = _COMMENT.sub(' ', query).strip()
    first = bare.split(None, 1)[0].upper() if bare else ''
    if first not in ('SELECT', 'WITH') or _WRITE_KEYWORDS.search(bare):
        raise ValueError(
            "explain(analyze=True) executes the statement, so only SELECT "
            f"statements are accepted; got: {query.strip()[:80]!r}"
        )


def explain_sql(db_type: DatabaseType, query: str, analyze: bool) -> str:
    """
    The statement that asks ``db_type`` for the plan of ``query``.

    SQL Server is not handled here: its plan comes from a session setting
    wrapped around the unchanged statement, see ``explain``.
    """
    body = query.strip().rstrip(';').strip()
    if db_type == DatabaseType.POSTGRESQL:
        prefix = 'EXPLAIN (ANALYZE, FORMAT TEXT) ' if analyze else 'EXPLAIN '
    elif db_type == DatabaseType.SQLITE:
        # SQLite has no executing EXPLAIN; the plan is the same either way.
        prefix = 'EXPLAIN QUERY PLAN '
    elif db_type == DatabaseType.MYSQL:
        prefix = 'EXPLAIN ANALYZE ' if analyze else 'EXPLAIN '
    else:
        raise NotImplementedError(f"explain() is not supported for {db_type}")
    return prefix + body


def rows_as_dicts(rows: Iterable[Any], description: Optional[Sequence]) -> List[Dict[str, Any]]:
    """
    Driver rows as plain dicts.

    psycopg (dict_row) and pymysql (DictCursor) rows already are dicts;
    sqlite3.Row has ``keys()``; pyodbc rows are tuples named only through
    ``cursor.description``.
    """
    columns = [col[0] for col in description] if description else None
    out = []
    for row in rows:
        if isinstance(row, dict) or hasattr(row, 'keys'):
            out.append(dict(row))
        elif columns is not None:
            out.append(dict(zip(columns, row)))
        else:
            out.append({str(i): v for i, v in enumerate(row)})
    return out


def plan_text(db_type: DatabaseType, row: Dict[str, Any]) -> str:
    """
    One line of plan for a backend row, the value of the ``plan`` column.

    Each backend reports its plan in a differently named column: PostgreSQL
    ``QUERY PLAN``, SQLite ``detail``, MySQL ``EXPLAIN`` (tree or ANALYZE
    output) or a table of columns, SQL Server ``StmtText`` or an XML
    showplan column. The tabular shapes are flattened to ``name=value``.
    """
    if db_type == DatabaseType.POSTGRESQL and 'QUERY PLAN' in row:
        return str(row['QUERY PLAN'])
    if db_type == DatabaseType.SQLITE and 'detail' in row:
        return str(row['detail'])
    if db_type == DatabaseType.MYSQL and 'EXPLAIN' in row:
        return str(row['EXPLAIN'])
    if db_type == DatabaseType.MSSQL:
        if 'StmtText' in row:
            return str(row['StmtText'])
        for name, value in row.items():
            if 'Showplan' in name:
                return str(value)
    if len(row) == 1:
        return str(next(iter(row.values())))
    return ' '.join(f"{k}={v}" for k, v in row.items() if v is not None)


def with_plan(db_type: DatabaseType, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add the normalised ``plan`` column to each backend row."""
    return [{**row, 'plan': plan_text(db_type, row)} for row in rows]


def _fetch_dicts(cursor) -> List[Dict[str, Any]]:
    rows = cursor.fetchall() if cursor.description else []
    return rows_as_dicts(rows, cursor.description)


def _mssql_showplan_sets(cursor) -> List[Dict[str, Any]]:
    """
    Collect the showplan result sets pyodbc returns under STATISTICS XML.

    The statement's own rows come first and are discarded; the plan follows
    as its own result set with a single ``... XML Showplan`` column.
    """
    plans: List[Dict[str, Any]] = []
    while True:
        description = cursor.description
        if description and any('Showplan' in (col[0] or '') for col in description):
            plans.extend(rows_as_dicts(cursor.fetchall(), description))
        if not cursor.nextset():
            break
    return plans


def explain(manager, query: str, params: Optional[Dict[str, Any]],
            analyze: bool) -> List[Dict[str, Any]]:
    """
    Implementation of ``DatabaseManager.explain``.

    Runs on a borrowed connection through the manager's own translation and
    parameter conversion, so the planned statement is the one ``execute``
    would send. Outside a caller's transaction the connection is rolled back
    afterwards: a plan is never worth keeping a transaction open for, and
    on SQL Server it also discards whatever an analysed SELECT touched.
    """
    db_type = manager.config.database_type
    if analyze:
        check_analyzable(query)

    with manager._borrow() as conn:
        try:
            if db_type == DatabaseType.MSSQL:
                rows = _explain_mssql(manager, conn, query, params, analyze)
            else:
                cursor = manager._execute_raw(
                    explain_sql(db_type, query, analyze), params, conn)
                try:
                    rows = _fetch_dicts(cursor)
                finally:
                    cursor.close()
        finally:
            if not manager._in_transaction:
                try:
                    conn.rollback()
                except Exception:
                    pass
    return with_plan(db_type, rows)


def _explain_mssql(manager, conn, query: str, params, analyze: bool) -> List[Dict[str, Any]]:
    """
    SQL Server plans come from a session setting, not a statement prefix.

    ``SET SHOWPLAN_ALL ON`` makes the server return the estimated plan for
    each following statement instead of running it; ``SET STATISTICS XML
    ON`` runs the statement and appends the actual plan as an extra result
    set. Both are session-wide, so the setting is switched off again on the
    same connection whatever happens, or every later statement on that
    pooled connection would come back as a plan.
    """
    setting = 'STATISTICS XML' if analyze else 'SHOWPLAN_ALL'
    control = conn.cursor()
    try:
        control.execute(f"SET {setting} ON")
    finally:
        control.close()
    try:
        cursor = manager._execute_raw(query, params, conn)
        try:
            if analyze:
                return _mssql_showplan_sets(cursor)
            return _fetch_dicts(cursor)
        finally:
            cursor.close()
    finally:
        control = conn.cursor()
        try:
            control.execute(f"SET {setting} OFF")
        finally:
            control.close()
