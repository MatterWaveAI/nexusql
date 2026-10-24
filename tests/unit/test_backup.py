"""
backup()/restore() and export_tables()/import_tables().

SQLite exercises the real paths without a server. The pg_dump/pg_restore and
mysqldump/mysql branches run against a fake subprocess.run so the exact
command line, and the password travelling in the environment rather than in
it, are pinned down whether or not the tools are installed (they are not on
most dev machines). The SQL Server branch runs against a pyodbc-shaped fake
connection. Live backends are covered by tests/integration/test_backup.py.
"""

import datetime as dt
import decimal
import json
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager

import pytest

from nexusql import BackupError, BackupToolNotFound, ConnectionConfig, DatabaseManager
from nexusql import backup as backup_module
from nexusql.backup import (
    backup_database,
    connection_fields,
    decode_value,
    discover_tables,
    dumps_row,
    encode_value,
    loads_row,
    mssql_backup_sql,
    mssql_master_url,
    mssql_restore_sql,
    quote_identifier,
    read_manifest,
    restore_database,
)

SCHEMA = (
    "CREATE TABLE items (id SERIAL PRIMARY KEY, name TEXT NOT NULL, "
    "payload BLOB, created TIMESTAMP, active BOOLEAN)"
)

ROWS = [
    {"name": "alpha", "payload": b"\x00\x01\xfe\xff", "created": "2024-05-06 07:08:09", "active": True},
    {"name": "beta", "payload": None, "created": None, "active": False},
    {"name": "gamma", "payload": b"", "created": "2025-01-01 00:00:00", "active": True},
]


def seed(manager):
    for row in ROWS:
        manager.execute(
            "INSERT INTO items (name, payload, created, active) "
            "VALUES (:name, :payload, :created, :active)",
            row,
        )


def all_rows(manager):
    return manager.execute("SELECT * FROM items ORDER BY id")


@pytest.fixture
def db(temp_db_path):
    url = f"sqlite:///{temp_db_path}"
    manager = DatabaseManager(url)
    assert manager.connect(), "SQLite connect failed"
    manager.execute(SCHEMA)
    seed(manager)
    yield manager, url
    try:
        manager.disconnect()
    except Exception:
        pass


def unconnected(url: str) -> DatabaseManager:
    """A manager with dialect config but no live connection."""
    return DatabaseManager(ConnectionConfig.from_url(url))


# ---------------------------------------------------------------------------
# SQLite: the online backup API
# ---------------------------------------------------------------------------

class TestSqliteBackupRestore:
    def test_backup_file_is_a_complete_copy(self, db, tmp_path):
        manager, _ = db
        target = tmp_path / "copies" / "items.sqlite"
        assert manager.backup(str(target)) == str(target)

        copy = sqlite3.connect(str(target))
        try:
            copy.row_factory = sqlite3.Row
            names = [r["name"] for r in copy.execute("SELECT name FROM items ORDER BY id")]
            payload = copy.execute("SELECT payload FROM items WHERE id = 1").fetchone()[0]
        finally:
            copy.close()
        assert names == ["alpha", "beta", "gamma"]
        assert payload == b"\x00\x01\xfe\xff"

    def test_restore_overwrites_the_live_database(self, db, tmp_path):
        manager, url = db
        before = all_rows(manager)
        backup = tmp_path / "items.bak"
        manager.backup(str(backup))

        manager.execute("DELETE FROM items WHERE name = :n", {"n": "alpha"})
        manager.execute("INSERT INTO items (name) VALUES (:n)", {"n": "intruder"})
        assert [r["name"] for r in all_rows(manager)] == ["beta", "gamma", "intruder"]

        manager.restore(str(backup))
        assert all_rows(manager) == before

        # The restored state is on disk, not just in this connection.
        reader = DatabaseManager(url)
        assert reader.connect()
        try:
            assert all_rows(reader) == before
        finally:
            reader.disconnect()

    def test_manager_keeps_working_after_restore(self, db, tmp_path):
        manager, _ = db
        backup = tmp_path / "items.bak"
        manager.backup(str(backup))
        manager.restore(str(backup))
        new_id = manager.execute(
            "INSERT INTO items (name) VALUES (:n)", {"n": "after"}, return_id=True
        )
        assert new_id == 4

    def test_tables_argument_is_rejected(self, db, tmp_path):
        manager, _ = db
        with pytest.raises(ValueError, match="whole-database"):
            manager.backup(str(tmp_path / "x.bak"), tables=["items"])

    def test_restore_missing_file(self, db, tmp_path):
        manager, _ = db
        with pytest.raises(FileNotFoundError):
            manager.restore(str(tmp_path / "nope.bak"))

    def test_restore_inside_transaction_is_refused(self, db, tmp_path):
        manager, _ = db
        backup = tmp_path / "items.bak"
        manager.backup(str(backup))
        with manager.transaction():
            with pytest.raises(BackupError, match="transaction"):
                manager.restore(str(backup))


# ---------------------------------------------------------------------------
# Portable export and import on SQLite
# ---------------------------------------------------------------------------

class TestExportImport:
    def test_round_trip_with_bytes_and_datetime(self, db, tmp_path):
        manager, _ = db
        before = all_rows(manager)
        out = tmp_path / "export"

        manifest = manager.export_tables(str(out), tables=["items"])

        from nexusql import __version__
        assert manifest["format"] == "nexusql-export"
        assert manifest["nexusql_version"] == __version__
        assert manifest["dialect"] == "sqlite"
        assert manifest["schema"] == "main"
        entry, = manifest["tables"]
        assert entry["name"] == "items"
        assert entry["columns"] == ["id", "name", "payload", "created", "active"]
        assert entry["row_count"] == 3
        assert (out / "manifest.json").is_file()
        assert read_manifest(str(out)) == manifest

        lines = (out / entry["file"]).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first[1] == "alpha"
        assert first[2] == {"$bytes": "AAH+/w=="}

        manager.execute("DELETE FROM items")
        assert manager.import_tables(str(out)) == {"items": 3}
        assert all_rows(manager) == before

    def test_import_into_a_fresh_database(self, db, tmp_path):
        manager, _ = db
        out = tmp_path / "export"
        manager.export_tables(str(out))

        other = DatabaseManager(f"sqlite:///{tmp_path / 'other.db'}")
        assert other.connect()
        try:
            other.execute(SCHEMA)
            assert other.import_tables(str(out)) == {"items": 3}
            assert all_rows(other) == all_rows(manager)
            # AUTOINCREMENT carried on from the imported keys.
            assert other.execute(
                "INSERT INTO items (name) VALUES (:n)", {"n": "d"}, return_id=True
            ) == 4
        finally:
            other.disconnect()

    def test_discovery_skips_sqlite_bookkeeping_tables(self, db, tmp_path):
        manager, _ = db
        # SERIAL PRIMARY KEY becomes AUTOINCREMENT, which creates
        # sqlite_sequence; the engine owns that table.
        internal = manager.execute(
            "SELECT name FROM sqlite_master WHERE name = 'sqlite_sequence'"
        )
        assert internal, "expected sqlite_sequence to exist"
        assert discover_tables(manager) == ["items"]
        manifest = manager.export_tables(str(tmp_path / "all"))
        assert [t["name"] for t in manifest["tables"]] == ["items"]

    def test_truncate_replaces_and_default_appends(self, db, tmp_path):
        manager, _ = db
        out = tmp_path / "export"
        manager.export_tables(str(out))

        # Exported rows keep their keys, so appending only makes sense onto
        # rows that do not share them.
        manager.execute("DELETE FROM items")
        manager.execute("INSERT INTO items (id, name) VALUES (:id, :n)", {"id": 100, "n": "kept"})
        manager.import_tables(str(out))
        assert [r["name"] for r in all_rows(manager)] == ["alpha", "beta", "gamma", "kept"]

        manager.import_tables(str(out), truncate=True)
        assert [r["name"] for r in all_rows(manager)] == ["alpha", "beta", "gamma"]

    def test_tables_filter(self, db, tmp_path):
        manager, _ = db
        manager.execute("CREATE TABLE tags (id SERIAL PRIMARY KEY, label TEXT)")
        manager.execute("INSERT INTO tags (label) VALUES (:l)", {"l": "x"})
        out = tmp_path / "export"
        manifest = manager.export_tables(str(out))
        assert [t["name"] for t in manifest["tables"]] == ["items", "tags"]

        manager.execute("DELETE FROM tags")
        manager.execute("DELETE FROM items")
        assert manager.import_tables(str(out), tables=["tags"]) == {"tags": 1}
        assert manager.fetch_one("SELECT COUNT(*) AS n FROM items") == {"n": 0}

        with pytest.raises(BackupError, match="not present in the export manifest"):
            manager.import_tables(str(out), tables=["nothing_here"])

    def test_empty_table_keeps_its_columns(self, db, tmp_path):
        manager, _ = db
        manager.execute("DELETE FROM items")
        manifest = manager.export_tables(str(tmp_path / "export"))
        entry, = manifest["tables"]
        assert entry["row_count"] == 0
        assert entry["columns"] == ["id", "name", "payload", "created", "active"]

    def test_bad_row_rolls_the_table_back(self, db, tmp_path):
        manager, _ = db
        out = tmp_path / "export"
        manifest = manager.export_tables(str(out))
        data_file = out / manifest["tables"][0]["file"]
        with open(data_file, "a", encoding="utf-8") as fh:
            fh.write('[99,"short"]\n')

        with pytest.raises(BackupError, match="row 4 has 2 values"):
            manager.import_tables(str(out), truncate=True)
        # The DELETE ran in the same transaction and was rolled back with it.
        assert [r["name"] for r in all_rows(manager)] == ["alpha", "beta", "gamma"]

    def test_missing_target_table_raises(self, db, tmp_path):
        manager, _ = db
        out = tmp_path / "export"
        manager.export_tables(str(out))
        manager.execute("DROP TABLE items")
        with pytest.raises(sqlite3.OperationalError):
            manager.import_tables(str(out))

    def test_foreign_or_missing_manifest(self, db, tmp_path):
        manager, _ = db
        with pytest.raises(FileNotFoundError):
            manager.import_tables(str(tmp_path / "nowhere"))
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "manifest.json").write_text('{"format": "something-else"}', encoding="utf-8")
        with pytest.raises(BackupError, match="not a nexusql-export manifest"):
            manager.import_tables(str(bad))
        (bad / "manifest.json").write_text(
            '{"format": "nexusql-export", "format_version": 99, "tables": []}', encoding="utf-8"
        )
        with pytest.raises(BackupError, match="format version 99"):
            manager.import_tables(str(bad))


# ---------------------------------------------------------------------------
# JSON codec
# ---------------------------------------------------------------------------

class TestJsonCodec:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            1,
            1.5,
            True,
            "text with unicode \u00e9\u4e2d",
            b"\x00\xff\x10",
            bytearray(b"abc"),
            dt.datetime(2024, 5, 6, 7, 8, 9, 123456),
            dt.datetime(2024, 5, 6, 7, 8, 9, tzinfo=dt.timezone(dt.timedelta(hours=2))),
            dt.date(2024, 5, 6),
            dt.time(7, 8, 9, 5),
            decimal.Decimal("12.50"),
            decimal.Decimal("-0.000001"),
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
            {"a": [1, 2, {"b": None}]},
            [1, "two", 3.0],
        ],
    )
    def test_round_trip(self, value):
        decoded, = loads_row(dumps_row([value]))
        if isinstance(value, bytearray):
            assert decoded == bytes(value)
        elif isinstance(value, decimal.Decimal):
            assert decoded == value and str(decoded) == str(value)
        else:
            assert decoded == value
            assert type(decoded) is type(value)

    def test_memoryview_is_written_as_bytes(self):
        assert encode_value(memoryview(b"xy")) == {"$bytes": "eHk="}

    def test_unknown_type_is_refused(self):
        with pytest.raises(TypeError, match="cannot encode object"):
            dumps_row([object()])

    def test_ordinary_objects_pass_through_the_decoder(self):
        assert decode_value({"$bytes": "AA==", "other": 1}) == {"$bytes": "AA==", "other": 1}
        assert decode_value({"name": "x"}) == {"name": "x"}
        assert decode_value({}) == {}


# ---------------------------------------------------------------------------
# pg_dump / pg_restore and mysqldump / mysql: command line and environment
# ---------------------------------------------------------------------------

class ToolRecorder:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, env=None, stdin=None, **kwargs):
        self.calls.append({"argv": list(argv), "env": dict(env), "stdin": stdin, "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


@pytest.fixture
def tools(monkeypatch):
    """Pretend every client binary is installed and record what gets run."""
    recorder = ToolRecorder()
    monkeypatch.setattr(backup_module.shutil, "which", lambda name: f"/opt/db/bin/{name}")
    monkeypatch.setattr(backup_module.subprocess, "run", recorder)
    monkeypatch.setenv("PGSSLMODE", "require")
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("MYSQL_PWD", raising=False)
    return recorder


PG_URL = "postgresql://alice:s3cr%40t%2Fx@db.example:5433/shop?sslmode=require"


class TestPostgresTools:
    def test_pg_dump_argv_and_password_in_env(self, tools, tmp_path):
        manager = unconnected(PG_URL)
        target = tmp_path / "shop.dump"
        assert manager.backup(str(target), tables=["orders", "public.items"]) == str(target)

        call, = tools.calls
        assert call["argv"] == [
            "/opt/db/bin/pg_dump",
            "--format=custom",
            "--no-password",
            "--host=db.example",
            "--port=5433",
            "--username=alice",
            "--dbname=shop",
            f"--file={target}",
            "--table=orders",
            "--table=public.items",
        ]
        assert call["env"]["PGPASSWORD"] == "s3cr@t/x"
        assert call["env"]["PGSSLMODE"] == "require", "caller's environment is inherited"
        joined = " ".join(call["argv"])
        assert "s3cr" not in joined and "%40" not in joined
        assert call["kwargs"]["capture_output"] is True

    def test_pg_dump_without_tables(self, tools, tmp_path):
        unconnected(PG_URL).backup(str(tmp_path / "all.dump"))
        argv = tools.calls[0]["argv"]
        assert not any(a.startswith("--table") for a in argv)

    @pytest.mark.parametrize("replace", [False, True])
    def test_pg_restore_argv(self, tools, tmp_path, replace):
        dump = tmp_path / "shop.dump"
        dump.write_bytes(b"PGDMP")
        unconnected(PG_URL).restore(str(dump), replace=replace)

        call, = tools.calls
        expected = [
            "/opt/db/bin/pg_restore",
            "--no-password",
            "--host=db.example",
            "--port=5433",
            "--username=alice",
            "--dbname=shop",
        ]
        if replace:
            expected += ["--clean", "--if-exists"]
        expected.append(str(dump))
        assert call["argv"] == expected
        assert call["env"]["PGPASSWORD"] == "s3cr@t/x"

    def test_restore_checks_the_file_before_the_binary(self, tools, tmp_path):
        with pytest.raises(FileNotFoundError):
            unconnected(PG_URL).restore(str(tmp_path / "missing.dump"))
        assert tools.calls == []

    def test_missing_binary_names_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backup_module.shutil, "which", lambda name: None)
        with pytest.raises(BackupToolNotFound, match="pg_dump was not found on PATH"):
            unconnected(PG_URL).backup(str(tmp_path / "x.dump"))
        with pytest.raises(BackupToolNotFound, match="pg_restore"):
            (tmp_path / "x.dump").write_bytes(b"PGDMP")
            unconnected(PG_URL).restore(str(tmp_path / "x.dump"))

    def test_tool_failure_surfaces_stderr(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backup_module.shutil, "which", lambda name: f"/opt/db/bin/{name}")
        monkeypatch.setattr(
            backup_module.subprocess, "run",
            ToolRecorder(returncode=1, stderr="pg_dump: error: connection refused\n"),
        )
        with pytest.raises(BackupError, match="pg_dump exited with status 1: pg_dump: error: connection refused"):
            unconnected(PG_URL).backup(str(tmp_path / "x.dump"))

    def test_url_without_password_adds_no_env(self, tools, tmp_path):
        unconnected("postgresql://alice@db.example/shop").backup(str(tmp_path / "x.dump"))
        call, = tools.calls
        assert "PGPASSWORD" not in call["env"]
        assert "--port" not in " ".join(call["argv"])
        assert "--host=db.example" in call["argv"]


MYSQL_URL = "mysql://bob:pa%3Ass@mysql.internal:3307/shop"


class TestMysqlTools:
    def test_mysqldump_argv_and_password_in_env(self, tools, tmp_path):
        target = tmp_path / "shop.sql"
        unconnected(MYSQL_URL).backup(str(target), tables=["orders"])
        call, = tools.calls
        assert call["argv"] == [
            "/opt/db/bin/mysqldump",
            "--single-transaction",
            "--protocol=TCP",
            "--host=mysql.internal",
            "--port=3307",
            "--user=bob",
            f"--result-file={target}",
            "shop",
            "orders",
        ]
        assert call["env"]["MYSQL_PWD"] == "pa:ss"
        assert "pa:ss" not in " ".join(call["argv"])

    def test_mysql_restore_reads_the_dump_on_stdin(self, tools, tmp_path):
        dump = tmp_path / "shop.sql"
        dump.write_text("CREATE TABLE t (a INT);\n", encoding="utf-8")
        unconnected(MYSQL_URL).restore(str(dump))
        call, = tools.calls
        assert call["argv"] == [
            "/opt/db/bin/mysql",
            "--protocol=TCP",
            "--host=mysql.internal",
            "--port=3307",
            "--user=bob",
            "shop",
        ]
        assert call["stdin"] is not None and call["stdin"].name == str(dump)
        assert call["env"]["MYSQL_PWD"] == "pa:ss"

    def test_missing_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(backup_module.shutil, "which", lambda name: None)
        with pytest.raises(BackupToolNotFound, match="mysqldump"):
            unconnected(MYSQL_URL).backup(str(tmp_path / "x.sql"))


# ---------------------------------------------------------------------------
# SQL Server: BACKUP / RESTORE T-SQL against a pyodbc-shaped fake
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params, self.conn.autocommit))

    def nextset(self):
        return False

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.autocommit = False
        self.executed = []
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeSideManager:
    """Stands in for the DatabaseManager opened on master for RESTORE."""

    def __init__(self, conn):
        self.conn = conn
        self.disconnected = False

    @contextmanager
    def _borrow(self):
        yield self.conn

    def disconnect(self):
        self.disconnected = True


MSSQL_URL = "mssql://sa:Pass@sql.internal:1433/shop?TrustServerCertificate=yes"


class TestMssqlBranch:
    def test_backup_runs_outside_a_transaction(self, tmp_path):
        manager = unconnected(MSSQL_URL)
        conn = FakeConnection()
        # No pool on an unconnected manager; pin the fake so _borrow yields it.
        manager._state.pinned = conn

        assert backup_database(manager, r"D:\backups\shop.bak") == r"D:\backups\shop.bak"
        assert conn.executed == [
            ("BACKUP DATABASE [shop] TO DISK = ?", (r"D:\backups\shop.bak",), True)
        ]
        assert conn.autocommit is False, "autocommit restored for the pool"
        assert all(c.closed for c in conn.cursors)

    def test_backup_rejects_tables(self):
        with pytest.raises(ValueError, match="whole-database"):
            backup_database(unconnected(MSSQL_URL), "x.bak", tables=["t"])

    @pytest.mark.parametrize("replace", [False, True])
    def test_restore_runs_from_master(self, monkeypatch, replace):
        conn = FakeConnection()
        side = FakeSideManager(conn)
        opened = []

        def open_side(url):
            opened.append(url)
            return side

        monkeypatch.setattr(backup_module, "_open_manager", open_side)
        manager = unconnected(MSSQL_URL)
        restore_database(manager, r"D:\backups\shop.bak", replace=replace)

        assert opened == ["mssql://sa:Pass@sql.internal:1433/master?TrustServerCertificate=yes"]
        options = "WITH RECOVERY, REPLACE" if replace else "WITH RECOVERY"
        assert conn.executed == [
            (f"RESTORE DATABASE [shop] FROM DISK = ? {options}", (r"D:\backups\shop.bak",), True)
        ]
        assert side.disconnected

    def test_restore_refuses_master_itself(self):
        with pytest.raises(BackupError, match="names the database"):
            restore_database(unconnected("mssql://sa:Pass@sql.internal/master"), "x.bak")

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("mssql://u:p@h:1433/shop", "mssql://u:p@h:1433/master"),
            ("mssql+pyodbc://u:p@h/shop?driver=ODBC+Driver+18", "mssql+pyodbc://u:p@h/master?driver=ODBC+Driver+18"),
            ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=h,1433;DATABASE=shop;UID=u;PWD=p",
             "DRIVER={ODBC Driver 18 for SQL Server};SERVER=h,1433;DATABASE=master;UID=u;PWD=p"),
            ("DRIVER={ODBC Driver 18 for SQL Server};SERVER=h;UID=u;PWD=p",
             "DRIVER={ODBC Driver 18 for SQL Server};SERVER=h;UID=u;PWD=p;DATABASE=master"),
        ],
    )
    def test_master_url(self, url, expected):
        assert mssql_master_url(url) == expected

    def test_sql_quotes_the_database_name(self):
        assert mssql_backup_sql("odd]name") == "BACKUP DATABASE [odd]]name] TO DISK = ?"
        assert mssql_restore_sql("shop", False).endswith("FROM DISK = ? WITH RECOVERY")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_quote_identifier_per_dialect(self):
        from nexusql import DatabaseType
        assert quote_identifier(DatabaseType.POSTGRESQL, 'we"ird') == '"we""ird"'
        assert quote_identifier(DatabaseType.SQLITE, "app.users") == '"app"."users"'
        assert quote_identifier(DatabaseType.MYSQL, "us`ers") == "`us``ers`"
        assert quote_identifier(DatabaseType.MSSQL, "dbo.us]ers") == "[dbo].[us]]ers]"

    def test_connection_fields_defaults_and_decoding(self):
        fields = connection_fields(unconnected("postgresql://a%20b:p%40w@/db"))
        assert fields == {
            "host": "localhost", "port": "", "user": "a b", "password": "p@w", "database": "db",
        }
        fields = connection_fields(unconnected("mysql://h/db"))
        assert fields["user"] is None and fields["password"] is None

    def test_config_fields_win_over_the_url(self):
        from nexusql import DatabaseType
        config = ConnectionConfig(
            database_type=DatabaseType.POSTGRESQL,
            database_url="postgresql://x:y@h/db",
            host="override", port=6000, username="u2", password="p2", database_name="db2",
        )
        fields = connection_fields(DatabaseManager(config))
        assert fields == {
            "host": "override", "port": "6000", "user": "u2", "password": "p2", "database": "db2",
        }
