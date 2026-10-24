"""
Migration rollback against a live PostgreSQL.

The database is shared with other test runs, so every runner here records
into a migrations table of its own and the migrations create uniquely named
tables; everything is dropped again in the fixture's teardown. PostgreSQL is
the one backend where the down script and the row removal are a single
transaction, which test_failed_down_script_is_fully_rolled_back relies on.
"""

import logging
import uuid

import pytest

from nexusql import DatabaseManager, MigrationRollbackError, MigrationRunner

BACKENDS = ["postgresql"]


@pytest.fixture
def suffix():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def migrations_table(db_manager, suffix):
    name = f"mig_{suffix}"
    yield name
    try:
        db_manager.execute(f"DROP TABLE IF EXISTS {name}")
    except Exception:
        pass


@pytest.fixture
def tables(db_manager, suffix):
    """Three table names the migrations will create, dropped on teardown."""
    names = [f"rb_{kind}_{suffix}" for kind in ("users", "posts", "tags")]
    yield names
    for name in names:
        try:
            db_manager.execute(f"DROP TABLE IF EXISTS {name}")
        except Exception:
            pass


def write_migrations(path, tables, down="file", skip_down_for=()):
    """Three migrations creating ``tables``; ``down`` is 'file' or 'marker'."""
    versions = []
    for index, table in enumerate(tables, start=1):
        version = f"{index:03d}_{table}"
        versions.append(version)
        up = f"CREATE TABLE {table} (id SERIAL PRIMARY KEY, name VARCHAR(50));"
        down_sql = f"DROP TABLE {table};"
        if version in skip_down_for:
            (path / f"{version}.sql").write_text(up)
        elif down == "marker":
            (path / f"{version}.sql").write_text(f"{up}\n-- down\n{down_sql}\n")
        else:
            (path / f"{version}.sql").write_text(up)
            (path / f"{version}.down.sql").write_text(down_sql)
    return versions


def recorded(db, table):
    return [r["version"] for r in db.execute(f"SELECT version FROM {table} ORDER BY version")]


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestRollbackPostgres:
    @pytest.mark.asyncio
    async def test_apply_three_roll_back_one(self, db_manager, migrations_table, tables, tmp_path):
        versions = write_migrations(tmp_path, tables)
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()
        assert recorded(db_manager, migrations_table) == versions
        assert all(db_manager.table_exists(t) for t in tables)

        assert await runner.rollback() == [versions[2]]

        assert recorded(db_manager, migrations_table) == versions[:2]
        assert not db_manager.table_exists(tables[2])
        assert db_manager.table_exists(tables[1])

    @pytest.mark.asyncio
    async def test_roll_back_to_version(self, db_manager, migrations_table, tables, tmp_path):
        versions = write_migrations(tmp_path, tables)
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()

        assert await runner.rollback_to(versions[0]) == [versions[2], versions[1]]

        assert recorded(db_manager, migrations_table) == versions[:1]
        assert db_manager.table_exists(tables[0])
        assert not db_manager.table_exists(tables[1])
        assert not db_manager.table_exists(tables[2])

    @pytest.mark.asyncio
    async def test_marker_form(self, db_manager, migrations_table, tables, tmp_path):
        versions = write_migrations(tmp_path, tables, down="marker")
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()
        # The down section did not run on the way up
        assert all(db_manager.table_exists(t) for t in tables)

        plan = await runner.plan_rollback(steps=3)
        assert [s.version for s in plan] == list(reversed(versions))
        assert all(s.down_source == "marker" for s in plan)

        assert await runner.rollback(steps=3) == list(reversed(versions))
        assert recorded(db_manager, migrations_table) == []
        assert not any(db_manager.table_exists(t) for t in tables)

    @pytest.mark.asyncio
    async def test_missing_down_script_refused_with_nothing_changed(
        self, db_manager, migrations_table, tables, tmp_path
    ):
        versions = write_migrations(tmp_path, tables, skip_down_for=[f"002_{tables[1]}"])
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()

        with pytest.raises(MigrationRollbackError, match=versions[1]):
            await runner.rollback(steps=2)

        assert recorded(db_manager, migrations_table) == versions
        assert all(db_manager.table_exists(t) for t in tables)

    @pytest.mark.asyncio
    async def test_failed_down_script_is_fully_rolled_back(
        self, db_manager, migrations_table, tables, tmp_path
    ):
        """DDL is transactional on PostgreSQL: a down script that fails on
        its second statement leaves neither the first statement's effect nor
        a missing migrations row behind."""
        versions = write_migrations(tmp_path, tables)
        (tmp_path / f"{versions[2]}.down.sql").write_text(
            f"DROP TABLE {tables[2]};\nDROP TABLE {tables[2]}_missing;\n"
        )
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()

        with pytest.raises(MigrationRollbackError, match=versions[2]):
            await runner.rollback()

        assert recorded(db_manager, migrations_table) == versions
        assert db_manager.table_exists(tables[2])
        # The connection is usable afterwards
        assert db_manager.execute("SELECT 1 AS one") == [{"one": 1}]

    @pytest.mark.asyncio
    async def test_plan_rollback_describes_only(self, db_manager, migrations_table, tables, tmp_path):
        versions = write_migrations(tmp_path, tables)
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()

        plan = await runner.plan_rollback(to=versions[0])

        assert [s.version for s in plan] == [versions[2], versions[1]]
        assert [s.down_source for s in plan] == ["file", "file"]
        assert [s.down_path.name for s in plan] == [
            f"{versions[2]}.down.sql", f"{versions[1]}.down.sql"
        ]
        assert not any(s.down_changed for s in plan)
        assert recorded(db_manager, migrations_table) == versions
        assert all(db_manager.table_exists(t) for t in tables)

    @pytest.mark.asyncio
    async def test_down_checksum_recorded_and_change_warns(
        self, db_manager, migrations_table, tables, tmp_path, caplog
    ):
        versions = write_migrations(tmp_path, tables)
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()

        rows = db_manager.execute(
            f"SELECT version, checksum, down_checksum FROM {migrations_table} ORDER BY version"
        )
        assert all(r["checksum"] and r["down_checksum"] for r in rows)

        (tmp_path / f"{versions[2]}.down.sql").write_text(f"DROP TABLE IF EXISTS {tables[2]};")
        with caplog.at_level(logging.WARNING, logger="nexusql.migrations"):
            plan = await runner.plan_rollback()
            assert plan[0].down_changed
            assert "changed since it was applied" in caplog.text
            assert await runner.rollback() == [versions[2]]
        assert not db_manager.table_exists(tables[2])

    @pytest.mark.asyncio
    async def test_existing_table_gains_down_checksum_column(
        self, db_manager, migrations_table, tables, tmp_path
    ):
        """A migrations table created before down scripts were tracked is
        upgraded in place; its NULL rows roll back without a warning."""
        db_manager.execute(
            f"CREATE TABLE {migrations_table} (id SERIAL PRIMARY KEY, version VARCHAR(255) NOT NULL, "
            f"filename VARCHAR(500) NOT NULL, migration_type VARCHAR(50) NOT NULL, description TEXT, "
            f"applied_at TIMESTAMP DEFAULT NOW(), checksum VARCHAR(64), UNIQUE (version, migration_type))"
        )
        versions = write_migrations(tmp_path, tables[:1])
        runner = MigrationRunner(db_manager, tmp_path, migrations_table=migrations_table)
        assert await runner.run_pending_migrations()

        row = db_manager.fetch_one(
            f"SELECT down_checksum FROM {migrations_table} WHERE version = :v", {"v": versions[0]}
        )
        assert row["down_checksum"]
        assert await runner.rollback() == versions
        assert not db_manager.table_exists(tables[0])
