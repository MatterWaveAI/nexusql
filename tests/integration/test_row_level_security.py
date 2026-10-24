"""
Row-level security helpers against live backends.

The same enable / tenant / scoped sequence must hide the other tenant's rows
on every backend. On PostgreSQL that has to be the server enforcing it, and
the test user is a superuser, who bypasses row-level security entirely. So
the PostgreSQL cases create a throwaway role with a uuid name, grant it the
table, and ``SET LOCAL ROLE`` inside the tenant transaction; the role is
dropped again in teardown. Elsewhere the ``<table>_scoped`` view does the
filtering and nothing stops a query against the table itself, which one test
below records on purpose.

Backends that are not reachable are skipped at collection (see conftest).
"""

import os
import uuid
from contextlib import contextmanager

import pytest

from nexusql import DatabaseManager, DatabaseType

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


def table_name() -> str:
    return "rls_" + uuid.uuid4().hex[:10]


def role_name() -> str:
    return "rls_role_" + uuid.uuid4().hex[:10]


def ids(rows):
    return sorted(r["id"] for r in rows)


def is_postgres(db) -> bool:
    return db.config.database_type == DatabaseType.POSTGRESQL


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} (id INT PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL, "
        f"item VARCHAR(50))"
    )
    db_manager.execute(
        f"INSERT INTO {name} (id, tenant_id, item) VALUES "
        f"(1, 'alpha', 'a1'), (2, 'alpha', 'a2'), (3, 'beta', 'b1')"
    )
    db_manager.enable_row_security(name)
    yield name
    for cleanup in (
        lambda: db_manager.disable_row_security(name),
        lambda: db_manager.execute(f"DROP TABLE {name}"),
    ):
        try:
            cleanup()
        except Exception:
            pass


@pytest.fixture
def role(db_manager, table):
    """A non-superuser role with full DML on the table; None off PostgreSQL."""
    if not is_postgres(db_manager):
        yield None
        return
    name = role_name()
    db_manager.execute(f"CREATE ROLE {name} NOLOGIN")
    db_manager.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {name}")
    yield name
    for statement in (f"DROP OWNED BY {name}", f"DROP ROLE IF EXISTS {name}"):
        try:
            db_manager.execute(statement)
        except Exception:
            pass


@contextmanager
def acting_as(db, role):
    """Run the block as ``role`` on PostgreSQL (inside the open transaction)."""
    if role is None:
        yield
        return
    db.execute(f"SET LOCAL ROLE {role}")
    try:
        yield
    finally:
        try:
            db.execute("RESET ROLE")
        except Exception:
            pass


SESSION_VALUE_SQL = {
    DatabaseType.POSTGRESQL: "SELECT current_setting('app.tenant_id', true) AS v",
    DatabaseType.MYSQL: "SELECT @app_tenant_id AS v",
    DatabaseType.MSSQL: "SELECT CAST(SESSION_CONTEXT(N'tenant_id') AS NVARCHAR(128)) AS v",
    DatabaseType.SQLITE: "SELECT app_tenant_id() AS v",
}


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestRowLevelSecurity:
    def test_each_tenant_sees_only_its_rows(self, db_manager, table, role):
        scoped = db_manager.scoped(table)
        with db_manager.tenant("alpha"), acting_as(db_manager, role):
            assert ids(db_manager.execute(f"SELECT id FROM {scoped}")) == [1, 2]
        with db_manager.tenant("beta"), acting_as(db_manager, role):
            assert ids(db_manager.execute(f"SELECT id FROM {scoped}")) == [3]

    def test_without_a_tenant_nothing_is_visible(self, db_manager, table, role):
        with db_manager.transaction(), acting_as(db_manager, role):
            assert db_manager.execute(f"SELECT id FROM {db_manager.scoped(table)}") == []

    def test_nested_tenant_restores_the_outer_one(self, db_manager, table, role):
        scoped = db_manager.scoped(table)
        with db_manager.tenant("alpha"), acting_as(db_manager, role):
            with db_manager.tenant("beta"):
                assert ids(db_manager.execute(f"SELECT id FROM {scoped}")) == [3]
            assert db_manager.current_tenant == "alpha"
            assert ids(db_manager.execute(f"SELECT id FROM {scoped}")) == [1, 2]

    def test_tenant_does_not_outlive_its_connection_use(self, db_manager, table):
        """After the block the pooled connection carries no tenant for its next borrower."""
        single = DatabaseManager(db_manager.database_url, pool_min=1, pool_max=1)
        assert single.connect()
        try:
            with single.tenant("alpha"):
                row = single.fetch_one(SESSION_VALUE_SQL[single.config.database_type])
                assert row["v"] == "alpha"
            assert single.current_tenant is None
            row = single.fetch_one(SESSION_VALUE_SQL[single.config.database_type])
            assert row["v"] in (None, "")
            assert single.execute(f"SELECT id FROM {single.scoped(table)}") == [] or is_postgres(single)
        finally:
            single.disconnect()

    def test_joins_an_open_transaction_and_rolls_back_with_it(self, db_manager, table, role):
        with pytest.raises(RuntimeError, match="abandon"):
            with db_manager.transaction(), acting_as(db_manager, role):
                with db_manager.tenant("alpha"):
                    db_manager.execute(
                        f"INSERT INTO {table} (id, tenant_id, item) VALUES (4, 'alpha', 'a3')"
                    )
                    assert ids(db_manager.execute(
                        f"SELECT id FROM {db_manager.scoped(table)}"
                    )) == [1, 2, 4]
                raise RuntimeError("abandon")
        assert db_manager.current_tenant is None
        assert ids(db_manager.execute(f"SELECT id FROM {table}")) == [1, 2, 3]

    def test_write_for_own_tenant_is_accepted(self, db_manager, table, role):
        with db_manager.tenant("beta"), acting_as(db_manager, role):
            db_manager.execute(
                f"INSERT INTO {table} (id, tenant_id, item) VALUES (4, 'beta', 'b2')"
            )
            db_manager.execute(
                f"UPDATE {table} SET item = :item WHERE id = :id", {"item": "b1!", "id": 3}
            )
        rows = db_manager.execute(f"SELECT id, item FROM {table} WHERE tenant_id = 'beta'")
        assert sorted((r["id"], r["item"]) for r in rows) == [(3, "b1!"), (4, "b2")]

    def test_enable_twice_is_harmless(self, db_manager, table, role):
        db_manager.enable_row_security(table)
        with db_manager.tenant("beta"), acting_as(db_manager, role):
            assert ids(db_manager.execute(f"SELECT id FROM {db_manager.scoped(table)}")) == [3]

    def test_disable_restores_unfiltered_access(self, db_manager, table, role):
        db_manager.disable_row_security(table)
        if is_postgres(db_manager):
            with db_manager.transaction(), acting_as(db_manager, role):
                assert ids(db_manager.execute(f"SELECT id FROM {table}")) == [1, 2, 3]
        else:
            with pytest.raises(Exception):
                db_manager.execute(f"SELECT id FROM {db_manager.scoped(table)}")
            assert ids(db_manager.execute(f"SELECT id FROM {table}")) == [1, 2, 3]

    def test_table_itself_is_filtered_only_on_postgres(self, db_manager, table, role):
        """The boundary each backend provides: server-enforced versus a view."""
        with db_manager.tenant("alpha"), acting_as(db_manager, role):
            visible = ids(db_manager.execute(f"SELECT id FROM {table}"))
        if is_postgres(db_manager):
            assert visible == [1, 2]
        else:
            assert visible == [1, 2, 3]


# ---------------------------------------------------------------------------
# PostgreSQL: the server enforces the policy
# ---------------------------------------------------------------------------

@pytest.fixture
def pg(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    yield db
    db.disconnect()


@contextmanager
def pg_table_and_role(db, column_type="VARCHAR(36)", owner=False):
    """Table plus a throwaway role granted on it; optionally the role owns the table."""
    table, role = table_name(), role_name()
    try:
        db.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, tenant_id {column_type} NOT NULL)")
        db.execute(f"CREATE ROLE {role} NOLOGIN")
        if owner:
            db.execute(f"ALTER TABLE {table} OWNER TO {role}")
        else:
            db.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        yield table, role
    finally:
        for statement in (
            f"DROP TABLE IF EXISTS {table}",
            f"DROP OWNED BY {role}",
            f"DROP ROLE IF EXISTS {role}",
        ):
            try:
                db.execute(statement)
            except Exception:
                pass


def test_postgres_cross_tenant_writes_are_refused(pg):
    import psycopg

    with pg_table_and_role(pg) as (table, role):
        pg.execute(f"INSERT INTO {table} VALUES (1, 'alpha'), (2, 'beta')")
        pg.enable_row_security(table)

        with pg.tenant("alpha"), acting_as(pg, role):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
                pg.execute(f"INSERT INTO {table} VALUES (3, 'beta')")

        with pg.tenant("alpha"), acting_as(pg, role):
            # Moving a row to another tenant fails the policy's WITH CHECK.
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
                pg.execute(f"UPDATE {table} SET tenant_id = 'beta' WHERE id = 1")

        with pg.tenant("alpha"), acting_as(pg, role):
            # The other tenant's row is invisible, so there is nothing to delete.
            pg.execute(f"DELETE FROM {table} WHERE id = 2")
            assert ids(pg.execute(f"SELECT id FROM {table}")) == [1]

        assert ids(pg.execute(f"SELECT id FROM {table}")) == [1, 2]


def test_postgres_superuser_bypasses_the_policy(pg):
    """Documented boundary: a superuser sees every row even inside tenant()."""
    with pg_table_and_role(pg) as (table, _):
        pg.execute(f"INSERT INTO {table} VALUES (1, 'alpha'), (2, 'beta')")
        pg.enable_row_security(table)
        with pg.tenant("alpha"):
            assert ids(pg.execute(f"SELECT id FROM {table}")) == [1, 2]


def test_postgres_force_applies_the_policy_to_the_owner(pg):
    with pg_table_and_role(pg, owner=True) as (table, owner):
        pg.execute(f"INSERT INTO {table} VALUES (1, 'alpha'), (2, 'beta')")
        pg.enable_row_security(table)
        with pg.tenant("beta"), acting_as(pg, owner):
            assert ids(pg.execute(f"SELECT id FROM {table}")) == [2]
        pg.disable_row_security(table)
        with pg.transaction(), acting_as(pg, owner):
            assert ids(pg.execute(f"SELECT id FROM {table}")) == [1, 2]


def test_postgres_policy_is_typed_like_the_column(pg):
    """An integer tenant column is compared as integer, so its index stays usable."""
    with pg_table_and_role(pg, column_type="INTEGER") as (table, role):
        pg.execute(f"INSERT INTO {table} VALUES (1, 7), (2, 8)")
        pg.enable_row_security(table)
        expr = pg.fetch_one(
            "SELECT pg_get_expr(polqual, polrelid) AS q FROM pg_policy "
            "WHERE polrelid = :table::regclass", {"table": table}
        )
        assert "::integer" in expr["q"]
        with pg.tenant(7), acting_as(pg, role):
            assert ids(pg.execute(f"SELECT id FROM {table}")) == [1]


def test_postgres_setting_is_local_to_the_transaction(pg):
    single = DatabaseManager(pg.database_url, pool_min=1, pool_max=1)
    assert single.connect()
    try:
        with single.tenant("alpha"):
            assert single.fetch_one("SELECT current_setting('app.tenant_id', true) AS v") == {"v": "alpha"}
        assert single.fetch_one("SELECT current_setting('app.tenant_id', true) AS v") == {"v": ""}
    finally:
        single.disconnect()


# ---------------------------------------------------------------------------
# Async PostgreSQL backend
# ---------------------------------------------------------------------------

ASYNC_URL = os.getenv(
    "TEST_POSTGRESQL_URL", "postgresql://postgres:mwtest@127.0.0.1:15432/mwtest"
)


@pytest.fixture
async def async_backend():
    from nexusql import PSYCOPG_AVAILABLE
    if not PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed")
    from nexusql import AsyncPostgresBackend

    backend = AsyncPostgresBackend(ASYNC_URL, max_pool_size=4)
    try:
        if not await backend.connect():
            pytest.skip("PostgreSQL not reachable for async tests")
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable for async tests: {exc}")
    yield backend
    await backend.disconnect()


async def test_async_backend_enforces_the_policy(async_backend):
    import psycopg

    db = async_backend
    table, role = table_name(), role_name()
    try:
        await db.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, tenant_id UUID NOT NULL)")
        alpha, beta = uuid.uuid4(), uuid.uuid4()
        await db.execute(
            f"INSERT INTO {table} VALUES (1, :alpha), (2, :alpha), (3, :beta)",
            {"alpha": alpha, "beta": beta},
        )
        await db.execute(f"CREATE ROLE {role} NOLOGIN")
        await db.execute(f"GRANT SELECT, INSERT ON {table} TO {role}")
        await db.enable_row_security(table)
        assert db.scoped(table) == table

        async with db.tenant(alpha):
            await db.execute(f"SET LOCAL ROLE {role}")
            assert db.current_tenant == str(alpha)
            assert ids(await db.execute(f"SELECT id FROM {table}")) == [1, 2]
            async with db.tenant(beta):
                assert ids(await db.execute(f"SELECT id FROM {table}")) == [3]
            assert ids(await db.execute(f"SELECT id FROM {table}")) == [1, 2]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await db.execute(f"INSERT INTO {table} VALUES (4, :beta)", {"beta": beta})

        assert db.current_tenant is None
        assert ids(await db.execute(f"SELECT id FROM {table}")) == [1, 2, 3]

        await db.disable_row_security(table)
        async with db.transaction():
            await db.execute(f"SET LOCAL ROLE {role}")
            assert ids(await db.execute(f"SELECT id FROM {table}")) == [1, 2, 3]
            await db.execute("RESET ROLE")
    finally:
        for statement in (
            f"DROP TABLE IF EXISTS {table}",
            f"DROP OWNED BY {role}",
            f"DROP ROLE IF EXISTS {role}",
        ):
            try:
                await db.execute(statement)
            except Exception:
                pass


async def test_async_tenant_rejects_none(async_backend):
    with pytest.raises(ValueError, match="None"):
        async with async_backend.tenant(None):
            pass
