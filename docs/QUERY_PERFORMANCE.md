# Query Performance Tools

Three opt-in tools for finding out where the time goes, built so that a
manager configured without them is unchanged: slow-query logging, `explain()`,
and per-statement stats. All of them live in `nexusql/performance.py`; the
`DatabaseManager` and `AsyncPostgresBackend` surface is a handful of
constructor options and three methods.

```python
from nexusql import DatabaseManager, QueryTiming

def alert(t: QueryTiming):
    metrics.timing("db.slow", t.duration_ms, tags={"backend": t.backend})

db = DatabaseManager(
    "postgresql://user:pass@localhost/app",
    slow_query_threshold_ms=200,   # WARNING for anything at or over 200 ms
    on_slow_query=alert,           # optional callback per slow statement
    log_params=False,              # default: keep parameter values out of logs
    collect_stats=True,            # aggregate count / total / max per statement
)
```

The same four options exist on `AsyncPostgresBackend`.

## Slow-query logging

Every `execute` and `fetch_one` (and so `execute_async`, `fetch_one_async`,
and the statements `execute_script` runs on the server backends) is timed
with one `time.perf_counter()` pair. When the duration is at or over
`slow_query_threshold_ms`, a `WARNING` is logged through the
`nexusql.performance` logger (a child of `nexusql`) naming the duration, the
backend and the statement:

```
WARNING nexusql.performance Slow query (312.4 ms, postgresql): SELECT * FROM orders WHERE customer_id = :cid
```

The SQL in the log line is the statement as the caller wrote it, with
whitespace collapsed onto one line. Parameter values are left out unless
`log_params=True`: they tend to hold the very data (emails, names, tokens) a
log must not keep.

`on_slow_query` receives a frozen `QueryTiming(sql, params, duration_ms,
backend, rows)`. `params` is always present there, since the callback is your
own code. `rows` is the number of rows the call returned; it is `None` when
that is not known, which happens for `execute(..., return_id=True)` (which
returns a key, not rows) and for statements that raised. Failed statements
are still timed and reported, because a statement that fails by hitting a
timeout is the slow query most worth seeing.

With `slow_query_threshold_ms=None` (the default) and `collect_stats=False`
nothing is timed at all; the wrapper around each query method is a single
attribute check. `on_slow_query` without a threshold does nothing.

## `explain(query, params=None, analyze=False)`

Returns a list of dicts, one per plan line. Each row carries the backend's
own columns plus a normalised `plan` text column, so

```python
print("\n".join(r["plan"] for r in db.explain(sql, params)))
```

prints a readable plan on every backend. The statement goes through the same
translation and parameter conversion as `execute`, so the plan is for what
would actually run, and `:name` parameters bind the same way.

| Backend | `analyze=False` | `analyze=True` | `plan` column from |
|---------|-----------------|----------------|--------------------|
| PostgreSQL | `EXPLAIN <sql>` | `EXPLAIN (ANALYZE, FORMAT TEXT) <sql>` | `QUERY PLAN` |
| SQLite | `EXPLAIN QUERY PLAN <sql>` | same (SQLite has no executing EXPLAIN) | `detail` |
| MySQL | `EXPLAIN <sql>` | `EXPLAIN ANALYZE <sql>` | `EXPLAIN` when present, else the table flattened to `name=value` |
| SQL Server | `SET SHOWPLAN_ALL ON` around the statement, which is then not executed | `SET STATISTICS XML ON`; the XML showplan result set is returned | `StmtText`, or the `... XML Showplan` column |

On SQL Server the session setting is switched off again on the same
connection whatever happens, including when the statement fails; otherwise
every later statement on that pooled connection would come back as a plan
instead of running.

`analyze=True` executes the statement to measure it. Rather than execute a
write and try to undo it (which cannot be done the same way everywhere: MySQL
commits DDL implicitly, sequences advance regardless, and a caller's open
transaction would have to be rolled back with it), `explain` accepts only
SELECT statements for analysis, including a leading WITH clause that itself
only reads. Anything else raises `ValueError` before anything is sent. A
plain `explain` of a write is fine: it only plans.

Outside a caller's transaction the borrowed connection is rolled back after
the plan is read. A plan is never worth keeping a transaction open for, and
on SQL Server this also discards whatever an analysed SELECT touched. Inside
`with db.transaction():` the transaction is left alone.

`explain` is not itself a timed query method: it does not appear in the
slow-query log or in `query_stats()`.

## `query_stats()` and `reset_query_stats()`

With `collect_stats=True`, each timed statement is aggregated under its SQL
with whitespace collapsed, so the same query written across different lines
counts as one. `query_stats()` returns `QueryStat(sql, count, total_ms,
max_ms)` records, slowest total first, with an `avg_ms` property.
`reset_query_stats()` clears them. Calling `query_stats()` on a manager built
without `collect_stats=True` raises `RuntimeError` rather than returning an
empty list that would look like "no queries ran".

The aggregates sit behind a lock, so a manager shared across threads keeps
accurate counts. Stats collection alone does not log anything; the threshold
controls logging and the callback independently.

## Design notes

- Timing is applied by a decorator (`performance.timed` / `timed_async`) on
  `execute` and `fetch_one`, which keeps the change to `manager.py` to one
  line per method and leaves the method bodies untouched. The decorator
  preserves the signature (`inspect.signature` follows `__wrapped__`).
- On the async backend only `execute` is decorated: `fetch_one`, `fetch_all`,
  `table_exists` and `health_check` all run through it, so each statement is
  timed once.
- `QueryMonitor` is shared between the two backends, so the log format,
  callback payload and stats behave identically on both paths.
