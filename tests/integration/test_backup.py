"""
Portable export/import and native backup against live backends.

The JSON-lines round trip runs on every reachable backend with the column
types that need special handling in JSON (bytes, timestamps, decimals,
booleans). The pg_dump path runs only where the binary is installed; the
restore side is never exercised against the shared PostgreSQL database,
since pg_restore acts on the whole database. Backends that are not
reachable are skipped at collection (see conftest).
"""

import datetime as dt
import decimal
import shutil
import uuid

import pytest

from nexusql import BackupToolNotFound, DatabaseManager
from nexusql.backup import discover_tables

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]

# The canonical dialect has no portable binary type the translator rewrites,
# so the bytes column is declared per backend, as tests/integration/
# test_binary_data.py does.
BYTES_TYPE = {
    "sqlite": "BLOB",
    "postgresql": "BYTEA",
    "mysql": "LONGBLOB",
    "mssql": "VARBINARY(MAX)",
}


def table_name() -> str:
    return "bk_" + uuid.uuid4().hex[:10]


def backend(db_manager) -> str:
    return db_manager.config.database_type.value


def sample_rows(dialect: str):
    created = dt.datetime(2024, 5, 6, 7, 8, 9)
    # sqlite3 has no Decimal adapter and stores TIMESTAMP as text.
    if dialect == "sqlite":
        amounts = [12.5, 0.0, 1000000.25]
        created = created.isoformat(" ")
    else:
        amounts = [decimal.Decimal("12.50"), decimal.Decimal("0.00"), decimal.Decimal("1000000.25")]
    return [
        {"name": "alpha", "payload": bytes(range(256)), "created": created, "amount": amounts[0], "active": True},
        {"name": "beta é中", "payload": None, "created": None, "amount": amounts[1], "active": False},
        {"name": "gamma", "payload": b"\x00" * 1024, "created": created, "amount": amounts[2], "active": True},
    ]


@pytest.fixture
def table(db_manager):
    name = table_name()
    db_manager.execute(
        f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL, "
        f"payload {BYTES_TYPE[backend(db_manager)]}, created TIMESTAMP, "
        "amount NUMERIC(12,2), active BOOLEAN)"
    )
    for row in sample_rows(backend(db_manager)):
        db_manager.execute(
            f"INSERT INTO {name} (name, payload, created, amount, active) "
            "VALUES (:name, :payload, :created, :amount, :active)",
            row,
        )
    yield name
    try:
        db_manager.execute(f"DROP TABLE {name}")
    except Exception:
        pass


def rows_of(db_manager, table):
    return db_manager.execute(f"SELECT * FROM {table} ORDER BY id")


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestExportImport:
    def test_round_trip_restores_every_value(self, db_manager, table, tmp_path):
        before = rows_of(db_manager, table)
        assert len(before) == 3

        manifest = db_manager.export_tables(str(tmp_path / "export"), tables=[table])
        assert manifest["dialect"] == backend(db_manager)
        entry, = manifest["tables"]
        assert entry["name"] == table
        assert [c.lower() for c in entry["columns"]] == [
            "id", "name", "payload", "created", "amount", "active"
        ]
        assert entry["row_count"] == 3

        db_manager.execute(f"DELETE FROM {table}")
        assert db_manager.import_tables(str(tmp_path / "export")) == {table: 3}
        assert rows_of(db_manager, table) == before

    def test_truncate_then_a_plain_insert_gets_a_fresh_key(self, db_manager, table, tmp_path):
        before = rows_of(db_manager, table)
        db_manager.export_tables(str(tmp_path / "export"), tables=[table])

        assert db_manager.import_tables(str(tmp_path / "export"), truncate=True) == {table: 3}
        assert rows_of(db_manager, table) == before

        # Imported rows carried explicit keys; the generator must have
        # moved past them (PostgreSQL sequences do not do that on their own,
        # and SQL Server must have IDENTITY_INSERT switched back off).
        new_id = db_manager.execute(
            f"INSERT INTO {table} (name) VALUES (:name)", {"name": "after"}, return_id=True
        )
        assert new_id > max(r["id"] for r in before)

    def test_discovery_lists_the_table(self, db_manager, table):
        assert table in discover_tables(db_manager)

    def test_import_appends_without_truncate(self, db_manager, table, tmp_path):
        db_manager.export_tables(str(tmp_path / "export"), tables=[table])
        db_manager.execute(f"DELETE FROM {table}")
        db_manager.execute(
            f"INSERT INTO {table} (name) VALUES (:name)", {"name": "kept"}
        )
        kept = rows_of(db_manager, table)
        assert kept[0]["id"] > 3, "the key generator kept counting past the deleted rows"

        db_manager.import_tables(str(tmp_path / "export"))
        names = [r["name"] for r in rows_of(db_manager, table)]
        assert names[:3] == ["alpha", "beta é中", "gamma"] and names[3] == "kept"


# ---------------------------------------------------------------------------
# pg_dump against the live server, when the binary exists
# ---------------------------------------------------------------------------

def test_pg_dump_of_one_table(postgresql_url, skip_if_no_postgresql, tmp_path):
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump not on PATH")
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    name = table_name()
    try:
        db.execute(f"CREATE TABLE {name} (id SERIAL PRIMARY KEY, note TEXT)")
        db.execute(f"INSERT INTO {name} (note) VALUES (:n)", {"n": "dumped"})
        target = tmp_path / f"{name}.dump"
        assert db.backup(str(target), tables=[name]) == str(target)
        # Custom-format archives open with this magic.
        assert target.read_bytes()[:5] == b"PGDMP"
    finally:
        db.execute(f"DROP TABLE IF EXISTS {name}")
        db.disconnect()


def test_pg_dump_absence_is_reported_by_name(postgresql_url, skip_if_no_postgresql, tmp_path):
    if shutil.which("pg_dump") is not None:
        pytest.skip("pg_dump is installed")
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    try:
        with pytest.raises(BackupToolNotFound, match="pg_dump"):
            db.backup(str(tmp_path / "x.dump"))
    finally:
        db.disconnect()
