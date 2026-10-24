# Sharding

`ShardedDatabaseManager` puts one manager-shaped object in front of several
databases. It holds a named `DatabaseManager` per shard, decides per statement
which shard runs it, and exposes the same calls as `DatabaseManager`
(`execute`, `fetch_one`, `execute_async`, `fetch_one_async`, `transaction`,
`table_exists`, `execute_script`, `connect`, `disconnect`, context manager),
so code written against one database can be pointed at several without its
query calls changing.

Everything lives in `nexusql/sharding.py`; `DatabaseManager` is untouched.

```python
from nexusql import ShardedDatabaseManager, KeyRouter

db = ShardedDatabaseManager(
    {
        "eu": "postgresql://user:pass@eu-db/app",
        "us": "postgresql://user:pass@us-db/app",
        "ap": "sqlite:///ap.db",
    },
    router=KeyRouter("region", {"eu": "eu", "us": "us", "ap": "ap"}),
    pool_max=5,                      # forwarded to each DatabaseManager built from a URL
)

with db:
    db.execute("CREATE TABLE IF NOT EXISTS users (...)", all_shards=True)

    # Routed by the shard key in params
    db.execute("INSERT INTO users (region, name) VALUES (:region, :name)",
               {"region": "eu", "name": "Ada"})

    # Unrouted read: runs on every shard, rows concatenated in shard order,
    # then ordered and capped client-side
    recent = db.execute("SELECT region, name, created_at FROM users",
                        order_by=[("created_at", "desc")], limit=20)
```

## How a statement is placed

1. `shard="name"` on the call, when given.
2. The router, which reads the shard key out of the statement's params.
3. `default_shard`, when configured.
4. Otherwise the statement is **unrouted**:
   - a read (`SELECT`, `WITH`, `SHOW`, `EXPLAIN`, `PRAGMA`, `DESCRIBE`,
     `VALUES`) fans out to every shard and the rows are concatenated in shard
     order;
   - anything else is a write and raises `ShardingError`, because silently
     writing the same row to every shard is never what was meant. Pass
     `all_shards=True` to opt a write in, which is how schema changes reach
     every shard.

Shard order is the dict's insertion order. It fixes the order of fan-out
results and the order in which an unrouted `fetch_one` asks the shards.

`shard_for(params)` returns the shard the router (then `default_shard`) would
pick, or `None` for unrouted. `shards` is a read-only mapping of name to
manager and `for_each_shard()` iterates `(name, manager)`, for per-shard
maintenance such as running migrations.

## Routers

A router is anything with `shard_for(params) -> str | None`, or a plain
callable with that signature. Returning `None` leaves the statement unrouted
rather than raising, so statements without the key (schema changes, global
reads) still work through the same object. A router that names a shard not in
the mapping raises `ShardingError`.

| Router | Use when | Notes |
|---|---|---|
| `KeyRouter(param_name, mapping)` | A small set of known values (region, tenant tier) | Values absent from the mapping are unrouted |
| `HashRouter(param_name, shard_names)` | Many keys, even spread wanted | BLAKE2b over `str(value)` modulo `len(shard_names)`; stable across processes, unlike Python's `hash()`. `7` and `"7"` land together. Reordering or resizing the list moves rows |
| `RangeRouter(param_name, boundaries)` | Monotonic keys (ids, dates) | `[(1000, "a"), (2000, "b"), (None, "c")]`: `< 1000` to `a`, `1000..1999` to `b`, `2000+` to `c`. Upper bounds are exclusive and must ascend; the `None` tail is optional and last |
| any callable `(params) -> str \| None` | Anything else | Receives the raw params dict (or `None`) |

`HashRouter.bucket(value, n)` is exposed so a placement can be computed outside
the router, for instance when planning a rebalance by hand.

## Fan-out reads

A fan-out runs the same SQL on each shard and concatenates the rows. Two
arguments shape the combined result on the client:

```python
db.execute("SELECT id, score FROM plays WHERE game = :game", {"game": "x"},
           order_by=[("score", "desc"), ("id", "asc")], limit=10)
```

- `order_by` is a list of `(column, "asc" | "desc")`; the column must be in the
  selected rows. `NULL`s sort first ascending and last descending.
- `limit` caps the concatenated rows.
- A `LIMIT` written in the SQL applies **per shard**: `... LIMIT 1` across
  three shards returns up to three rows. Use `limit=` for a global cap, and
  keep a per-shard `LIMIT` in the SQL as well when it saves transfer (a
  top-N is correct if each shard returns its own top N and the client takes
  the top N of those).
- Aggregates come back once per shard. `SELECT COUNT(*) AS n FROM users`
  returns one row per shard; sum them yourself.
- A `JOIN` only sees rows that live on the same shard. There are no
  distributed joins.
- `fetch_one` without a routable key asks the shards in order and returns the
  first row found, so it costs up to one query per shard.
- `execute_async` runs the shards concurrently and still returns rows in
  shard order. `fetch_one_async` asks every shard at once.

`return_id=True` needs a single shard, since it returns one key. Keys are
generated per shard, so an id only identifies a row together with its shard.

## Transactions

A transaction spans exactly one shard. There are no cross-shard transactions.

```python
with db.transaction():                       # shard chosen by the first statement
    db.execute("INSERT INTO orders (region, ...) VALUES (:region, ...)", {"region": "eu", ...})
    db.execute("UPDATE counters SET n = n + 1")   # no key: stays on the pinned shard

with db.transaction(shard="us"):             # or pinned up front
    ...
```

- The shard is `shard=` when given, otherwise the one the first statement
  routes to (falling back to `default_shard`). A first statement that cannot
  be routed raises.
- A later statement that routes to a different shard raises `ShardingError`
  and the transaction rolls back. Statements inside the block with no shard
  key run on the pinned shard.
- `all_shards=True` is refused inside a transaction.
- Nested `transaction()` joins the outer block, as with `DatabaseManager`.
  A nested `transaction(shard=...)` naming a different shard raises.
- The shard's own transaction is entered when the shard becomes known, so a
  block that runs no statement opens nothing.
- As with `DatabaseManager`, use the synchronous calls inside a transaction:
  the server backends run `execute_async` in a worker thread, which does not
  see the thread-bound transaction.

## Writes and scripts

- An unrouted write raises; `all_shards=True` runs it on every shard in order.
  A fan-out write is not atomic across shards: a later shard failing leaves
  the earlier ones' writes committed.
- `execute_script(script, shard=..., all_shards=...)` follows the same rule.
  With `all_shards=True` the shards run in order and the first failure stops
  the run and is returned as a `QueryResult` whose `error_message` names the
  shard.
- `table_exists(name)` is true only when every shard has the table, since the
  sharded manager can only serve a table it finds everywhere. Use
  `table_exists(name, shard=...)` for a table that lives on one shard.

## Connecting

`connect()` connects every shard in order and returns `True` only if all of
them connected. If any fails, the shards already connected are disconnected
again and it returns `False`, so the object is never left half-open: a
partial shard set would silently miss rows on reads and scatter writes.
`disconnect()` disconnects every shard and continues past one that fails.

Shards may be given as URLs, which are wrapped in
`DatabaseManager(url, **manager_kwargs)`, or as `DatabaseManager` instances,
used as given. Pass instances unconnected, since `connect()` connects every
shard.

## Thread safety and consistency

Thread safety is that of the underlying managers: each shard's manager tracks
its transaction per thread, and so does the sharded manager, so two threads
can run independent transactions through one object.

In short: each shard is an independent database and nothing here changes
that. No cross-shard transactions, no distributed joins, no global
uniqueness of generated keys, no automatic rebalancing when the shard set
changes. The router is a pure function of the params, so moving rows between
shards is an application-level migration.

## Not in this version

- Migrations across shards: run `initialize()` per shard via
  `for_each_shard()`.
- Consistent hashing or any rebalancing on shard-set changes.
- Read replicas or per-shard failover.
- Merging aggregates across shards.
