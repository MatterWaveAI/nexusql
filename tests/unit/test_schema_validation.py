"""
Schema introspection, validation and parameter checking.

The SQLite tests exercise the real catalog path without a server. MySQL
and SQL Server read their catalogs through information_schema and sys
views, so those branches are checked here against fake connections that
return rows shaped like the drivers do; the live backends are covered by
tests/integration/test_schema_validation.py when reachable.
"""

import datetime
import decimal
import uuid

import pytest

from nexusql import (
    ColumnInfo,
    ColumnSpec,
    ConnectionConfig,
    DatabaseManager,
    DatabaseType,
    IndexInfo,
    IndexSpec,
    SchemaValidationError,
    TableInfo,
    TableNotFoundError,
    TableSpec,
    ValidationReport,
)
from nexusql.schema import (
    build_report,
    coerce_specs,
    kinds_satisfied_by,
    normalize_kind,
    pg_resolve_name,
    split_qualified_name,
    value_fits,
)


CANONICAL_DDL = """
    CREATE TABLE {t} (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL,
        active BOOLEAN DEFAULT TRUE,
        meta JSONB,
        ext_id UUID,
        created_at TIMESTAMP DEFAULT NOW(),
        score DECIMAL(10,2),
        big BIGINT,
        body TEXT
    )
"""


def expected_spec(table: str) -> TableSpec:
    """The spec a developer would write for CANONICAL_DDL."""
    return TableSpec(table, columns=[
        ColumnSpec("id", "SERIAL", primary_key=True, auto_increment=True),
        ColumnSpec("email", "text", nullable=False),
        ColumnSpec("active", "boolean"),
        ColumnSpec("meta", "json"),
        ColumnSpec("ext_id", "uuid"),
        ColumnSpec("created_at", "timestamp"),
        ColumnSpec("score", "decimal"),
        ColumnSpec("big", "bigint"),
        ColumnSpec("body", "text"),
    ], indexes=[
        IndexSpec(("email",), unique=True),
        IndexSpec(("created_at", "big")),
    ])


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    manager.execute(CANONICAL_DDL.format(t="people"))
    manager.execute("CREATE UNIQUE INDEX ux_people_email ON people(email)")
    manager.execute("CREATE INDEX ix_people_created ON people(created_at, big)")
    yield manager
    try:
        manager.disconnect()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SQLite: the real catalog path
# ---------------------------------------------------------------------------

class TestSqliteIntrospect:
    def test_list_tables(self, db):
        db.execute("CREATE TABLE other (x INTEGER)")
        assert db.list_tables() == ["other", "people"]

    def test_columns_kinds_and_flags(self, db):
        info = db.introspect("people")
        by_name = {c.name: c for c in info.columns}
        assert [c.name for c in info.columns] == [
            "id", "email", "active", "meta", "ext_id", "created_at", "score", "big", "body"
        ]
        # The translator turned SERIAL into INTEGER PRIMARY KEY AUTOINCREMENT.
        assert by_name["id"].kind == "integer"
        assert by_name["id"].raw_type == "INTEGER"
        assert by_name["id"].is_primary_key and by_name["id"].is_auto_increment
        assert not by_name["id"].nullable
        assert by_name["email"] == ColumnInfo(
            "email", "TEXT", "text", nullable=False, default=None)
        # BOOLEAN, JSONB, UUID and TIMESTAMP all land as the dialect's own types.
        assert by_name["active"].kind == "integer" and by_name["active"].default == "1"
        assert by_name["meta"].kind == "text"
        assert by_name["ext_id"].kind == "text"
        assert by_name["created_at"].kind == "text"
        assert by_name["created_at"].default == "CURRENT_TIMESTAMP"
        assert by_name["score"].kind == "decimal"
        assert by_name["big"].kind == "bigint"
        assert by_name["body"].nullable
        assert info.primary_key == ["id"]

    def test_indexes(self, db):
        info = db.introspect("people")
        assert IndexInfo("ux_people_email", ("email",), unique=True) in info.indexes
        assert IndexInfo("ix_people_created", ("created_at", "big")) in info.indexes
        # INTEGER PRIMARY KEY is the rowid alias and has no index of its own.
        assert not any(ix.primary for ix in info.indexes)

    def test_column_lookup_is_case_insensitive(self, db):
        info = db.introspect("people")
        assert info.column("EMAIL") is info.column("email")
        assert info.column("missing") is None

    def test_missing_table_raises(self, db):
        with pytest.raises(TableNotFoundError, match="nope"):
            db.introspect("nope")

    def test_text_primary_key_is_reported_not_null(self, db):
        """SQLite's pragma calls a TEXT PRIMARY KEY nullable; the canonical
        dialect never has a nullable key, so it is reported as NOT NULL."""
        db.execute("CREATE TABLE keyed (code TEXT PRIMARY KEY, n INTEGER)")
        info = db.introspect("keyed")
        code = info.column("code")
        assert code.is_primary_key and not code.nullable and not code.is_auto_increment
        # A non-INTEGER key gets an autoindex, reported as the primary index.
        assert any(ix.primary and ix.columns == ("code",) for ix in info.indexes)


class TestSqliteValidate:
    def test_matching_spec_is_ok(self, db):
        report = db.validate_schema([expected_spec("people")])
        assert report.ok and bool(report)
        assert report.errors == [] and report.warnings == []
        assert str(report) == "schema OK (1 table checked)"

    def test_dict_form_matches_too(self, db):
        report = db.validate_schema({
            "people": {
                "columns": {
                    "id": {"kind": "integer", "primary_key": True},
                    "email": {"kind": "text", "nullable": False},
                    "active": "boolean",
                },
                "indexes": [["email"], {"columns": ["created_at", "big"]}],
            }
        })
        assert report.ok
        # Columns the spec does not mention are warnings, not errors.
        assert ("people", "meta") in report.unexpected_columns
        assert any("meta: unexpected column" in w for w in report.warnings)

    def test_missing_table(self, db):
        report = db.validate_schema([TableSpec("ghosts", [ColumnSpec("id", "integer")])])
        assert not report.ok
        assert report.missing_tables == ["ghosts"]
        assert report.errors == ["ghosts: missing table"]

    def test_missing_column(self, db):
        spec = expected_spec("people")
        spec.columns.append(ColumnSpec("phone", "text"))
        report = db.validate_schema([spec])
        assert report.missing_columns == [("people", "phone")]
        assert report.errors == ["people.phone: missing column"]

    def test_wrong_kind(self, db):
        spec = expected_spec("people")
        spec.columns[1] = ColumnSpec("email", "integer", nullable=False)
        report = db.validate_schema([spec])
        assert report.errors == [
            "people.email: expected kind integer, found text (declared TEXT)"
        ]

    def test_boolean_on_sqlite_is_integer_but_bigint_is_not(self, db):
        spec = expected_spec("people")
        spec.columns[7] = ColumnSpec("big", "integer")
        report = db.validate_schema([spec])
        assert report.errors == [
            "people.big: expected kind integer, found bigint (declared BIGINT)"
        ]

    def test_nullability_both_directions(self, db):
        spec = expected_spec("people")
        spec.columns[1] = ColumnSpec("email", "text", nullable=True)
        spec.columns[8] = ColumnSpec("body", "text", nullable=False)
        report = db.validate_schema([spec])
        assert report.errors == [
            "people.email: expected nullable, found NOT NULL",
            "people.body: expected NOT NULL, found nullable",
        ]

    def test_primary_key_and_auto_increment_expectations(self, db):
        spec = expected_spec("people")
        spec.columns[1] = ColumnSpec("email", "text", primary_key=True)
        spec.columns[0] = ColumnSpec("id", "integer", primary_key=True, auto_increment=False)
        report = db.validate_schema([spec])
        assert "people.email: expected a primary key column" in report.errors
        assert ("people.id: expected no auto-increment column, found one"
                in report.errors)

    def test_missing_index(self, db):
        spec = expected_spec("people")
        spec.indexes.append(IndexSpec(("body",)))
        spec.indexes.append(IndexSpec(("created_at", "big"), unique=True))
        report = db.validate_schema([spec])
        assert report.missing_indexes == [
            ("people", ("body",)), ("people", ("created_at", "big")),
        ]
        assert report.errors == [
            "people: missing index on (body)",
            "people: missing unique index on (created_at, big)",
        ]

    def test_primary_key_satisfies_unique_index(self, db):
        spec = expected_spec("people")
        spec.indexes.append(IndexSpec(("id",), unique=True))
        assert db.validate_schema([spec]).ok

    def test_strict_turns_unexpected_columns_into_errors(self, db):
        spec = TableSpec("people", [ColumnSpec("id", "integer", primary_key=True)])
        assert db.validate_schema([spec]).ok
        report = db.validate_schema([spec], strict=True)
        assert not report.ok
        assert len(report.errors) == 8
        assert report.errors[0] == "people.email: unexpected column (TEXT)"

    def test_report_is_readable(self, db):
        spec = expected_spec("people")
        spec.columns.append(ColumnSpec("phone", "text"))
        text = str(db.validate_schema([spec, TableSpec("ghosts", [])]))
        assert text.splitlines() == [
            "schema validation failed with 2 errors (2 tables checked):",
            "  - people.phone: missing column",
            "  - ghosts: missing table",
        ]


class TestSqliteCheckParams:
    def test_fitting_params_pass(self, db):
        db.check_params("people", {
            "email": "a@b.c",
            "active": True,          # BOOLEAN is INTEGER on SQLite
            "meta": {"k": 1},        # JSONB is TEXT on SQLite
            "ext_id": uuid.uuid4(),  # UUID is TEXT on SQLite
            "created_at": datetime.datetime.now(),
            "score": decimal.Decimal("1.50"),
            "big": 10 ** 12,
            "body": None,
        })

    def test_offending_keys_are_listed(self, db):
        with pytest.raises(SchemaValidationError) as excinfo:
            db.check_params("people", {
                "email": None, "big": "12", "ghost": 1, "body": "fine",
            })
        err = excinfo.value
        assert err.table == "people"
        assert err.keys == ["email", "big", "ghost"]
        assert err.errors == [
            "email: None for NOT NULL column",
            "big: str value for bigint column (BIGINT)",
            "ghost: no such column",
        ]
        assert str(err).startswith("params do not fit table people: ")

    def test_cache_is_warm_after_first_call(self, db, monkeypatch):
        db.check_params("people", {"email": "x"})
        calls = []
        original = db._schema.introspect
        monkeypatch.setattr(db._schema, "introspect",
                            lambda table: calls.append(table) or original(table))
        db.check_params("people", {"email": "y"})
        assert calls == []

    def test_invalidate_refreshes(self, db):
        db.check_params("people", {"email": "x"})
        db.execute("ALTER TABLE people ADD COLUMN phone TEXT")
        with pytest.raises(SchemaValidationError, match="phone: no such column"):
            db.check_params("people", {"phone": "1"})
        db.invalidate_schema_cache("people")
        db.check_params("people", {"phone": "1"})

    def test_introspect_refreshes_the_cache(self, db):
        db.check_params("people", {"email": "x"})
        db.execute("ALTER TABLE people ADD COLUMN phone TEXT")
        db.introspect("people")
        db.check_params("people", {"phone": "1"})

    def test_missing_table_raises_not_found(self, db):
        with pytest.raises(TableNotFoundError):
            db.check_params("nope", {"a": 1})


# ---------------------------------------------------------------------------
# MySQL and SQL Server catalog branches against driver-shaped fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """Serves one scripted result set per execute(), in order."""

    def __init__(self, result_sets):
        self._sets = list(result_sets)
        self.executed = []
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        description, rows = self._sets.pop(0)
        self.description = description
        self._rows = rows

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

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self, timeout=None):
        return self._conn

    def release(self, conn, discard=False):
        pass

    def close(self):
        pass


def manager_with_fake(db_type: str, result_sets) -> tuple:
    """A manager whose single pooled connection replays ``result_sets``."""
    urls = {"mysql": "mysql://u:p@localhost/db", "mssql": "mssql://u:p@localhost/db"}
    manager = DatabaseManager(ConnectionConfig.from_url(urls[db_type]))
    cursor = FakeCursor(result_sets)
    manager._pool = FakePool(FakeConnection(cursor))
    return manager, cursor


def dict_rows(rows):
    """pymysql's DictCursor shape: no description needed."""
    return (None, rows)


def pyodbc_rows(columns, rows):
    """pyodbc shape: tuples plus a cursor.description naming the columns."""
    return ([(name, None) for name in columns], [tuple(r) for r in rows])


class TestMysqlBranch:
    COLUMN_ROWS = [
        {"name": "id", "raw_type": "int", "is_nullable": "NO",
         "column_default": None, "column_key": "PRI", "extra": "auto_increment"},
        {"name": "email", "raw_type": b"varchar(255)", "is_nullable": "NO",
         "column_default": None, "column_key": "UNI", "extra": ""},
        {"name": "active", "raw_type": "tinyint(1)", "is_nullable": "YES",
         "column_default": "1", "column_key": "", "extra": ""},
        {"name": "meta", "raw_type": "json", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "ext_id", "raw_type": "char(36)", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "created_at", "raw_type": "timestamp", "is_nullable": "YES",
         "column_default": "CURRENT_TIMESTAMP", "column_key": "", "extra": "DEFAULT_GENERATED"},
        {"name": "qty", "raw_type": "int unsigned", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "small", "raw_type": "tinyint", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "flag", "raw_type": "bit(1)", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "big", "raw_type": "bigint", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "body", "raw_type": "longtext", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
        {"name": "payload", "raw_type": "longblob", "is_nullable": "YES",
         "column_default": None, "column_key": "", "extra": ""},
    ]
    INDEX_ROWS = [
        {"name": "PRIMARY", "column_name": "id", "non_unique": 0, "seq_in_index": 1},
        {"name": "email", "column_name": "email", "non_unique": 0, "seq_in_index": 1},
        {"name": "ix_created", "column_name": "created_at", "non_unique": 1, "seq_in_index": 1},
        {"name": "ix_created", "column_name": "big", "non_unique": 1, "seq_in_index": 2},
    ]

    def test_catalog_queries_and_normalisation(self):
        manager, cursor = manager_with_fake("mysql", [
            dict_rows(self.COLUMN_ROWS), dict_rows(self.INDEX_ROWS),
        ])
        info = manager.introspect("people")

        columns_sql, columns_params = cursor.executed[0]
        assert "information_schema.columns" in columns_sql
        assert "table_schema = DATABASE()" in columns_sql
        assert columns_params == ("people",)
        indexes_sql, indexes_params = cursor.executed[1]
        assert "information_schema.statistics" in indexes_sql
        assert indexes_params == ("people",)

        kinds = {c.name: c.kind for c in info.columns}
        assert kinds == {
            "id": "integer", "email": "text", "active": "boolean", "meta": "json",
            "ext_id": "text", "created_at": "timestamp", "qty": "integer",
            "small": "integer", "flag": "boolean", "big": "bigint",
            "body": "text", "payload": "bytes",
        }
        id_col = info.column("id")
        assert id_col.is_primary_key and id_col.is_auto_increment and not id_col.nullable
        # bytes from the driver are decoded, not repr'd.
        assert info.column("email").raw_type == "varchar(255)"
        assert not info.column("email").is_primary_key
        assert info.column("created_at").default == "CURRENT_TIMESTAMP"

        assert info.indexes == [
            IndexInfo("PRIMARY", ("id",), unique=True, primary=True),
            IndexInfo("email", ("email",), unique=True),
            IndexInfo("ix_created", ("created_at", "big"), unique=False),
        ]

    def test_schema_qualified_name(self):
        manager, cursor = manager_with_fake("mysql", [
            dict_rows(self.COLUMN_ROWS[:1]), dict_rows([]),
        ])
        manager.introspect("shop.people")
        sql, params = cursor.executed[0]
        assert "table_schema = %s" in sql and params == ("shop", "people")

    def test_canonical_spec_validates_against_mysql_types(self):
        manager, _ = manager_with_fake("mysql", [
            dict_rows(self.COLUMN_ROWS), dict_rows(self.INDEX_ROWS),
        ])
        spec = TableSpec("people", columns=[
            ColumnSpec("id", "SERIAL", primary_key=True, auto_increment=True),
            ColumnSpec("email", "VARCHAR(255)", nullable=False),
            ColumnSpec("active", "BOOLEAN"),
            ColumnSpec("meta", "JSONB"),
            ColumnSpec("ext_id", "UUID"),      # CHAR(36) on MySQL
            ColumnSpec("created_at", "TIMESTAMP"),
        ], indexes=[IndexSpec(("email",), unique=True), IndexSpec(("created_at", "big"))])
        report = manager.validate_schema([spec])
        assert report.ok, report.errors

    def test_list_tables_uses_current_database(self):
        manager, cursor = manager_with_fake("mysql", [
            dict_rows([{"name": "a"}, {"name": "b"}]),
        ])
        assert manager.list_tables() == ["a", "b"]
        assert "table_schema = DATABASE()" in cursor.executed[0][0]

    def test_check_params_uses_mysql_equivalences(self):
        manager, _ = manager_with_fake("mysql", [
            dict_rows(self.COLUMN_ROWS), dict_rows(self.INDEX_ROWS),
        ])
        manager.check_params("people", {
            "ext_id": uuid.uuid4(), "active": True, "meta": {"a": 1}, "email": "e",
        })
        with pytest.raises(SchemaValidationError) as excinfo:
            manager.check_params("people", {"id": True, "meta": 3, "body": uuid.uuid4()})
        # body is longtext, not the CHAR(36) that UUID becomes, so a UUID is wrong there.
        assert excinfo.value.keys == ["id", "meta", "body"]


class TestMssqlBranch:
    COLUMN_NAMES = ["name", "data_type", "char_length", "numeric_precision",
                    "numeric_scale", "is_nullable", "column_default",
                    "is_identity", "is_primary_key"]
    COLUMN_ROWS = [
        ("id", "int", None, 10, 0, "NO", None, 1, 1),
        ("email", "nvarchar", 255, None, None, "NO", None, 0, 0),
        ("active", "bit", None, None, None, "YES", "((1))", 0, 0),
        ("meta", "nvarchar", -1, None, None, "YES", None, 0, 0),
        ("ext_id", "uniqueidentifier", None, None, None, "YES", None, 0, 0),
        ("created_at", "datetime2", None, None, None, "YES", "(getdate())", 0, 0),
        ("score", "decimal", None, 10, 2, "YES", None, 0, 0),
        ("big", "bigint", None, 19, 0, "YES", None, 0, 0),
        ("ratio", "float", None, 53, None, "YES", None, 0, 0),
        ("payload", "varbinary", -1, None, None, "YES", None, 0, 0),
        ("born", "date", None, None, None, "YES", None, 0, 0),
    ]
    INDEX_NAMES = ["name", "is_unique", "is_primary", "column_name", "key_ordinal"]
    INDEX_ROWS = [
        ("PK__people__3213E83F", True, True, "id", 1),
        ("ux_people_email", True, False, "email", 1),
        ("ix_people_created", False, False, "created_at", 1),
        ("ix_people_created", False, False, "big", 2),
    ]

    def test_catalog_queries_and_normalisation(self):
        manager, cursor = manager_with_fake("mssql", [
            pyodbc_rows(self.COLUMN_NAMES, self.COLUMN_ROWS),
            pyodbc_rows(self.INDEX_NAMES, self.INDEX_ROWS),
        ])
        info = manager.introspect("people")

        columns_sql, columns_params = cursor.executed[0]
        assert "information_schema.columns" in columns_sql
        assert "table_schema = SCHEMA_NAME()" in columns_sql
        assert "'IsIdentity'" in columns_sql
        assert "constraint_type = 'PRIMARY KEY'" in columns_sql
        assert columns_params == ("people",)
        indexes_sql, indexes_params = cursor.executed[1]
        assert "sys.indexes" in indexes_sql and "sys.index_columns" in indexes_sql
        assert indexes_params == ("people",)
        # The translator must not have rewritten the catalog query.
        assert "DATETIME2" not in columns_sql and "GETDATE" not in columns_sql

        kinds = {c.name: c.kind for c in info.columns}
        assert kinds == {
            "id": "integer", "email": "text", "active": "boolean", "meta": "text",
            "ext_id": "uuid", "created_at": "timestamp", "score": "decimal",
            "big": "bigint", "ratio": "float", "payload": "bytes", "born": "date",
        }
        raw = {c.name: c.raw_type for c in info.columns}
        assert raw["email"] == "nvarchar(255)"
        assert raw["meta"] == "nvarchar(max)"
        assert raw["score"] == "decimal(10,2)"
        id_col = info.column("id")
        assert id_col.is_primary_key and id_col.is_auto_increment and not id_col.nullable
        assert info.column("active").default == "((1))"

        assert info.indexes == [
            IndexInfo("PK__people__3213E83F", ("id",), unique=True, primary=True),
            IndexInfo("ux_people_email", ("email",), unique=True),
            IndexInfo("ix_people_created", ("created_at", "big")),
        ]

    def test_schema_qualified_name(self):
        manager, cursor = manager_with_fake("mssql", [
            pyodbc_rows(self.COLUMN_NAMES, self.COLUMN_ROWS[:1]),
            pyodbc_rows(self.INDEX_NAMES, []),
        ])
        manager.introspect("[sales].[people]")
        columns_sql, columns_params = cursor.executed[0]
        assert "table_schema = ?" in columns_sql and columns_params == ("sales", "people")
        assert cursor.executed[1][1] == ("sales.people",)

    def test_canonical_spec_validates_against_mssql_types(self):
        manager, _ = manager_with_fake("mssql", [
            pyodbc_rows(self.COLUMN_NAMES, self.COLUMN_ROWS),
            pyodbc_rows(self.INDEX_NAMES, self.INDEX_ROWS),
        ])
        spec = TableSpec("people", columns=[
            ColumnSpec("id", "SERIAL", primary_key=True, auto_increment=True),
            ColumnSpec("email", "VARCHAR(255)", nullable=False),
            ColumnSpec("active", "BOOLEAN"),       # BIT
            ColumnSpec("meta", "JSONB"),           # NVARCHAR(MAX)
            ColumnSpec("ext_id", "UUID"),          # UNIQUEIDENTIFIER
            ColumnSpec("created_at", "TIMESTAMP"), # DATETIME2
            ColumnSpec("score", "DECIMAL(10,2)"),
        ], indexes=[IndexSpec(("email",), unique=True), IndexSpec(("id",), unique=True)])
        report = manager.validate_schema([spec])
        assert report.ok, report.errors

    def test_json_check_param_accepted_for_nvarchar_max(self):
        manager, _ = manager_with_fake("mssql", [
            pyodbc_rows(self.COLUMN_NAMES, self.COLUMN_ROWS),
            pyodbc_rows(self.INDEX_NAMES, self.INDEX_ROWS),
        ])
        manager.check_params("people", {"meta": {"a": 1}, "active": True, "ext_id": "abc"})
        with pytest.raises(SchemaValidationError, match="email: dict value for text"):
            manager.check_params("people", {"email": {"a": 1}})

    def test_list_tables_uses_default_schema(self):
        manager, cursor = manager_with_fake("mssql", [
            pyodbc_rows(["name"], [("a",), ("b",)]),
        ])
        assert manager.list_tables() == ["a", "b"]
        assert "table_schema = SCHEMA_NAME()" in cursor.executed[0][0]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestNormalizeKind:
    @pytest.mark.parametrize("raw, kind", [
        ("integer", "integer"), ("INTEGER", "integer"), ("int4", "integer"),
        ("smallint", "integer"), ("serial", "integer"),
        ("bigint", "bigint"), ("int8", "bigint"), ("bigserial", "bigint"),
        ("double precision", "float"), ("real", "float"), ("float", "float"),
        ("numeric", "decimal"), ("DECIMAL(10,2)", "decimal"), ("money", "decimal"),
        ("text", "text"), ("character varying(255)", "text"), ("VARCHAR(40)", "text"),
        ("char(36)", "text"), ("nvarchar(max)", "text"), ("longtext", "text"),
        ("boolean", "boolean"), ("bool", "boolean"), ("bit", "boolean"),
        ("timestamp without time zone", "timestamp"), ("timestamptz", "timestamp"),
        ("datetime2", "timestamp"), ("datetime", "timestamp"),
        ("TIMESTAMP(6) WITH TIME ZONE", "timestamp"),
        ("date", "date"),
        ("json", "json"), ("jsonb", "json"),
        ("uuid", "uuid"), ("uniqueidentifier", "uuid"),
        ("bytea", "bytes"), ("BLOB", "bytes"), ("varbinary(max)", "bytes"),
        ("interval", "other"), ("time", "other"), ("_int4", "other"),
        ("", "other"), (None, "other"),
    ])
    def test_common_spellings(self, raw, kind):
        assert normalize_kind(raw, DatabaseType.POSTGRESQL) == kind

    def test_kind_names_pass_through(self):
        assert normalize_kind("json", DatabaseType.SQLITE) == "json"
        assert normalize_kind("BYTES", DatabaseType.MSSQL) == "bytes"

    def test_mysql_width_and_modifiers(self):
        assert normalize_kind("tinyint(1)", DatabaseType.MYSQL) == "boolean"
        assert normalize_kind("tinyint", DatabaseType.MYSQL) == "integer"
        assert normalize_kind("tinyint(4) unsigned", DatabaseType.MYSQL) == "integer"
        assert normalize_kind("bigint unsigned", DatabaseType.MYSQL) == "bigint"
        assert normalize_kind("bit(1)", DatabaseType.MYSQL) == "boolean"
        assert normalize_kind("bit(8)", DatabaseType.MYSQL) == "other"
        assert normalize_kind("enum('a','b')", DatabaseType.MYSQL) == "text"

    def test_equivalences_follow_the_translator(self):
        assert kinds_satisfied_by("integer", DatabaseType.SQLITE) == {"integer", "boolean"}
        assert kinds_satisfied_by("text", DatabaseType.SQLITE) == {
            "text", "uuid", "json", "timestamp"}
        # Only the shape the translator produces carries the equivalence.
        assert kinds_satisfied_by("text", DatabaseType.MYSQL, "char(36)") == {"text", "uuid"}
        assert kinds_satisfied_by("text", DatabaseType.MYSQL, "varchar(36)") == {"text"}
        assert kinds_satisfied_by("text", DatabaseType.MSSQL, "nvarchar(max)") == {"text", "json"}
        assert kinds_satisfied_by("text", DatabaseType.MSSQL, "NVARCHAR(MAX)") == {"text", "json"}
        assert kinds_satisfied_by("text", DatabaseType.MSSQL, "nvarchar(255)") == {"text"}
        assert kinds_satisfied_by("text", DatabaseType.POSTGRESQL, "text") == {"text"}
        assert kinds_satisfied_by("date", DatabaseType.SQLITE) == {"date"}


class TestSpecs:
    def test_column_spec_normalises_canonical_types(self):
        assert ColumnSpec("id", "SERIAL").kind == "integer"
        assert ColumnSpec("n", "VARCHAR(255)").kind == "text"
        assert ColumnSpec("n", "JSONB").kind == "json"
        assert ColumnSpec("n", "boolean").kind == "boolean"

    def test_index_spec_accepts_single_column_string(self):
        assert IndexSpec("email").columns == ("email",)
        assert IndexSpec(["a", "b"]).columns == ("a", "b")

    def test_coerce_specs_forms(self):
        spec = TableSpec("t", [ColumnSpec("a", "integer")])
        assert coerce_specs([spec]) == [spec]
        assert coerce_specs(spec) == [spec]
        from_list = coerce_specs([{"name": "t", "columns": [{"name": "a", "kind": "integer"}]}])
        from_map = coerce_specs({"t": {"columns": {"a": "integer"}}})
        assert from_list == from_map == [spec]

    def test_bad_dict_forms(self):
        with pytest.raises(ValueError, match="needs a name"):
            coerce_specs([{"columns": []}])
        with pytest.raises(ValueError, match="needs a name"):
            ColumnSpec.from_dict("integer")

    def test_build_report_counts_tables(self):
        specs = [TableSpec("a", [ColumnSpec("x", "integer")]), TableSpec("b", [])]
        info = TableInfo("a", [ColumnInfo("x", "int", "integer", nullable=True)])
        report = build_report(specs, {"a": info, "b": None}, DatabaseType.POSTGRESQL)
        assert isinstance(report, ValidationReport)
        assert report.tables_checked == 2
        assert report.errors == ["b: missing table"]


class TestValueFits:
    def column(self, kind, nullable=True):
        return ColumnInfo("c", kind, kind, nullable=nullable)

    def test_none_depends_on_nullability(self):
        assert value_fits(None, self.column("text"), DatabaseType.POSTGRESQL)
        assert not value_fits(None, self.column("text", nullable=False), DatabaseType.POSTGRESQL)

    def test_bool_is_not_an_integer_on_postgres(self):
        pg = DatabaseType.POSTGRESQL
        assert not value_fits(True, self.column("integer"), pg)
        assert value_fits(True, self.column("integer"), DatabaseType.SQLITE)
        assert value_fits(1, self.column("boolean"), pg)
        assert not value_fits(2, self.column("boolean"), pg)

    def test_kinds_accept_their_python_types(self):
        pg = DatabaseType.POSTGRESQL
        assert value_fits(3, self.column("float"), pg)
        assert value_fits(decimal.Decimal("1"), self.column("decimal"), pg)
        assert value_fits("2024-01-01", self.column("date"), pg)
        assert value_fits(datetime.date.today(), self.column("timestamp"), pg) is False
        assert value_fits(datetime.datetime.now(), self.column("date"), pg)
        assert value_fits(b"x", self.column("bytes"), pg)
        assert value_fits(memoryview(b"x"), self.column("bytes"), pg)
        assert value_fits([1, 2], self.column("json"), pg)
        assert value_fits(str(uuid.uuid4()), self.column("uuid"), pg)
        assert value_fits(object(), self.column("other"), pg)
        assert not value_fits(1.5, self.column("integer"), pg)


class TestNames:
    @pytest.mark.parametrize("name, parts", [
        ("users", (None, "users")),
        ("app.users", ("app", "users")),
        ('"My Users"', (None, "My Users")),
        ('"app"."Users"', ("app", "Users")),
        ("[dbo].[Users]", ("dbo", "Users")),
        ("`db`.`t`", ("db", "t")),
    ])
    def test_split_qualified_name(self, name, parts):
        assert split_qualified_name(name) == parts

    def test_pg_folds_unquoted_names(self):
        assert pg_resolve_name("Users") == (None, "users")
        assert pg_resolve_name("App.Users") == ("app", "users")
        assert pg_resolve_name('"Users"') == (None, "Users")

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            split_qualified_name("")
