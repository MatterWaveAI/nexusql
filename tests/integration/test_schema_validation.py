"""
Schema introspection and validation against live backends.

One canonical CREATE TABLE is run through the translator on every backend,
and the same TableSpec must validate on each: the kinds are compared
through the translator's own equivalences, so BOOLEAN-as-INTEGER on SQLite
or JSONB-as-NVARCHAR(MAX) on SQL Server is a match, not a mismatch.
Backends that are not reachable are skipped at collection (see conftest).
"""

import datetime
import os
import uuid

import pytest

from nexusql import (
    ColumnSpec,
    DatabaseType,
    IndexSpec,
    SchemaValidationError,
    TableNotFoundError,
    TableSpec,
)

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]


def table_name() -> str:
    return "sch_" + uuid.uuid4().hex[:10]


CANONICAL_DDL = """
    CREATE TABLE {t} (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL,
        active BOOLEAN DEFAULT TRUE,
        meta JSONB,
        ext_id UUID,
        created_at TIMESTAMP,
        score DECIMAL(10,2),
        big BIGINT,
        body TEXT
    )
"""


def expected_spec(table: str) -> TableSpec:
    return TableSpec(table, columns=[
        ColumnSpec("id", "SERIAL", primary_key=True, auto_increment=True),
        ColumnSpec("email", "VARCHAR(255)", nullable=False),
        ColumnSpec("active", "BOOLEAN"),
        ColumnSpec("meta", "JSONB"),
        ColumnSpec("ext_id", "UUID"),
        ColumnSpec("created_at", "TIMESTAMP"),
        ColumnSpec("score", "DECIMAL(10,2)"),
        ColumnSpec("big", "BIGINT"),
        ColumnSpec("body", "TEXT"),
    ], indexes=[
        IndexSpec(("email",), unique=True),
        IndexSpec(("created_at", "big")),
        IndexSpec(("id",), unique=True),
    ])


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(CANONICAL_DDL.format(t=name))
    db_manager.execute(f"CREATE UNIQUE INDEX ux_{name}_email ON {name}(email)")
    db_manager.execute(f"CREATE INDEX ix_{name}_created ON {name}(created_at, big)")
    yield name
    try:
        db_manager.execute(f"DROP TABLE {name}")
    except Exception:
        pass


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestIntrospect:
    def test_list_tables_contains_ours(self, db_manager, table):
        # The server databases are shared with other test runs, so only
        # containment can be asserted.
        tables = db_manager.list_tables()
        assert table in tables
        assert all(isinstance(t, str) for t in tables)

    def test_columns(self, db_manager, table):
        info = db_manager.introspect(table)
        assert [c.name for c in info.columns] == [
            "id", "email", "active", "meta", "ext_id", "created_at", "score", "big", "body"
        ]
        col = {c.name: c for c in info.columns}
        db_type = db_manager.config.database_type

        assert col["id"].kind == "integer"
        assert col["id"].is_primary_key
        assert col["id"].is_auto_increment
        assert not col["id"].nullable
        assert info.primary_key == ["id"]

        assert col["email"].kind == "text" and not col["email"].nullable
        assert col["body"].kind == "text" and col["body"].nullable
        assert col["big"].kind == "bigint"
        assert col["score"].kind == "decimal"

        # Each dialect reports what the translator gave it.
        expected = {
            DatabaseType.SQLITE: {"active": "integer", "meta": "text", "ext_id": "text",
                                  "created_at": "text"},
            DatabaseType.POSTGRESQL: {"active": "boolean", "meta": "json", "ext_id": "uuid",
                                      "created_at": "timestamp"},
            DatabaseType.MYSQL: {"active": "boolean", "meta": "json", "ext_id": "text",
                                 "created_at": "timestamp"},
            DatabaseType.MSSQL: {"active": "boolean", "meta": "text", "ext_id": "uuid",
                                 "created_at": "timestamp"},
        }[db_type]
        assert {name: col[name].kind for name in expected} == expected
        assert all(c.raw_type for c in info.columns)

    def test_indexes(self, db_manager, table):
        info = db_manager.introspect(table)
        by_columns = {ix.columns: ix for ix in info.indexes}
        assert by_columns[("email",)].unique
        assert not by_columns[("created_at", "big")].unique
        assert not by_columns[("created_at", "big")].primary

    def test_missing_table_raises(self, db_manager):
        with pytest.raises(TableNotFoundError):
            db_manager.introspect(table_name())


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestValidate:
    def test_matching_spec_is_ok_on_every_backend(self, db_manager, table):
        report = db_manager.validate_schema([expected_spec(table)])
        assert report.ok, str(report)
        assert str(report) == "schema OK (1 table checked)"

    def test_dict_form(self, db_manager, table):
        report = db_manager.validate_schema({
            table: {"columns": {"id": {"kind": "SERIAL", "primary_key": True},
                                "email": {"kind": "text", "nullable": False}},
                    "indexes": [["email"]]},
        })
        assert report.ok, str(report)
        assert report.warnings  # the unlisted columns

    def test_differences_are_reported(self, db_manager, table):
        missing = table_name()
        spec = expected_spec(table)
        spec.columns[1] = ColumnSpec("email", "integer")          # wrong kind, nullability
        spec.columns[8] = ColumnSpec("body", "TEXT", nullable=False)
        spec.columns.append(ColumnSpec("phone", "text"))         # missing column
        spec.indexes.append(IndexSpec(("body",)))                # missing index
        report = db_manager.validate_schema(
            [spec, TableSpec(missing, [ColumnSpec("id", "integer")])])

        assert not report.ok
        assert report.missing_tables == [missing]
        assert report.missing_columns == [(table, "phone")]
        assert report.missing_indexes == [(table, ("body",))]
        assert any(e.startswith(f"{table}.email: expected kind integer, found text")
                   for e in report.errors)
        assert f"{table}.email: expected nullable, found NOT NULL" in report.errors
        assert f"{table}.body: expected NOT NULL, found nullable" in report.errors
        assert f"{table}: missing index on (body)" in report.errors
        assert f"{missing}: missing table" in report.errors
        text = str(report)
        assert text.startswith("schema validation failed with 6 errors (2 tables checked):")
        assert text.count("\n  - ") == 6

    def test_strict_flags_unexpected_columns(self, db_manager, table):
        spec = TableSpec(table, [ColumnSpec("id", "integer", primary_key=True)])
        assert db_manager.validate_schema([spec]).ok
        report = db_manager.validate_schema([spec], strict=True)
        assert len(report.errors) == 8
        assert all("unexpected column" in e for e in report.errors)


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestCheckParams:
    def test_fitting_params(self, db_manager, table):
        params = {
            "email": "a@b.c",
            "active": True,
            "meta": '{"k": 1}',
            "ext_id": str(uuid.uuid4()),
            "created_at": datetime.datetime(2024, 1, 2, 3, 4, 5),
            "score": 1.5,
            "big": 10 ** 12,
            "body": None,
        }
        db_manager.check_params(table, params)
        # The same values really do insert, so the check is not stricter
        # than the database.
        columns = ", ".join(params)
        placeholders = ", ".join(f":{k}" for k in params)
        db_manager.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", params)
        assert db_manager.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 1

    def test_offenders_are_named(self, db_manager, table):
        with pytest.raises(SchemaValidationError) as excinfo:
            db_manager.check_params(table, {"email": None, "big": "x", "ghost": 1})
        assert excinfo.value.keys == ["email", "big", "ghost"]

    def test_cache_and_invalidation(self, db_manager, table):
        db_manager.check_params(table, {"email": "x"})
        db_manager.execute(f"ALTER TABLE {table} ADD phone VARCHAR(20)")
        with pytest.raises(SchemaValidationError, match="phone: no such column"):
            db_manager.check_params(table, {"phone": "1"})
        db_manager.invalidate_schema_cache()
        db_manager.check_params(table, {"phone": "1"})


# ---------------------------------------------------------------------------
# PostgreSQL specifics
# ---------------------------------------------------------------------------

def test_postgres_identity_and_quoted_names(postgresql_url, skip_if_no_postgresql):
    from nexusql import DatabaseManager

    db = DatabaseManager(postgresql_url)
    assert db.connect()
    name = table_name()
    try:
        db.execute(
            f'CREATE TABLE "{name}" (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, '
            f'"Mixed" INTEGER)'
        )
        info = db.introspect(name)
        assert info.column("id").kind == "bigint"
        assert info.column("id").is_auto_increment
        assert info.column("Mixed").raw_type == "integer"
        # Unquoted lookups fold; quoted ones keep their case.
        assert db.introspect(f"public.{name}").columns == info.columns
        assert db.introspect(f'"{name}"').columns == info.columns
    finally:
        db.execute(f'DROP TABLE IF EXISTS "{name}"')
        db.disconnect()


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


async def test_async_backend_introspect_and_validate(async_backend):
    name = table_name()
    try:
        await async_backend.execute(CANONICAL_DDL.format(t=name))
        await async_backend.execute(f"CREATE UNIQUE INDEX ux_{name}_email ON {name}(email)")
        await async_backend.execute(f"CREATE INDEX ix_{name}_created ON {name}(created_at, big)")

        assert name in await async_backend.list_tables()

        info = await async_backend.introspect(name)
        assert info.column("id").is_auto_increment and info.column("id").is_primary_key
        assert info.column("meta").kind == "json"
        assert any(ix.columns == ("email",) and ix.unique for ix in info.indexes)

        report = await async_backend.validate_schema([expected_spec(name)])
        assert report.ok, str(report)

        spec = expected_spec(name)
        spec.columns.append(ColumnSpec("phone", "text"))
        report = await async_backend.validate_schema([spec, TableSpec("zz_" + name, [])])
        assert report.errors == [f"{name}.phone: missing column", f"zz_{name}: missing table"]

        with pytest.raises(TableNotFoundError):
            await async_backend.introspect("zz_" + name)
    finally:
        await async_backend.execute(f"DROP TABLE IF EXISTS {name}")
