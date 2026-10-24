# Row-Level Security and Multi-Tenancy

A multi-tenant application keeps every tenant's rows in the same tables and
must never let one tenant read or write another's. Filtering with
`WHERE tenant_id = :tenant` on every query works until someone forgets one.
These helpers move the tenant into the database session, where a policy or a
view applies it to every statement that runs there.

```python
db.enable_row_security("orders")            # once, at schema setup

with db.tenant(current_user.tenant_id):     # per request
    rows = db.execute(f"SELECT * FROM {db.scoped('orders')} WHERE status = :s",
                      {"s": "open"})
```

## API

| Call | Purpose |
|------|---------|
| `db.tenant(tenant_id)` | Context manager. Sets the tenant on the connection for one transaction. |
| `db.current_tenant` | The tenant set by the innermost active `tenant()` block on this thread, or `None`. |
| `db.enable_row_security(table, tenant_column="tenant_id")` | Scope `table` to the session tenant: a policy on PostgreSQL, a `<table>_scoped` view elsewhere. Idempotent. |
| `db.disable_row_security(table)` | Reverse it. |
| `db.scoped(table)` | The name to query: `table` on PostgreSQL, `<table>_scoped` elsewhere. |

`AsyncPostgresBackend` has the same five, with `tenant`, `enable_row_security`
and `disable_row_security` as `async` versions.

The DDL is also available as plain statements for migrations:
`nexusql.row_security_statements(db_type, table, tenant_column, column_type=None)`
and `nexusql.disable_row_security_statements(db_type, table)`, with
`nexusql.scoped_name(db_type, table)` for the view name.

No query is rewritten. `scoped()` only tells you what to put after `FROM`;
the SQL you write is the SQL that runs.

## How the tenant reaches the database

`tenant()` always runs inside a transaction. If none is open it opens one; if
one is open it joins it. The manager pins a single connection for a
transaction, and that is where the value is stored, so every statement in the
block sees it and nothing on any other connection does. That connection
affinity is the whole mechanism: a value set on one pooled connection means
nothing on another, which is why the helper refuses to set it outside a
transaction where the next statement could borrow a different connection.

| Backend | Set by | Read by | Scope |
|---------|--------|---------|-------|
| PostgreSQL | `SELECT set_config('app.tenant_id', :id, true)` | `current_setting('app.tenant_id', true)` | Transaction. `is_local => true` reverts it at commit or rollback. |
| MySQL | `SET @app_tenant_id = :id` | `nexusql_tenant_id()`, a one-line stored function returning the variable | Session. The helper writes the previous value (or NULL) back when the block exits. |
| SQL Server | `EXEC sp_set_session_context N'tenant_id', :id` | `SESSION_CONTEXT(N'tenant_id')` | Session. Previous value written back on exit. |
| SQLite | Held on the manager, per thread | `app_tenant_id()`, registered on every connection the manager opens | The `tenant()` block. |

The value is sent as text on every backend and the database converts it to
the column's type. Integers and UUIDs work as tenant ids; `str()` is applied.
`None` is refused, since "no tenant" must be the absence of a block, not a
value that could be passed by accident.

Nesting is allowed. The inner block replaces the tenant and the outer one is
restored when it exits. Inside an open `transaction()`, the block joins it and
nothing commits until the outer transaction does.

Without a tenant set, the policy and the views match nothing: an application
that forgets `tenant()` sees an empty table rather than everyone's rows.

## What is enforced, per backend

This is the part to read before relying on the feature.

### PostgreSQL: enforced by the server

`enable_row_security(table)` runs, in one transaction:

```sql
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS nexusql_tenant_isolation ON orders;
CREATE POLICY nexusql_tenant_isolation ON orders FOR ALL
    USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::<column type>)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::<column type>);
```

`FOR ALL` covers SELECT, INSERT, UPDATE and DELETE. `USING` hides rows that
belong to other tenants; `WITH CHECK` refuses an INSERT or UPDATE that would
produce a row for another tenant, so a request cannot write into a tenant it
is not acting for. The setting is cast to the tenant column's declared type
(looked up in `pg_attribute` when the policy is created), so an index on an
integer or uuid `tenant_id` is still used. `NULLIF` turns the empty string
the setting reverts to into NULL, which matches nothing.

The filter applies to every client of the table, not only to this library.
Two groups are exempt, and this is PostgreSQL's design rather than something
the helper can change:

- **Superusers** bypass row-level security always. Nothing turns this off.
- **The table owner** bypasses it unless `FORCE ROW LEVEL SECURITY` is on.
  The helper turns FORCE on, so the owner is filtered too.

Roles with `BYPASSRLS` are exempt as well. If your application connects as a
superuser (the test database here does), the policy does nothing for it:
connect the application as an ordinary role that has been granted the table,
or `SET ROLE` to one. The integration tests do exactly that to prove the
policy works: create a role, grant it the table, `SET LOCAL ROLE` inside the
tenant transaction, and check that the other tenant's rows are invisible and
a cross-tenant INSERT raises `InsufficientPrivilege`.

Grants are not changed by the helper. A role still needs `GRANT SELECT, ...
ON table` to use it at all; the policy decides which rows that grant reaches.

`disable_row_security(table)` drops the policy, then runs `NO FORCE` and
`DISABLE ROW LEVEL SECURITY`.

### MySQL, SQL Server, SQLite: a convenience filter

These engines have no row-level security the manager can switch on with one
call. SQL Server has security policies, but they need a predicate function
written per table; MySQL and SQLite have nothing comparable. The helper
instead creates a view:

```sql
-- MySQL
CREATE OR REPLACE VIEW orders_scoped AS
    SELECT * FROM orders WHERE tenant_id = nexusql_tenant_id();
-- SQL Server
CREATE OR ALTER VIEW orders_scoped AS
    SELECT * FROM orders WHERE tenant_id = CAST(SESSION_CONTEXT(N'tenant_id') AS NVARCHAR(128));
-- SQLite
CREATE VIEW orders_scoped AS
    SELECT * FROM orders WHERE tenant_id = app_tenant_id();
```

Queries through `db.scoped("orders")` are tenant-scoped. Queries against
`orders` are not, and nothing in the database stops them. A missed
`scoped()` is the same bug as a missed `WHERE`; the view just makes the
correct form shorter and keeps the tenant out of the query text. If you need
the database to enforce isolation on these backends, revoke table access
from the application role and grant it only on the views (MySQL and SQL
Server support this; SQLite has no roles), or use PostgreSQL.

Writes through the view are a per-engine matter. SQLite views are read-only.
MySQL and SQL Server allow INSERT and UPDATE through a simple single-table
view, but do not check that the new row still matches the filter unless the
view is created `WITH CHECK OPTION`, which the helper does not add. Write to
the table and include the tenant column yourself.

Backend notes:

- **MySQL** views cannot reference user variables directly, so the first
  `enable_row_security()` in a schema creates a stored function
  `nexusql_tenant_id()` (`DETERMINISTIC NO SQL`, returning
  `@app_tenant_id`). Creating it needs the `CREATE ROUTINE` privilege.
  `disable_row_security()` leaves the function in place, since other views
  may use it.
- **SQL Server**'s `SESSION_CONTEXT` persists for the connection, so the
  helper writes the previous value back when the block ends; the connection
  returns to the pool clean.
- **SQLite**'s `app_tenant_id()` exists only on connections this manager
  opened. The `sqlite3` command-line tool or another library querying the
  view will fail with "no such function".

### `scoped()` keeps application code portable

`db.scoped(table)` is the one place the backends differ for a caller. On
PostgreSQL it returns the table, because the policy already filters it; on
the others it returns the view. Code written against `scoped()` runs
unchanged on all four, with the stronger guarantee wherever the server can
give it.

## Operational notes

- `enable_row_security()` is idempotent: it replaces the policy or view, so
  it is safe in a migration that may run twice.
- The policy name is fixed (`nexusql_tenant_isolation`), one per table. Add
  further policies of your own if a table needs more than tenant isolation;
  PostgreSQL combines permissive policies with OR.
- Table and column names are spliced into DDL and are validated as plain,
  optionally schema-qualified, bare or double-quoted identifiers. Anything
  else raises `ValueError` before any statement runs.
- The value is per thread on the sync manager, like the transaction that
  holds it. On the async backend it is per instance, matching
  `transaction()`, which pins one connection per instance.
- The PostgreSQL setting name is `app.tenant_id` (`nexusql.PG_TENANT_SETTING`),
  so hand-written policies, triggers and audit columns can read the same value
  with `current_setting('app.tenant_id', true)`.
