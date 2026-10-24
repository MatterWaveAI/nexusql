"""
Row-level security helpers: tenant(), enable_row_security(), scoped().

SQLite exercises the real path without a server: the ``<table>_scoped`` view
reads the tenant through the ``app_tenant_id()`` function registered on each
connection. The PostgreSQL, MySQL and SQL Server branches send dialect
statements to the pinned connection, so they are checked here against fake
connections that record what was sent; the live backends are covered by
tests/integration/test_row_level_security.py when reachable.
"""

import pytest

from nexusql import (
    ConnectionConfig,
    DatabaseManager,
    DatabaseType,
    disable_row_security_statements,
    row_security_statements,
    scoped_name,
)
from nexusql.tenancy import check_identifier, tenant_value


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, item TEXT)"
    )
    manager.execute(
        "INSERT INTO orders (id, tenant_id, item) VALUES "
        "(1, 'alpha', 'a1'), (2, 'alpha', 'a2'), (3, 'beta', 'b1')"
    )
    manager.enable_row_security("orders")
    yield manager
    try:
        manager.disconnect()
    except Exception:
        pass


def ids(rows):
    return sorted(r["id"] for r in rows)


# ---------------------------------------------------------------------------
# SQLite: the real driver path, filtered through the view
# ---------------------------------------------------------------------------

class TestSqlite:
    def test_scoped_names_the_view(self, db):
        assert db.scoped("orders") == "orders_scoped"
        views = db.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        assert [v["name"] for v in views] == ["orders_scoped"]

    def test_each_tenant_sees_only_its_rows(self, db):
        with db.tenant("alpha"):
            assert ids(db.execute(f"SELECT id FROM {db.scoped('orders')}")) == [1, 2]
        with db.tenant("beta"):
            assert ids(db.execute(f"SELECT id FROM {db.scoped('orders')}")) == [3]

    def test_no_tenant_means_no_rows(self, db):
        assert db.current_tenant is None
        assert db.execute("SELECT id FROM orders_scoped") == []
        assert db.fetch_one("SELECT app_tenant_id() AS t") == {"t": None}

    def test_base_table_is_not_filtered(self, db):
        """The view is a convenience filter; the table stays open to anyone with access."""
        with db.tenant("alpha"):
            assert ids(db.execute("SELECT id FROM orders")) == [1, 2, 3]

    def test_current_tenant_tracks_the_block(self, db):
        with db.tenant("alpha"):
            assert db.current_tenant == "alpha"
        assert db.current_tenant is None

    def test_nested_tenant_restores_the_outer_one(self, db):
        with db.tenant("alpha"):
            with db.tenant("beta"):
                assert db.current_tenant == "beta"
                assert ids(db.execute("SELECT id FROM orders_scoped")) == [3]
            assert db.current_tenant == "alpha"
            assert ids(db.execute("SELECT id FROM orders_scoped")) == [1, 2]

    def test_joins_an_open_transaction(self, db):
        """Inside transaction() the tenant block must not commit on its own."""
        with pytest.raises(RuntimeError, match="abandon"):
            with db.transaction():
                with db.tenant("alpha"):
                    db.execute(
                        "INSERT INTO orders (id, tenant_id, item) VALUES (4, 'alpha', 'a3')"
                    )
                    assert ids(db.execute("SELECT id FROM orders_scoped")) == [1, 2, 4]
                raise RuntimeError("abandon")
        assert ids(db.execute("SELECT id FROM orders")) == [1, 2, 3]
        assert db.current_tenant is None

    def test_failure_inside_the_block_clears_the_tenant(self, db):
        with pytest.raises(Exception):
            with db.tenant("alpha"):
                db.execute("INSERT INTO orders (id, tenant_id) VALUES (1, 'alpha')")
        assert db.current_tenant is None
        assert db.execute("SELECT id FROM orders_scoped") == []

    def test_value_is_sent_as_text_and_compared_by_affinity(self, db):
        db.execute("CREATE TABLE numbered (id INTEGER PRIMARY KEY, tenant_id INTEGER)")
        db.execute("INSERT INTO numbered VALUES (1, 7), (2, 8)")
        db.enable_row_security("numbered")
        with db.tenant(7):
            assert db.current_tenant == "7"
            assert ids(db.execute(f"SELECT id FROM {db.scoped('numbered')}")) == [1]

    def test_custom_tenant_column(self, db):
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, org TEXT)")
        db.execute("INSERT INTO notes VALUES (1, 'x'), (2, 'y')")
        db.enable_row_security("notes", tenant_column="org")
        with db.tenant("y"):
            assert ids(db.execute("SELECT id FROM notes_scoped")) == [2]

    def test_enable_twice_replaces_the_view(self, db):
        db.enable_row_security("orders")
        with db.tenant("beta"):
            assert ids(db.execute("SELECT id FROM orders_scoped")) == [3]

    def test_disable_drops_the_view(self, db):
        db.disable_row_security("orders")
        assert db.execute("SELECT name FROM sqlite_master WHERE type = 'view'") == []
        db.disable_row_security("orders")  # already gone: not an error

    def test_tenant_set_through_a_second_manager_is_separate(self, db, temp_db_path):
        """The value lives on the manager's connection, not in the database file."""
        other = DatabaseManager(f"sqlite:///{temp_db_path}")
        assert other.connect()
        try:
            with db.tenant("alpha"):
                assert other.current_tenant is None
                assert other.execute("SELECT id FROM orders_scoped") == []
        finally:
            other.disconnect()

    @pytest.mark.parametrize(
        "table", ["orders; DROP TABLE orders", "orders)", "", "orders WHERE 1"]
    )
    def test_unsafe_table_names_are_refused(self, db, table):
        with pytest.raises(ValueError, match="identifier"):
            db.enable_row_security(table)
        with pytest.raises(ValueError, match="identifier"):
            db.scoped(table)
        with pytest.raises(ValueError, match="identifier"):
            db.disable_row_security(table)

    def test_unsafe_column_names_are_refused(self, db):
        with pytest.raises(ValueError, match="identifier"):
            db.enable_row_security("orders", tenant_column="tenant_id = tenant_id OR 1")
        # Nothing was touched: the existing view still filters.
        with db.tenant("beta"):
            assert ids(db.execute("SELECT id FROM orders_scoped")) == [3]

    def test_none_tenant_is_refused(self, db):
        with pytest.raises(ValueError, match="None"):
            with db.tenant(None):
                pass
        assert db.current_tenant is None


# ---------------------------------------------------------------------------
# Server dialects against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, fetch_results=None):
        self.executed = []
        # One result per fetchone() call, in order.
        self._results = list(fetch_results or [])
        self.description = None
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.released = []

    def acquire(self, timeout=None):
        return self._conn

    def release(self, conn, *, discard=False):
        self.released.append(discard)

    def close(self):
        pass


def fake_manager(db_type: str, fetch_results=None):
    """A manager whose pool hands out one recording connection."""
    urls = {
        "postgresql": "postgresql://u:p@localhost/db",
        "mysql": "mysql://u:p@localhost/db",
        "mssql": "mssql://u:p@localhost/db",
    }
    manager = DatabaseManager(ConnectionConfig.from_url(urls[db_type]))
    cursor = FakeCursor(fetch_results)
    conn = FakeConnection(cursor)
    manager._pool = FakePool(conn)
    return manager, cursor, conn


class TestPostgresBranch:
    def test_tenant_is_a_transaction_local_set_config(self):
        manager, cursor, conn = fake_manager("postgresql")
        with manager.tenant(42):
            assert manager.current_tenant == "42"
        # No explicit restore: the local setting ends with the transaction.
        assert cursor.executed == [
            ("SELECT set_config('app.tenant_id', %(tenant_id)s, true)", {"tenant_id": "42"})
        ]
        assert conn.commits == 1 and conn.rollbacks == 0

    def test_nested_block_writes_the_outer_tenant_back(self):
        manager, cursor, _ = fake_manager("postgresql")
        with manager.tenant("a"):
            with manager.tenant("b"):
                pass
        assert [p["tenant_id"] for _, p in cursor.executed] == ["a", "b", "a"]

    def test_joined_transaction_clears_with_empty_string(self):
        """set_config() takes text, so 'no tenant' is '' and NULLIF() in the policy reads it as NULL."""
        manager, cursor, _ = fake_manager("postgresql")
        with manager.transaction():
            with manager.tenant("a"):
                pass
        assert [p["tenant_id"] for _, p in cursor.executed] == ["a", ""]

    def test_scoped_is_the_table_itself(self):
        manager, _, _ = fake_manager("postgresql")
        assert manager.scoped("orders") == "orders"
        assert manager.scoped("app.orders") == "app.orders"

    def test_enable_casts_the_setting_to_the_column_type(self):
        manager, cursor, conn = fake_manager(
            "postgresql", fetch_results=[{"column_type": "uuid"}]
        )
        manager.enable_row_security("app.orders", tenant_column="tenant_id")
        lookup_sql, lookup_params = cursor.executed[0]
        assert "pg_attribute" in lookup_sql
        assert "%(table)s::regclass" in lookup_sql
        assert lookup_params == {"table": "app.orders", "column": "tenant_id"}
        ddl = [sql for sql, _ in cursor.executed[1:]]
        predicate = (
            "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        )
        assert ddl == [
            "ALTER TABLE app.orders ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE app.orders FORCE ROW LEVEL SECURITY",
            "DROP POLICY IF EXISTS nexusql_tenant_isolation ON app.orders",
            f"CREATE POLICY nexusql_tenant_isolation ON app.orders FOR ALL "
            f"USING ({predicate}) WITH CHECK ({predicate})",
        ]
        assert conn.commits == 1

    def test_quoted_column_is_looked_up_without_quotes(self):
        manager, cursor, _ = fake_manager(
            "postgresql", fetch_results=[{"column_type": "integer"}]
        )
        manager.enable_row_security("orders", tenant_column='"TenantId"')
        assert cursor.executed[0][1] == {"table": "orders", "column": "TenantId"}
        assert '"TenantId" = NULLIF' in cursor.executed[-1][0]

    def test_missing_column_is_reported(self):
        manager, cursor, conn = fake_manager("postgresql", fetch_results=[None])
        with pytest.raises(ValueError, match="not found"):
            manager.enable_row_security("orders", tenant_column="nope")
        assert len(cursor.executed) == 1
        assert conn.rollbacks == 1

    def test_disable_reverses_enable(self):
        manager, cursor, _ = fake_manager("postgresql")
        manager.disable_row_security("orders")
        assert [sql for sql, _ in cursor.executed] == [
            "DROP POLICY IF EXISTS nexusql_tenant_isolation ON orders",
            "ALTER TABLE orders NO FORCE ROW LEVEL SECURITY",
            "ALTER TABLE orders DISABLE ROW LEVEL SECURITY",
        ]


class TestMysqlBranch:
    def test_tenant_sets_then_clears_the_user_variable(self):
        """User variables outlive the transaction, so the exit must reset them."""
        manager, cursor, conn = fake_manager("mysql")
        with manager.tenant(42):
            pass
        assert cursor.executed == [
            ("SET @app_tenant_id = %s", ("42",)),
            ("SET @app_tenant_id = %s", (None,)),
        ]
        assert conn.commits == 1

    def test_nested_block_writes_the_outer_tenant_back(self):
        manager, cursor, _ = fake_manager("mysql")
        with manager.tenant("a"):
            with manager.tenant("b"):
                pass
        assert [p[0] for _, p in cursor.executed] == ["a", "b", "a", None]

    def test_enable_creates_the_function_once_then_the_view(self):
        manager, cursor, _ = fake_manager("mysql", fetch_results=[{"n": 0}])
        manager.enable_row_security("orders")
        statements = [sql for sql, _ in cursor.executed]
        assert "information_schema.routines" in statements[0]
        assert statements[1].startswith("CREATE FUNCTION nexusql_tenant_id()")
        assert statements[1].endswith("RETURN @app_tenant_id")
        assert statements[2] == (
            "CREATE OR REPLACE VIEW orders_scoped AS "
            "SELECT * FROM orders WHERE tenant_id = nexusql_tenant_id()"
        )

    def test_existing_function_is_kept(self):
        manager, cursor, _ = fake_manager("mysql", fetch_results=[{"n": 1}])
        manager.enable_row_security("orders")
        statements = [sql for sql, _ in cursor.executed]
        assert len(statements) == 2
        assert not any(s.startswith("CREATE FUNCTION") for s in statements)

    def test_scoped_and_disable(self):
        manager, cursor, _ = fake_manager("mysql")
        assert manager.scoped("orders") == "orders_scoped"
        manager.disable_row_security("orders")
        assert [sql for sql, _ in cursor.executed] == ["DROP VIEW IF EXISTS orders_scoped"]


class TestMssqlBranch:
    def test_tenant_uses_session_context_and_clears_it(self):
        manager, cursor, conn = fake_manager("mssql")
        with manager.tenant("42"):
            pass
        assert cursor.executed == [
            ("EXEC sp_set_session_context N'tenant_id', ?", ("42",)),
            ("EXEC sp_set_session_context N'tenant_id', ?", (None,)),
        ]
        assert conn.commits == 1

    def test_enable_creates_a_view_on_session_context(self):
        manager, cursor, _ = fake_manager("mssql")
        manager.enable_row_security("dbo.orders")
        assert [sql for sql, _ in cursor.executed] == [
            "CREATE OR ALTER VIEW dbo.orders_scoped AS SELECT * FROM dbo.orders "
            "WHERE tenant_id = CAST(SESSION_CONTEXT(N'tenant_id') AS NVARCHAR(128))"
        ]
        assert manager.scoped("dbo.orders") == "dbo.orders_scoped"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    @pytest.mark.parametrize(
        "db_type, table, expected",
        [
            (DatabaseType.POSTGRESQL, "orders", "orders"),
            (DatabaseType.POSTGRESQL, '"Orders"', '"Orders"'),
            (DatabaseType.SQLITE, "orders", "orders_scoped"),
            (DatabaseType.MYSQL, "app.orders", "app.orders_scoped"),
            (DatabaseType.MSSQL, 'dbo."Orders"', 'dbo."Orders_scoped"'),
            (DatabaseType.SQLITE, '"my table"', '"my table_scoped"'),
        ],
    )
    def test_scoped_name(self, db_type, table, expected):
        assert scoped_name(db_type, table) == expected

    def test_sqlite_statements_replace_the_view(self):
        assert row_security_statements(DatabaseType.SQLITE, "t", "org") == [
            "DROP VIEW IF EXISTS t_scoped",
            "CREATE VIEW t_scoped AS SELECT * FROM t WHERE org = app_tenant_id()",
        ]
        assert disable_row_security_statements(DatabaseType.SQLITE, "t") == [
            "DROP VIEW IF EXISTS t_scoped"
        ]

    def test_postgres_without_a_column_type_compares_as_text(self):
        policy = row_security_statements(DatabaseType.POSTGRESQL, "t")[-1]
        assert "USING (tenant_id::text = NULLIF(current_setting('app.tenant_id', true), ''))" in policy

    def test_unknown_dialect_is_rejected(self):
        with pytest.raises(ValueError, match="not supported"):
            row_security_statements(DatabaseType.DUCKDB, "t")
        with pytest.raises(ValueError, match="not supported"):
            disable_row_security_statements(DatabaseType.DUCKDB, "t")

    @pytest.mark.parametrize("name", ["t", "T_1", '"Mixed Case"', "s.t", 'app."T"'])
    def test_identifiers_accepted(self, name):
        assert check_identifier(name, qualified=True) == name

    @pytest.mark.parametrize("name", ["s.t", "t;", "t t", "1t", "", None, 't"', "a.b.c"])
    def test_identifiers_refused(self, name):
        with pytest.raises(ValueError):
            check_identifier(name)

    def test_tenant_value_is_text(self):
        assert tenant_value(7) == "7"
        assert tenant_value("x") == "x"
        with pytest.raises(ValueError):
            tenant_value(None)
