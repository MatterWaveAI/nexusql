# Query Builder

`nexusql.query` is a small, typed query builder. It is not an ORM: there are
no models, sessions or identity maps. It builds the string you would otherwise
assemble by hand, and it builds it safely.

```python
from nexusql import DatabaseManager, Q

db = DatabaseManager("sqlite:///app.db")

q = (Q.select("users").columns("id", "email")
     .where((Q.col("age") >= Q.p("min_age", 18)) & Q.col("email").like("%@example.com"))
     .order_by("-created_at").limit(10).offset(20))

q.build()
# ('SELECT id, email FROM users WHERE age >= :min_age AND email LIKE :p1 '
#  'ORDER BY created_at DESC LIMIT 10 OFFSET 20',
#  {'min_age': 18, 'p1': '%@example.com'})

rows = db.run(q)
```

## Why it exists

Queries that are assembled from optional pieces (a search form, an admin
listing whose sort column the client picks) are where string concatenation
creeps in and where injection bugs come from. The builder keeps the two kinds
of input apart by construction:

- **Values never reach the SQL text.** Every Python value becomes a `:name`
  parameter: `Q.col("name") == user_input` is always `name = :p1`, whatever
  the input contains.
- **Identifiers are validated, not escaped.** Tables, columns and aliases
  must match `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$`; anything
  else raises `QueryError` (a `ValueError`) before the database is touched.
  There is no quoting path for odd names, so there is nothing to get wrong.

The output is canonical PostgreSQL SQL with `:name` parameters, exactly what
`DatabaseManager.execute()` and `fetch_one()` already accept, so the existing
per-dialect translation and parameter conversion apply unchanged. The builder
itself knows nothing about dialects.

## Statements

Every method returns a new object; nothing is mutated. A base query can be
shared and refined without copying.

| Start with | Then chain |
|---|---|
| `Q.select(table, alias=None)` | `.columns(...)`, `.distinct()`, `.join(table, on=, kind=, alias=)`, `.where(expr)`, `.group_by(...)`, `.having(expr)`, `.order_by(...)`, `.limit(n)`, `.offset(n)` |
| `Q.insert(table)` | `.values(mapping=None, **columns)`, `.returning(...)` |
| `Q.update(table)` | `.set(mapping=None, **columns)`, `.where(expr)`, `.all_rows()` |
| `Q.delete(table)` | `.where(expr)`, `.all_rows()` |

- `.columns()` takes identifier strings (`"id"`, `"u.email"`, `"*"`, `"u.*"`)
  or expressions (`Q.raw("COUNT(*)").as_("n")`). No columns means `*`.
- `.where()` and `.having()` may be called repeatedly; the conditions are
  ANDed.
- `.order_by()` takes `"name"` (ascending), `"-name"` (descending), or an
  expression with `.asc()` / `.desc()`.
- `.join()` kinds are `inner` (default), `left`, `right`, `full` and `cross`.
  Every kind except `cross` requires `on=`.
- `.limit()` and `.offset()` take non-negative ints and are emitted as
  literals, not parameters, because the SQL Server translation rewrites
  `LIMIT n OFFSET m` into `OFFSET/FETCH` by matching literal numbers.
- `UPDATE` and `DELETE` refuse to build without a `WHERE`; call
  `.all_rows()` to say the whole table is intended.
- `.values()` / `.set()` accept a mapping, keywords, or both; a column named
  again in a later call takes the new value. Values may be expressions:
  `set(views=Q.col("views") + 1, updated=Q.raw("NOW()"))`.

## Expressions

`Q.col(name)` is a column reference. Comparing it to a Python value yields a
condition with that value bound as a parameter; comparing it to another
expression yields plain SQL with no parameter.

| Python | SQL |
|---|---|
| `c == 1`, `c != 1`, `c < 1`, `c <= 1`, `c > 1`, `c >= 1` | `c = :p1`, `c <> :p1`, ... |
| `c == None`, `c != None` | `c IS NULL`, `c IS NOT NULL` |
| `c.is_null()`, `c.is_not_null()` | `c IS NULL`, `c IS NOT NULL` |
| `c.like("a%")`, `c.not_like("a%")` | `c LIKE :p1`, `c NOT LIKE :p1` |
| `c.ilike("a%")` | `LOWER(c) LIKE LOWER(:p1)` |
| `c.in_([1, 2])`, `c.not_in([1, 2])` | `c IN (:p1, :p2)`, `c NOT IN (:p1, :p2)` |
| `c.in_([])`, `c.not_in([])` | `1 = 0`, `1 = 1` |
| `c.between(1, 5)` | `c BETWEEN :p1 AND :p2` |
| `c + 1`, `c - 1`, `c * 2`, `c / 2` | `c + :p1`, ... |
| `a & b`, `a \| b`, `~a` | `a AND b`, `a OR b`, `NOT (a)` |
| `Q.col("users.id") == Q.col("orders.user_id")` | `users.id = orders.user_id` |
| `expr.as_("alias")` | `expr AS alias` |

Compound operands are parenthesised, so `(a == 1) & ((b == 2) | (c == 3))`
renders as `a = :p1 AND (b = :p2 OR c = :p3)`. Use `&`, `|` and `~`, not
`and`, `or`, `not`: an expression raises `TypeError` if used as a boolean,
because `a and b` would silently keep only `b`.

`.ilike()` is rendered with `LOWER()` rather than `ILIKE` because `ILIKE` is
PostgreSQL-only and the translator does not rewrite it; `LOWER`/`LIKE` run
on every backend.

### Parameters

Literal values are auto-named `:p1`, `:p2`, ... in the order they appear in
the SQL. `Q.p("name", value)` names one explicitly, which is useful when the
same value appears twice or when the parameter dict is logged. The two styles
mix freely: auto-naming skips any name an explicit parameter already took.
Giving the same explicit name two different values raises `QueryError`.

### `Q.raw()`: the escape hatch

`Q.raw(sql, **params)` inserts SQL text verbatim. It is for SQL the author
typed, such as aggregates and functions the builder has no method for:

```python
Q.select("orders").columns("user_id", Q.raw("SUM(amount)").as_("total"))
 .where(Q.raw("created_at > NOW() - :days * INTERVAL '1 day'", days=7))
 .group_by("user_id").having(Q.raw("COUNT(*)") > 1)
```

A raw fragment supports the same operators as a column. It is the one place
where text bypasses identifier validation, so never build its argument from
request data; pass values through its keyword parameters instead.

### Case-sensitive names

Identifiers are emitted unquoted by default so the manager's translation
regexes see plain words. For a table or column created with a case-sensitive
name, `Q.col("Users", quote=True)` emits `"Users"` (and
`Q.col("public.Email", quote=True)` emits `"public"."Email"`). The validation
rule still applies.

## Running a statement

```python
sql, params = q.build()          # the canonical SQL and its parameters

rows = db.run(q)                 # DatabaseManager.execute(sql, params)
row = db.run_one(q)              # DatabaseManager.fetch_one(sql, params)
rows = q.execute(db)             # the same two calls, from the statement side
row = q.fetch_one(db)

new_id = Q.insert("users").values(email="a@b.c").execute(db, return_id=True)
new_id = db.run(Q.insert("users").values(email="a@b.c"), return_id=True)
```

`return_id=True` goes through the existing flag on `execute()`, so it returns
the generated key on every backend. A statement that already has
`.returning(...)` returns the first column of its first row, as a hand-written
INSERT with RETURNING does.

`AsyncPostgresBackend` offers the same two conveniences as coroutines:
`await backend.run(q, return_id=False)` and `await backend.run_one(q)`, where
`run_one` returns the first row as a dict (or `None`) rather than a
`QueryResult`, so builder code reads the same on both paths.

## Portability notes

The builder emits canonical SQL; what runs where is decided by the
translator, as for hand-written queries. The points that matter in practice:

- `.returning()` works on PostgreSQL and SQLite 3.35+. MySQL and SQL Server
  have no `RETURNING`; use `execute(db, return_id=True)` for the key.
- SQL Server requires `ORDER BY` with `OFFSET`/`FETCH`, so give `.limit()` /
  `.offset()` an `.order_by()` there.
- MySQL has no `FULL JOIN`.
- Boolean values are converted per backend by the manager, as for any
  parameter.

## Limitations

The builder covers single-table statements with joins, grouping, ordering
and paging. It does not generate subqueries (write them with `Q.raw()`),
common table expressions, `UNION`, multi-row `INSERT`, `INSERT ... SELECT`,
upserts or DDL. Those stay hand-written through `execute()`, which the
builder complements rather than replaces.
