"""
db.cdc: trigger-based change data capture.

The SQLite tests drive the real triggers without a server. MySQL and SQL
Server are checked against fake connections that record the DDL ``enable``
would run and answer the column introspection; the live backends are covered
by tests/integration/test_cdc.py when reachable.
"""

import pytest

from nexusql import ConnectionConfig, DatabaseManager, DatabaseType
from nexusql.cdc import (
    ChangeDataCapture,
    changes_sql,
    changes_table,
    decode_change,
    disable_statements,
    drop_statements,
    enable_statements,
    function_name,
    trigger_names,
    validate_identifier,
)


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute(
        "CREATE TABLE items (id SERIAL PRIMARY KEY, name TEXT, qty INTEGER, note TEXT)"
    )
    yield manager
    try:
        manager.disconnect()
    except Exception:
        pass


def insert_update_delete(db, name="widget"):
    new_id = db.execute(
        "INSERT INTO items (name, qty) VALUES (:name, :qty)",
        {"name": name, "qty": 1}, return_id=True,
    )
    db.execute("UPDATE items SET qty = :qty WHERE id = :id", {"qty": 2, "id": new_id})
    db.execute("DELETE FROM items WHERE id = :id", {"id": new_id})
    return new_id


# ---------------------------------------------------------------------------
# SQLite: the real trigger path
# ---------------------------------------------------------------------------

class TestSqlite:
    def test_property_is_lazy_and_cached(self, db):
        assert isinstance(db.cdc, ChangeDataCapture)
        assert db.cdc is db.cdc

    def test_insert_update_delete_produce_three_changes(self, db):
        db.cdc.enable("items")
        new_id = insert_update_delete(db)

        changes = db.cdc.changes("items")
        assert [c["op"] for c in changes] == ["I", "U", "D"]
        assert [c["pk"] for c in changes] == [str(new_id)] * 3
        assert [c["change_id"] for c in changes] == [1, 2, 3]

        inserted, updated, deleted = changes
        row_v1 = {"id": new_id, "name": "widget", "qty": 1, "note": None}
        row_v2 = {"id": new_id, "name": "widget", "qty": 2, "note": None}
        assert inserted["old_row"] is None and inserted["new_row"] == row_v1
        assert updated["old_row"] == row_v1 and updated["new_row"] == row_v2
        assert deleted["old_row"] == row_v2 and deleted["new_row"] is None
        assert all(c["changed_at"] for c in changes)

    def test_unicode_and_quotes_survive_the_json_round_trip(self, db):
        db.cdc.enable("items")
        name = "héllo \"quoted\" 'single' \\ 日本"
        db.execute("INSERT INTO items (name) VALUES (:name)", {"name": name})
        assert db.cdc.changes("items")[0]["new_row"]["name"] == name

    def test_is_enabled(self, db):
        assert db.cdc.is_enabled("items") is False
        db.cdc.enable("items")
        assert db.cdc.is_enabled("items") is True
        db.cdc.disable("items")
        assert db.cdc.is_enabled("items") is False

    def test_enable_is_idempotent_and_keeps_rows(self, db):
        db.cdc.enable("items")
        db.execute("INSERT INTO items (name) VALUES (:name)", {"name": "a"})
        db.cdc.enable("items")
        db.execute("INSERT INTO items (name) VALUES (:name)", {"name": "b"})
        # Still one row per insert: the triggers were replaced, not doubled.
        assert [c["new_row"]["name"] for c in db.cdc.changes("items")] == ["a", "b"]

    def test_re_enable_picks_up_new_columns(self, db):
        db.cdc.enable("items")
        db.execute("ALTER TABLE items ADD COLUMN colour TEXT")
        db.execute("INSERT INTO items (name, colour) VALUES (:n, :c)", {"n": "x", "c": "red"})
        assert "colour" not in db.cdc.changes("items")[0]["new_row"]

        db.cdc.enable("items")
        db.execute("INSERT INTO items (name, colour) VALUES (:n, :c)", {"n": "y", "c": "blue"})
        assert db.cdc.changes("items")[1]["new_row"]["colour"] == "blue"

    def test_disable_stops_capture_but_keeps_history(self, db):
        db.cdc.enable("items")
        insert_update_delete(db)
        db.cdc.disable("items")
        insert_update_delete(db, name="unseen")
        changes = db.cdc.changes("items")
        assert len(changes) == 3
        assert all(c["pk"] == "1" for c in changes)

    def test_drop_removes_triggers_and_table(self, db):
        db.cdc.enable("items")
        insert_update_delete(db)
        db.cdc.drop("items")
        assert not db.cdc.is_enabled("items")
        assert not db.table_exists(changes_table("items"))
        # Writes still work on the source table once capture is gone.
        db.execute("INSERT INTO items (name) VALUES (:name)", {"name": "after"})
        # Dropping twice is harmless.
        db.cdc.drop("items")

    def test_since_id_and_limit_page_through(self, db):
        db.cdc.enable("items")
        for i in range(5):
            db.execute("INSERT INTO items (name) VALUES (:name)", {"name": str(i)})
        first = db.cdc.changes("items", limit=2)
        assert [c["change_id"] for c in first] == [1, 2]
        rest = db.cdc.changes("items", since_id=first[-1]["change_id"])
        assert [c["change_id"] for c in rest] == [3, 4, 5]
        assert db.cdc.changes("items", since_id=5) == []

    def test_purge(self, db):
        db.cdc.enable("items")
        for i in range(4):
            db.execute("INSERT INTO items (name) VALUES (:name)", {"name": str(i)})
        db.cdc.purge("items", before_id=3)
        assert [c["change_id"] for c in db.cdc.changes("items")] == [3, 4]

    def test_custom_primary_key_column(self, db):
        db.execute("CREATE TABLE codes (code TEXT PRIMARY KEY, label TEXT)")
        db.cdc.enable("codes", pk="code")
        db.execute("INSERT INTO codes (code, label) VALUES (:c, :l)", {"c": "AB", "l": "x"})
        change = db.cdc.changes("codes")[0]
        assert change["pk"] == "AB" and change["new_row"] == {"code": "AB", "label": "x"}

    def test_capture_inside_a_transaction_rolls_back_with_it(self, db):
        db.cdc.enable("items")
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO items (name) VALUES (:name)", {"name": "x"})
                raise RuntimeError("abort")
        assert db.cdc.changes("items") == []

    def test_unknown_table_and_pk_are_rejected(self, db):
        with pytest.raises(ValueError, match="does not exist"):
            db.cdc.enable("missing")
        with pytest.raises(ValueError, match="not a column"):
            db.cdc.enable("items", pk="nope")
        assert not db.table_exists(changes_table("items"))

    @pytest.mark.parametrize("bad", ["", "1abc", "a-b", "a.b", 'a"b', "a b", "x" * 55])
    def test_malformed_identifiers_are_rejected(self, db, bad):
        for call in (db.cdc.enable, db.cdc.disable, db.cdc.drop,
                     db.cdc.is_enabled, db.cdc.changes):
            with pytest.raises(ValueError, match="Invalid table name"):
                call(bad)
        with pytest.raises(ValueError, match="Invalid primary key column"):
            db.cdc.enable("items", pk=bad)

    def test_bad_limit(self, db):
        db.cdc.enable("items")
        with pytest.raises(ValueError, match="limit must be positive"):
            db.cdc.changes("items", limit=0)


# ---------------------------------------------------------------------------
# MySQL and SQL Server against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """
    Answers the column introspection query with a fixed column list, with
    pymysql's dict rows or pyodbc's tuple rows plus description, and records
    everything else.
    """

    def __init__(self, columns, dialect):
        self._columns = columns
        self._dialect = dialect
        self.executed = []
        self._rows = []
        self.description = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "information_schema.columns" in sql:
            if self._dialect == "mysql":
                self._rows = [{"name": c} for c in self._columns]
            else:
                self.description = [("name", str)]
                self._rows = [(c,) for c in self._columns]
        else:
            self._rows = []
            self.description = None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


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

    def acquire(self, timeout=None):
        return self._conn

    def release(self, conn, *, discard=False):
        pass

    def close(self):
        pass


def fake_manager(dialect, columns):
    urls = {"mysql": "mysql://u:p@localhost/db", "mssql": "mssql://u:p@localhost/db"}
    manager = DatabaseManager(ConnectionConfig.from_url(urls[dialect]))
    cursor = FakeCursor(columns, dialect)
    conn = FakeConnection(cursor)
    manager._pool = FakePool(conn)
    return manager, cursor, conn


class TestMysqlFake:
    def test_enable_runs_the_generated_ddl_in_order(self):
        manager, cursor, conn = fake_manager("mysql", ["id", "name", "qty"])
        manager.cdc.enable("orders")

        introspect, *ddl = cursor.executed
        assert "information_schema.columns" in introspect[0]
        assert introspect[1] == ("orders",)
        statements = [sql for sql, _ in ddl]
        assert statements == enable_statements(
            DatabaseType.MYSQL, "orders", "id", ["id", "name", "qty"])
        # Two commits: one ends the catalog read that lists the columns (reads
        # outside a transaction commit so the connection goes back idle), one
        # is the DDL transaction.
        assert conn.commits == 2

    def test_generated_ddl(self):
        statements = enable_statements(DatabaseType.MYSQL, "orders", "id", ["id", "name"])
        create, drop_i, drop_u, drop_d, trig_i, trig_u, trig_d = statements

        assert create.startswith("CREATE TABLE IF NOT EXISTS `orders__changes`")
        assert "change_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY" in create
        assert "old_row LONGTEXT" in create and "new_row LONGTEXT" in create
        assert [drop_i, drop_u, drop_d] == [
            "DROP TRIGGER IF EXISTS `orders__cdc_i`",
            "DROP TRIGGER IF EXISTS `orders__cdc_u`",
            "DROP TRIGGER IF EXISTS `orders__cdc_d`",
        ]
        assert trig_i.startswith("CREATE TRIGGER `orders__cdc_i` AFTER INSERT ON `orders` FOR EACH ROW")
        assert "VALUES ('I', CAST(NEW.`id` AS CHAR), NULL, " in trig_i
        assert "JSON_OBJECT('id', NEW.`id`, 'name', NEW.`name`)" in trig_i
        assert "JSON_OBJECT('id', OLD.`id`, 'name', OLD.`name`), JSON_OBJECT('id', NEW.`id`, 'name', NEW.`name`)" in trig_u
        assert "AFTER DELETE ON `orders`" in trig_d
        assert "VALUES ('D', CAST(OLD.`id` AS CHAR), JSON_OBJECT('id', OLD.`id`, 'name', OLD.`name`), NULL)" in trig_d
        # Single-statement bodies: no BEGIN/END, so no DELIMITER problem.
        assert "BEGIN" not in trig_i

    def test_disable_and_drop_ddl(self):
        assert disable_statements(DatabaseType.MYSQL, "orders") == [
            "DROP TRIGGER IF EXISTS `orders__cdc_i`",
            "DROP TRIGGER IF EXISTS `orders__cdc_u`",
            "DROP TRIGGER IF EXISTS `orders__cdc_d`",
        ]
        assert drop_statements(DatabaseType.MYSQL, "orders")[-1] == (
            "DROP TABLE IF EXISTS `orders__changes`")

    def test_changes_query_uses_limit(self):
        sql = changes_sql(DatabaseType.MYSQL, "orders")
        assert sql.endswith("WHERE change_id > :since ORDER BY change_id LIMIT :limit")
        assert "`orders__changes`" in sql


class TestMssqlFake:
    def test_enable_runs_the_generated_ddl_one_batch_each(self):
        manager, cursor, conn = fake_manager("mssql", ["id", "name"])
        manager.cdc.enable("orders")

        introspect, *ddl = cursor.executed
        assert "information_schema.columns" in introspect[0]
        assert introspect[1] == ("orders",)
        statements = [sql for sql, _ in ddl]
        assert statements == enable_statements(DatabaseType.MSSQL, "orders", "id", ["id", "name"])
        # CREATE TRIGGER must be alone in its batch: one execute per statement.
        assert sum(s.startswith("CREATE TRIGGER") for s in statements) == 3
        # Two commits: one ends the catalog read that lists the columns (reads
        # outside a transaction commit so the connection goes back idle), one
        # is the DDL transaction.
        assert conn.commits == 2

    def test_generated_ddl(self):
        statements = enable_statements(DatabaseType.MSSQL, "orders", "id", ["id", "name"])
        create, drop_i, drop_u, drop_d, trig_i, trig_u, trig_d = statements

        assert create.startswith("IF OBJECT_ID(N'[orders__changes]', N'U') IS NULL\nCREATE TABLE [orders__changes]")
        assert "change_id BIGINT IDENTITY(1,1) PRIMARY KEY" in create
        assert "old_row NVARCHAR(MAX)" in create
        assert "changed_at DATETIME2" in create
        assert drop_i == "IF OBJECT_ID(N'[orders__cdc_i]', N'TR') IS NOT NULL DROP TRIGGER [orders__cdc_i]"

        assert trig_i.startswith("CREATE TRIGGER [orders__cdc_i] ON [orders] AFTER INSERT AS")
        json_new = "(SELECT n.[id], n.[name] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES)"
        json_old = "(SELECT o.[id], o.[name] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES)"
        assert f"SELECT 'I', CAST(n.[id] AS NVARCHAR(MAX)), NULL, {json_new}" in trig_i
        assert "FROM inserted AS n" in trig_i

        assert "AFTER UPDATE AS" in trig_u
        assert json_old in trig_u and json_new in trig_u
        assert "LEFT JOIN deleted AS o ON o.[id] = n.[id]" in trig_u

        assert "AFTER DELETE AS" in trig_d
        assert f"SELECT 'D', CAST(o.[id] AS NVARCHAR(MAX)), {json_old}, NULL" in trig_d
        assert "FROM deleted AS o" in trig_d

    def test_drop_ddl(self):
        assert drop_statements(DatabaseType.MSSQL, "orders")[-1] == (
            "IF OBJECT_ID(N'[orders__changes]', N'U') IS NOT NULL DROP TABLE [orders__changes]")

    def test_changes_query_uses_top(self):
        sql = changes_sql(DatabaseType.MSSQL, "orders")
        assert sql.startswith("SELECT TOP (:limit) ")
        assert sql.endswith("WHERE change_id > :since ORDER BY change_id")
        assert "LIMIT" not in sql


# ---------------------------------------------------------------------------
# PostgreSQL and SQLite DDL text, and the pure helpers
# ---------------------------------------------------------------------------

class TestGeneratedDdl:
    def test_postgres(self):
        statements = enable_statements(DatabaseType.POSTGRESQL, "orders", "id", ["id", "name"])
        create, drop_i, drop_u, drop_d, drop_fn, fn, trig_i, trig_u, trig_d = statements

        assert create.startswith('CREATE TABLE IF NOT EXISTS "orders__changes"')
        assert "change_id BIGSERIAL PRIMARY KEY" in create
        assert drop_i == 'DROP TRIGGER IF EXISTS "orders__cdc_i" ON "orders"'
        assert drop_fn == 'DROP FUNCTION IF EXISTS "orders__cdc_fn"()'
        assert fn.startswith('CREATE FUNCTION "orders__cdc_fn"() RETURNS trigger LANGUAGE plpgsql AS $$')
        assert "row_to_json(NEW)::text" in fn and "row_to_json(OLD)::text" in fn
        assert 'NEW."id"::text' in fn and 'OLD."id"::text' in fn
        # The function body reads the row at fire time, so the column list
        # is not baked in.
        assert "name" not in fn
        assert trig_i == ('CREATE TRIGGER "orders__cdc_i" AFTER INSERT ON "orders" '
                          'FOR EACH ROW EXECUTE FUNCTION "orders__cdc_fn"()')
        assert "AFTER UPDATE" in trig_u and "AFTER DELETE" in trig_d

    def test_sqlite(self):
        statements = enable_statements(DatabaseType.SQLITE, "orders", "id", ["id", "name"])
        create, drop_i, drop_u, drop_d, trig_i, trig_u, trig_d = statements
        assert "change_id INTEGER PRIMARY KEY AUTOINCREMENT" in create
        assert drop_i == 'DROP TRIGGER IF EXISTS "orders__cdc_i"'
        assert """json_object('id', NEW."id", 'name', NEW."name")""" in trig_i
        assert 'CAST(NEW."id" AS TEXT)' in trig_i
        assert """json_object('id', OLD."id", 'name', OLD."name")""" in trig_d

    def test_pk_must_be_a_column(self):
        with pytest.raises(ValueError, match="not a column"):
            enable_statements(DatabaseType.SQLITE, "orders", "nope", ["id"])

    def test_names(self):
        assert changes_table("orders") == "orders__changes"
        assert function_name("orders") == "orders__cdc_fn"
        assert trigger_names("orders") == {
            "I": "orders__cdc_i", "U": "orders__cdc_u", "D": "orders__cdc_d"}

    def test_validate_identifier(self):
        assert validate_identifier("ok_Name9") == "ok_Name9"
        assert validate_identifier("x" * 54) == "x" * 54
        for bad in ["", "9x", "a-b", "a.b", "x" * 55, "a;DROP TABLE t", None]:
            with pytest.raises(ValueError):
                validate_identifier(bad)

    def test_decode_change(self):
        row = {"change_id": 1, "op": "U", "pk": "1",
               "old_row": '{"a": 1}', "new_row": '{"a": 2}', "changed_at": "t"}
        assert decode_change(row)["old_row"] == {"a": 1}
        assert decode_change(row)["new_row"] == {"a": 2}
        assert decode_change({"old_row": None, "new_row": '{}'}) == {"old_row": None, "new_row": {}}
