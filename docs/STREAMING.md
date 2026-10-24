# Streaming Results

Status: implemented. `DatabaseManager.stream()` / `stream_chunks()` and the
same pair on `AsyncPostgresBackend`. The README has the usage summary; this
is the design record.

## Problem

`execute()` calls `cursor.fetchall()` and returns a list, so a query that
matches a million rows builds a million dicts before the caller sees the
first one. Exports, backfills and report jobs need to walk a large result
with memory proportional to a chunk, not to the table.

## API

```python
db.stream(query, params=None, chunk_size=1000)         # -> Iterator[dict]
db.stream_chunks(query, params=None, chunk_size=1000)  # -> Iterator[list[dict]]

await backend.stream(...)         # async generator of dict (AsyncPostgresBackend)
await backend.stream_chunks(...)  # async generator of list[dict]
```

Both take the same `:name` parameters and go through the same SQL
translation as `execute()`. `stream()` yields one dict per row in result
order; `stream_chunks()` yields each fetched batch as a list, for callers
that work in batches anyway.

Argument errors (`chunk_size < 1`, `execute("BEGIN")`-style transaction
statements) raise at the call. Everything else is lazy: calling `stream()`
runs nothing and borrows nothing. The query runs on the first `next()`,
which is also when the connection is borrowed.

## Connection lifetime

The iterator holds one connection from the first row to the end. It is
released by every exit route:

| How the stream ends | What happens before release |
|---|---|
| Rows run out | cursor closed, `commit()` (ends the server-cursor transaction on PostgreSQL) |
| `break` / `close()` | same as exhaustion: a stopped read is not a failure |
| Exception | cursor closed, `rollback()`, then raise; the pool gets a clean connection |

The commit after a successful stream is what lets a PostgreSQL connection go
back to the pool idle rather than "idle in transaction".

With `pool_max=1`, a second query on a server backend while a stream is open
waits up to `pool_timeout` seconds for the stream to release the connection
and then raises `PoolTimeout`; that is the pool working as designed, not a
leak. Size the pool for the streams you run concurrently. SQLite is unpooled,
so there a second statement shares the stream's connection.

### Inside `transaction()`

The stream uses the pinned connection and commits nothing; the block owns
the commit. The stream sees the transaction's uncommitted writes. Consume
or close the stream before the block ends: a stream that outlives its
transaction is iterating a cursor whose transaction has been committed or
rolled back, and on PostgreSQL the server cursor no longer exists.

## Per backend

| Backend | Cursor | Notes |
|---|---|---|
| PostgreSQL | psycopg named (server-side) cursor, `FETCH FORWARD chunk_size` per round-trip | The server does not send rows ahead of the fetches. `DECLARE CURSOR` needs a transaction block; psycopg opens one implicitly, and if the connection was switched to autocommit the stream issues `BEGIN` itself. Only `SELECT`/`VALUES`/`WITH ... SELECT` can be declared as a cursor, so `UPDATE ... RETURNING` cannot be streamed here; use `execute()`. |
| SQLite | plain cursor, `fetchmany` | The driver reads rows from the file as they are requested. Row-level errors (a user function raising) surface in the chunk that reaches them. |
| MySQL | pymysql `SSDictCursor` (unbuffered) | Rows are read from the socket as fetched. Closing early makes pymysql read and discard the rest of the result, because the protocol cannot abandon a result set without dropping the connection; an early `break` on a huge result still costs the network transfer. |
| SQL Server | pyodbc cursor, `fetchmany` | Forward-only cursors stream from the server. Rows are tuples and are converted through `cursor.description`, as `execute()` does. |

A statement that produces no result set (a write without `RETURNING`)
streams nothing and commits, so nothing special happens if one is passed by
mistake; `execute()` remains the right call for it.

## Async backend

`AsyncPostgresBackend.stream()` uses psycopg's `AsyncServerCursor` with the
same commit/rollback rules. The difference is who closes the generator on an
early exit: an `async for` loop that `break`s does not close an async
generator; that happens when the event loop's finalizer gets to it. To
release the connection deterministically, iterate inside
`contextlib.aclosing()` or call `aclose()`:

```python
async with contextlib.aclosing(db.stream("SELECT ...")) as rows:
    async for row in rows:
        if done(row):
            break
```

Exhaustion and errors release the connection on their own, as in the sync
path.

## Memory profile

`execute()` holds the full result as a list of dicts plus whatever the
driver buffered (psycopg's default cursor buffers the entire result from
the server before returning). `stream()` holds at most one chunk of driver
rows and the dicts built from it; the previous chunk is garbage as soon as
the consumer moves on. For a row of a few hundred bytes the default chunk
of 1000 is on the order of a megabyte however many rows match. A larger
chunk means fewer round-trips; a smaller one means less memory and a
shorter wait for the first row.

## Not done

- Scrollable or `WITH HOLD` cursors on PostgreSQL. Both change the cost
  model (`WITH HOLD` materialises the result at commit), and neither is
  needed for forward iteration.
- A stream on `execute_async()` for the sync manager. The sync iterator
  cannot be offloaded with `asyncio.to_thread` one `next()` at a time
  without pinning the SQLite connection to a thread it did not create; the
  async PostgreSQL backend covers the async case.
- Streaming a `COPY` on PostgreSQL. A different protocol from a cursor and a
  different feature.
