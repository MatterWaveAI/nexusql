"""
MigrationRunner.rollback / rollback_to / plan_rollback on SQLite.

Each test owns a migrations directory under tmp_path and a fresh database
file, so the shape of the down scripts (sibling .down.sql, -- down section,
none at all) is whatever the test writes. The live-server behaviour is
covered by tests/integration/test_migration_rollback.py.
"""

import logging
from pathlib import Path

import pytest

from nexusql import (
    DatabaseManager,
    MigrationChecksumMismatch,
    MigrationRollbackError,
    MigrationRunner,
    RollbackStep,
)
from nexusql.migrations import compute_checksum, load_migration, split_down_marker


UP = {
    "001_users": "CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT);",
    "002_posts": "CREATE TABLE posts (id SERIAL PRIMARY KEY, title TEXT);",
    "003_tags": "CREATE TABLE tags (id SERIAL PRIMARY KEY, label TEXT);",
}
DOWN = {
    "001_users": "DROP TABLE users;",
    "002_posts": "DROP TABLE posts;",
    "003_tags": "DROP TABLE tags;",
}


def write_sibling_set(migrations: Path, versions=("001_users", "002_posts", "003_tags")):
    for version in versions:
        (migrations / f"{version}.sql").write_text(UP[version])
        (migrations / f"{version}.down.sql").write_text(DOWN[version])


def write_marker_set(migrations: Path, versions=("001_users", "002_posts", "003_tags")):
    for version in versions:
        (migrations / f"{version}.sql").write_text(
            f"{UP[version]}\n-- down\n{DOWN[version]}\n"
        )


@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect(), "SQLite connect failed"
    yield manager
    try:
        manager.disconnect()
    except Exception:
        pass


@pytest.fixture
def migrations(tmp_path):
    path = tmp_path / "migrations"
    path.mkdir()
    return path


def applied_versions(db, table="ia_migrations"):
    return [r["version"] for r in db.execute(f"SELECT version FROM {table} ORDER BY version")]


async def apply_all(db, migrations):
    runner = MigrationRunner(db, migrations)
    assert await runner.run_pending_migrations()
    return runner


# ---------------------------------------------------------------------------
# Applying with down scripts present
# ---------------------------------------------------------------------------

class TestApply:
    @pytest.mark.asyncio
    async def test_down_files_are_not_applied_as_migrations(self, db, migrations):
        write_sibling_set(migrations)
        await apply_all(db, migrations)
        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]
        for table in ("users", "posts", "tags"):
            assert db.table_exists(table)

    @pytest.mark.asyncio
    async def test_marker_section_does_not_run_on_the_way_up(self, db, migrations):
        write_marker_set(migrations)
        await apply_all(db, migrations)
        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]
        # If the down section had run, the tables would be gone again.
        for table in ("users", "posts", "tags"):
            assert db.table_exists(table)

    @pytest.mark.asyncio
    async def test_records_up_and_down_checksums(self, db, migrations):
        write_sibling_set(migrations, ["001_users"])
        (migrations / "002_posts.sql").write_text(UP["002_posts"])
        await apply_all(db, migrations)
        rows = {
            r["version"]: r
            for r in db.execute("SELECT version, checksum, down_checksum FROM ia_migrations")
        }
        source = load_migration(migrations / "001_users.sql")
        assert rows["001_users"]["checksum"] == source.checksum
        assert rows["001_users"]["down_checksum"] == source.down_checksum
        assert rows["001_users"]["down_checksum"] is not None
        assert rows["002_posts"]["down_checksum"] is None

    @pytest.mark.asyncio
    async def test_listings(self, db, migrations):
        write_sibling_set(migrations)
        runner = MigrationRunner(db, migrations)
        assert await runner.applied() == []
        assert [p.name for p in await runner.pending()] == [
            "001_users.sql", "002_posts.sql", "003_tags.sql"
        ]
        assert await runner.run_pending_migrations()
        assert [m.version for m in await runner.applied()] == [
            "001_users", "002_posts", "003_tags"
        ]
        assert await runner.pending() == []


# ---------------------------------------------------------------------------
# Rolling back
# ---------------------------------------------------------------------------

class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_one_undoes_the_newest(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)

        assert await runner.rollback() == ["003_tags"]

        assert applied_versions(db) == ["001_users", "002_posts"]
        assert not db.table_exists("tags")
        assert db.table_exists("posts")

    @pytest.mark.asyncio
    async def test_rollback_steps_runs_newest_first(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)

        assert await runner.rollback(steps=2) == ["003_tags", "002_posts"]

        assert applied_versions(db) == ["001_users"]
        assert db.table_exists("users")
        assert not db.table_exists("posts")

    @pytest.mark.asyncio
    async def test_rollback_to_keeps_the_target_applied(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)

        assert await runner.rollback_to("001_users") == ["003_tags", "002_posts"]

        assert applied_versions(db) == ["001_users"]
        assert db.table_exists("users")

    @pytest.mark.asyncio
    async def test_rollback_to_accepts_the_number_alone(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        assert await runner.rollback_to("002") == ["003_tags"]
        assert applied_versions(db) == ["001_users", "002_posts"]

    @pytest.mark.asyncio
    async def test_rollback_to_unknown_version_changes_nothing(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        with pytest.raises(MigrationRollbackError, match="not an applied"):
            await runner.rollback_to("009_nothing")
        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]

    @pytest.mark.asyncio
    async def test_rollback_to_newest_is_a_no_op(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        assert await runner.rollback_to("003_tags") == []
        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]

    @pytest.mark.asyncio
    async def test_rolled_back_migration_is_pending_again(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        await runner.rollback()
        assert [p.name for p in await runner.pending()] == ["003_tags.sql"]
        assert await runner.run_pending_migrations()
        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]

    @pytest.mark.asyncio
    async def test_marker_form_rolls_back(self, db, migrations):
        write_marker_set(migrations)
        runner = await apply_all(db, migrations)

        assert await runner.rollback(steps=3) == ["003_tags", "002_posts", "001_users"]

        assert applied_versions(db) == []
        for table in ("users", "posts", "tags"):
            assert not db.table_exists(table)

    @pytest.mark.asyncio
    async def test_marker_is_case_insensitive_and_tolerates_whitespace(self, db, migrations):
        (migrations / "001_users.sql").write_text(
            f"{UP['001_users']}\n  -- DOWN  \n{DOWN['001_users']}\n"
        )
        runner = await apply_all(db, migrations)
        assert db.table_exists("users")
        assert await runner.rollback() == ["001_users"]
        assert not db.table_exists("users")

    @pytest.mark.asyncio
    async def test_multi_statement_down_script(self, db, migrations):
        (migrations / "001_two.sql").write_text(
            "CREATE TABLE a (id SERIAL PRIMARY KEY);\n"
            "CREATE TABLE b (id SERIAL PRIMARY KEY);\n"
            "-- down\n"
            "DROP TABLE b;\n"
            "DROP TABLE a;\n"
        )
        runner = await apply_all(db, migrations)
        assert db.table_exists("a") and db.table_exists("b")
        await runner.rollback()
        assert not db.table_exists("a") and not db.table_exists("b")

    @pytest.mark.asyncio
    async def test_rollback_with_other_migration_type_untouched(self, db, migrations):
        """Rollback is scoped to the runner's migration_type."""
        write_sibling_set(migrations)
        other_dir = migrations.parent / "system"
        other_dir.mkdir()
        (other_dir / "001_sys.sql").write_text("CREATE TABLE sys (id SERIAL PRIMARY KEY);")
        system = MigrationRunner(db, other_dir, migration_type="system")
        assert await system.run_pending_migrations()
        app = await apply_all(db, migrations)

        await app.rollback(steps=3)

        assert applied_versions(db) == ["001_sys"]
        assert db.table_exists("sys")

    @pytest.mark.asyncio
    async def test_custom_migrations_table(self, db, migrations):
        write_sibling_set(migrations)
        runner = MigrationRunner(db, migrations, migrations_table="my_migrations")
        assert await runner.run_pending_migrations()
        assert not db.table_exists("ia_migrations")
        assert applied_versions(db, "my_migrations") == ["001_users", "002_posts", "003_tags"]
        await runner.rollback()
        assert applied_versions(db, "my_migrations") == ["001_users", "002_posts"]


# ---------------------------------------------------------------------------
# Refusals: nothing changes
# ---------------------------------------------------------------------------

class TestRefusals:
    @pytest.mark.asyncio
    async def test_missing_down_script_refused_before_anything_runs(self, db, migrations):
        write_sibling_set(migrations, ["001_users", "003_tags"])
        (migrations / "002_posts.sql").write_text(UP["002_posts"])
        runner = await apply_all(db, migrations)

        # 003 has a down script but 002 does not; a two-step rollback must
        # not undo 003 and then stop.
        with pytest.raises(MigrationRollbackError, match="002_posts"):
            await runner.rollback(steps=2)

        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]
        assert db.table_exists("tags")

    @pytest.mark.asyncio
    async def test_single_step_without_down_refused(self, db, migrations):
        (migrations / "001_users.sql").write_text(UP["001_users"])
        runner = await apply_all(db, migrations)
        with pytest.raises(MigrationRollbackError, match="no down script"):
            await runner.rollback()
        assert applied_versions(db) == ["001_users"]
        assert db.table_exists("users")

    @pytest.mark.asyncio
    async def test_failing_down_script_leaves_row(self, db, migrations):
        write_sibling_set(migrations, ["001_users"])
        (migrations / "001_users.down.sql").write_text("DROP TABLE does_not_exist;")
        runner = await apply_all(db, migrations)
        with pytest.raises(MigrationRollbackError, match="001_users"):
            await runner.rollback()
        assert applied_versions(db) == ["001_users"]
        assert db.table_exists("users")

    @pytest.mark.asyncio
    async def test_both_steps_and_to_rejected(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        with pytest.raises(ValueError):
            await runner.plan_rollback(steps=1, to="001_users")

    @pytest.mark.asyncio
    async def test_rollback_more_than_applied_undoes_everything(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        assert await runner.rollback(steps=10) == ["003_tags", "002_posts", "001_users"]
        assert applied_versions(db) == []


# ---------------------------------------------------------------------------
# plan_rollback
# ---------------------------------------------------------------------------

class TestPlan:
    @pytest.mark.asyncio
    async def test_plan_describes_without_running(self, db, migrations):
        write_sibling_set(migrations, ["001_users", "002_posts"])
        (migrations / "003_tags.sql").write_text(
            f"{UP['003_tags']}\n-- down\n{DOWN['003_tags']}\n"
        )
        runner = await apply_all(db, migrations)

        plan = await runner.plan_rollback(steps=2)

        assert [s.version for s in plan] == ["003_tags", "002_posts"]
        assert all(isinstance(s, RollbackStep) for s in plan)
        assert plan[0].down_source == "marker"
        assert plan[0].down_path == migrations / "003_tags.sql"
        assert plan[1].down_source == "file"
        assert plan[1].down_path == migrations / "002_posts.down.sql"
        assert all(s.has_down for s in plan)
        assert not any(s.down_changed for s in plan)
        # Only described, nothing ran
        assert applied_versions(db) == ["001_users", "002_posts", "003_tags"]
        assert db.table_exists("tags")

    @pytest.mark.asyncio
    async def test_plan_defaults_to_one_step(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        plan = await runner.plan_rollback()
        assert [s.version for s in plan] == ["003_tags"]

    @pytest.mark.asyncio
    async def test_plan_to_version(self, db, migrations):
        write_sibling_set(migrations)
        runner = await apply_all(db, migrations)
        plan = await runner.plan_rollback(to="001_users")
        assert [s.version for s in plan] == ["003_tags", "002_posts"]

    @pytest.mark.asyncio
    async def test_plan_with_nothing_applied(self, db, migrations):
        write_sibling_set(migrations)
        runner = MigrationRunner(db, migrations)
        assert await runner.plan_rollback(steps=3) == []

    @pytest.mark.asyncio
    async def test_sibling_wins_over_marker_with_warning(self, db, migrations, caplog):
        (migrations / "001_users.sql").write_text(
            f"{UP['001_users']}\n-- down\nDROP TABLE not_the_one;\n"
        )
        (migrations / "001_users.down.sql").write_text(DOWN["001_users"])
        runner = await apply_all(db, migrations)
        with caplog.at_level(logging.WARNING, logger="nexusql.migrations"):
            plan = await runner.plan_rollback()
        assert plan[0].down_source == "file"
        assert "both" in caplog.text
        await runner.rollback()
        assert not db.table_exists("users")


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

class TestChecksums:
    def test_split_marker(self):
        up, down = split_down_marker(b"CREATE TABLE t (id INT);\n-- down\nDROP TABLE t;\n")
        assert up == b"CREATE TABLE t (id INT);\n"
        assert down == b"DROP TABLE t;\n"
        assert split_down_marker(b"CREATE TABLE t (id INT);") == (b"CREATE TABLE t (id INT);", None)

    def test_split_marker_crlf(self):
        up, down = split_down_marker(b"CREATE TABLE t (id INT);\r\n-- down\r\nDROP TABLE t;\r\n")
        assert up == b"CREATE TABLE t (id INT);\r\n"
        assert down == b"DROP TABLE t;\r\n"

    def test_up_checksum_of_plain_file_is_the_whole_file(self, migrations):
        """Rows recorded before down scripts existed must still verify."""
        path = migrations / "001_users.sql"
        path.write_text(UP["001_users"])
        import hashlib
        assert compute_checksum(path) == hashlib.sha256(path.read_bytes()).hexdigest()

    @pytest.mark.asyncio
    async def test_editing_the_down_section_does_not_block_up_runs(self, db, migrations):
        write_marker_set(migrations, ["001_users"])
        runner = await apply_all(db, migrations)
        (migrations / "001_users.sql").write_text(
            f"{UP['001_users']}\n-- down\nDROP TABLE IF EXISTS users;\n"
        )
        await runner.verify_checksums()
        assert await runner.run_pending_migrations()

    @pytest.mark.asyncio
    async def test_editing_the_up_section_still_blocks(self, db, migrations):
        write_marker_set(migrations, ["001_users"])
        runner = await apply_all(db, migrations)
        (migrations / "001_users.sql").write_text(
            f"{UP['001_users']} -- edited\n-- down\n{DOWN['001_users']}\n"
        )
        with pytest.raises(MigrationChecksumMismatch):
            await runner.run_pending_migrations()

    @pytest.mark.asyncio
    async def test_changed_down_script_warns_and_runs(self, db, migrations, caplog):
        write_sibling_set(migrations, ["001_users"])
        runner = await apply_all(db, migrations)
        (migrations / "001_users.down.sql").write_text("DROP TABLE IF EXISTS users;")

        with caplog.at_level(logging.WARNING, logger="nexusql.migrations"):
            plan = await runner.plan_rollback()
            assert plan[0].down_changed
            assert "changed since it was applied" in caplog.text
            assert await runner.rollback() == ["001_users"]
        assert not db.table_exists("users")

    @pytest.mark.asyncio
    async def test_old_rows_without_down_checksum_roll_back_quietly(self, db, migrations, caplog):
        """A migrations table from before down_checksum existed gains the
        column, and its NULL rows are not reported as changed."""
        db.execute(
            "CREATE TABLE ia_migrations (id SERIAL PRIMARY KEY, version VARCHAR(255) NOT NULL, "
            "filename VARCHAR(500) NOT NULL, migration_type VARCHAR(50) NOT NULL, description TEXT, "
            "applied_at TIMESTAMP DEFAULT NOW(), checksum VARCHAR(64), UNIQUE (version, migration_type))"
        )
        write_sibling_set(migrations, ["001_users"])
        db.execute(UP["001_users"])
        db.execute(
            "INSERT INTO ia_migrations (version, filename, migration_type, description, checksum) "
            "VALUES (:v, :f, 'app', 'old', :c)",
            {"v": "001_users", "f": "001_users.sql", "c": compute_checksum(migrations / "001_users.sql")},
        )
        runner = MigrationRunner(db, migrations)

        with caplog.at_level(logging.WARNING, logger="nexusql.migrations"):
            plan = await runner.plan_rollback()
        assert plan[0].recorded_down_checksum is None
        assert not plan[0].down_changed
        assert "changed since" not in caplog.text

        assert await runner.rollback() == ["001_users"]
        assert not db.table_exists("users")
        # The column now exists and new rows fill it in
        assert await runner.run_pending_migrations()
        row = db.fetch_one("SELECT down_checksum FROM ia_migrations WHERE version = '001_users'")
        assert row["down_checksum"] is not None
