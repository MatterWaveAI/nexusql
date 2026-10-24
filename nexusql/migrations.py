"""
Database migration system for IA Modules.
"""

import asyncio
import hashlib
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from .interfaces import DatabaseInterface, DatabaseType, QueryResult


logger = logging.getLogger(__name__)


class MigrationChecksumMismatch(Exception):
    """A previously applied migration file changed on disk."""


class MigrationRollbackError(Exception):
    """A rollback cannot be planned or did not complete."""


# A line holding nothing but ``-- down`` separates the up script from the
# down script inside a single migration file. Matched on bytes so the up
# section hashes exactly as the whole file did before down scripts existed.
DOWN_MARKER = re.compile(rb'^[ \t]*--[ \t]*down[ \t]*\r?$', re.IGNORECASE | re.MULTILINE)

DOWN_SUFFIX = ".down.sql"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class MigrationSource:
    """
    One migration as found on disk: its up script and, if provided, its down
    script.

    ``down_source`` is ``"file"`` for a sibling ``NNN_name.down.sql``,
    ``"marker"`` for a ``-- down`` section inside the migration file, and
    None when the migration cannot be rolled back.
    """
    path: Path
    up_sql: bytes
    down_sql: Optional[bytes] = None
    down_source: Optional[str] = None
    down_path: Optional[Path] = None

    @property
    def version(self) -> str:
        return version_of(self.path)

    @property
    def checksum(self) -> str:
        return sha256_hex(self.up_sql)

    @property
    def down_checksum(self) -> Optional[str]:
        return sha256_hex(self.down_sql) if self.down_sql is not None else None


def version_of(path: Path) -> str:
    """The version a migration file is recorded under: its name without .sql."""
    return path.name.replace('.sql', '')


def is_down_file(path: Path) -> bool:
    return path.name.lower().endswith(DOWN_SUFFIX)


def down_file_for(path: Path) -> Path:
    return path.with_name(version_of(path) + DOWN_SUFFIX)


def split_down_marker(content: bytes):
    """Split file bytes into (up, down) at the first ``-- down`` line."""
    match = DOWN_MARKER.search(content)
    if match is None:
        return content, None
    up = content[:match.start()]
    down = content[match.end():]
    if down.startswith(b'\n'):
        down = down[1:]
    return up, down


def load_migration(path: Path) -> MigrationSource:
    """
    Read a migration file and locate its down script.

    A sibling ``.down.sql`` file wins over a ``-- down`` section when both
    exist, because the sibling is the more deliberate of the two and a file
    is easier to review than the tail of another one.
    """
    up, marker_down = split_down_marker(path.read_bytes())
    sibling = down_file_for(path)
    if sibling.exists():
        if marker_down is not None:
            logger.warning(
                f"Migration {version_of(path)} has both a -- down section and "
                f"{sibling.name}; using the file and ignoring the section"
            )
        return MigrationSource(path, up, sibling.read_bytes(), "file", sibling)
    if marker_down is not None:
        return MigrationSource(path, up, marker_down, "marker", path)
    return MigrationSource(path, up)


def compute_checksum(path: Path) -> str:
    """
    SHA-256 hex digest of a migration's up script.

    For a file without a ``-- down`` section this is the digest of the whole
    file, so rows recorded before down scripts existed still verify.
    """
    return sha256_hex(split_down_marker(path.read_bytes())[0])


@dataclass
class MigrationRecord:
    """Record of an applied migration."""
    version: str
    description: str
    applied_at: datetime
    checksum: Optional[str] = None
    filename: Optional[str] = None
    down_checksum: Optional[str] = None


@dataclass
class RollbackStep:
    """
    One migration a rollback would undo.

    ``down_changed`` is True when a down checksum was recorded at apply time
    and the down script on disk no longer matches it.
    """
    version: str
    filename: str
    down_source: Optional[str]
    down_path: Optional[Path]
    recorded_down_checksum: Optional[str]
    current_down_checksum: Optional[str]

    @property
    def has_down(self) -> bool:
        return self.down_source is not None

    @property
    def down_changed(self) -> bool:
        return (
            self.recorded_down_checksum is not None
            and self.current_down_checksum is not None
            and self.recorded_down_checksum != self.current_down_checksum
        )


class MigrationRunner:
    """Handles database migration execution and tracking."""

    def __init__(
        self,
        database: DatabaseInterface,
        migration_path: Optional[Path] = None,
        migration_type: str = "app",
        migrations_table: str = "ia_migrations"
    ):
        """
        Initialize migration runner.

        Args:
            database: Database interface to run migrations against
            migration_path: Path to directory containing migration files
            migration_type: Type of migrations ('system' or 'app')
            migrations_table: Name of the table that records applied migrations
        """
        self.database = database
        self.migration_path = migration_path or Path(__file__).parent / "migrations"
        self.migration_type = migration_type
        self._migrations_table = migrations_table

    async def initialize_migration_table(self) -> bool:
        """Create migrations tracking table if it doesn't exist."""
        try:
            exists = self.database.table_exists(self._migrations_table)
            if exists:
                return self._ensure_down_checksum_column()

            # Use PostgreSQL canonical syntax - translates to all databases
            create_sql = f"""
            CREATE TABLE IF NOT EXISTS {self._migrations_table} (
                id SERIAL PRIMARY KEY,
                version VARCHAR(255) NOT NULL,
                filename VARCHAR(500) NOT NULL,
                migration_type VARCHAR(50) NOT NULL,
                description TEXT,
                applied_at TIMESTAMP DEFAULT NOW(),
                checksum VARCHAR(64),
                down_checksum VARCHAR(64),
                UNIQUE (version, migration_type)
            )
            """
            try:
                await self.database.execute_async(create_sql)
                return True
            except Exception as exec_error:
                logger.error(f"Failed to create migrations table: {exec_error}")
                return False
        except Exception as e:
            logger.error(f"Failed to initialize migration table: {e}")
            logger.exception(e)
            return False

    def _ensure_down_checksum_column(self) -> bool:
        """
        Add ``down_checksum`` to a migrations table created before down
        scripts were tracked. Rows from before then keep a NULL there, which
        rollback treats as "nothing to compare against".
        """
        try:
            self.database.execute(
                f"SELECT down_checksum FROM {self._migrations_table} WHERE 1 = 0"
            )
            return True
        except Exception:
            pass

        config = getattr(self.database, 'config', None)
        if config is not None and config.database_type == DatabaseType.MSSQL:
            # SQL Server has no COLUMN keyword in ALTER TABLE ... ADD
            alter_sql = f"ALTER TABLE {self._migrations_table} ADD down_checksum VARCHAR(64)"
        else:
            alter_sql = f"ALTER TABLE {self._migrations_table} ADD COLUMN down_checksum VARCHAR(64)"
        try:
            self.database.execute(alter_sql)
            return True
        except Exception as e:
            logger.error(f"Failed to add down_checksum to migrations table: {e}")
            return False

    async def get_applied_migrations(self) -> List[MigrationRecord]:
        """Get list of applied migrations for this migration type."""
        result = self.database.execute(
            f"SELECT version, filename, migration_type, description, applied_at, checksum, down_checksum FROM {self._migrations_table} WHERE migration_type = :migration_type ORDER BY version",
            {"migration_type": self.migration_type}
        )

        # execute returns a list of rows directly
        if not result:
            return []

        migrations = []
        for row in result:
            migrations.append(MigrationRecord(
                version=row['version'],
                description=row['description'],
                applied_at=row['applied_at'] if isinstance(row['applied_at'], datetime) else datetime.fromisoformat(row['applied_at']),
                checksum=row.get('checksum'),
                filename=row.get('filename'),
                down_checksum=row.get('down_checksum'),
            ))

        return migrations

    async def record_migration(
        self,
        version: str,
        filename: str,
        migration_type: str,
        description: str,
        checksum: Optional[str] = None,
        down_checksum: Optional[str] = None
    ) -> bool:
        """Record that a migration has been applied."""
        try:
            result = await self.database.execute_async(
                f"""
                INSERT INTO {self._migrations_table} (version, filename, migration_type, description, checksum, down_checksum)
                VALUES (:version, :filename, :migration_type, :description, :checksum, :down_checksum)
                """,
                {
                    'version': version,
                    'filename': filename,
                    'migration_type': migration_type,
                    'description': description,
                    'checksum': checksum,
                    'down_checksum': down_checksum
                }
            )

            # execute() returns a list of results, not a result object
            # If it doesn't raise an exception, it succeeded
            logger.info(f"Successfully recorded migration: {version}")
            return True
        except Exception as e:
            logger.error(f"Exception recording migration: {e}")
            logger.exception(e)
            return False

    async def run_migration_file(self, migration_file: Path) -> bool:
        """Run a single migration file."""
        try:
            # Only the up section runs here; the down section, if any, is
            # kept for rollback
            source = load_migration(migration_file)
            migration_sql = source.up_sql.decode('utf-8')

            # Use filename as version - no complex parsing needed
            filename = migration_file.name
            version = source.version
            description = f"Migration {filename}"

            # Use the migration type passed to the constructor
            migration_type = self.migration_type

            logger.info(f"Running migration {version} ({migration_type}): {description}")

            # Execute migration
            result = await self.database.execute_script(migration_sql)
            if not result.success:
                # QueryResult uses 'error_message' for errors — log safely
                err_msg = getattr(result, 'error_message', None) or getattr(result, 'error', None) or str(result)
                logger.error(f"Migration {version} failed: {err_msg}")
                # For debug, log the first 400 characters of the migration SQL
                logger.debug(f"Failed migration SQL preview: {migration_sql[:400]}")
                return False

            # Record migration, with a checksum so later runs can detect the
            # file being edited after it was applied, and the down script's
            # checksum so rollback can warn when that changed too
            success = await self.record_migration(
                version, filename, migration_type, description,
                checksum=source.checksum,
                down_checksum=source.down_checksum,
            )
            if not success:
                logger.error(f"Failed to record migration {version}")
                return False

            logger.info(f"Migration {version} completed successfully")
            return True

        except Exception as e:
            logger.error(f"Error running migration {migration_file}: {e}")
            return False

    def _up_files(self) -> List[Path]:
        """Migration files in apply order, leaving out ``.down.sql`` siblings."""
        files = [f for f in self.migration_path.glob("*.sql") if not is_down_file(f)]
        files.sort(key=lambda f: f.stem)
        return files

    async def get_pending_migrations(self) -> List[Path]:
        """Get list of migration files that haven't been applied."""
        if not self.migration_path.exists():
            logger.warning(f"Migration path does not exist: {self.migration_path}")
            return []

        # Get applied migrations
        applied_migrations = await self.get_applied_migrations()
        applied_keys = {(m.version, self.migration_type) for m in applied_migrations}
        logger.info(f"Applied migration keys: {applied_keys}")

        # Find migration files
        migration_files = []
        all_files = self._up_files()
        logger.info(f"All SQL files found: {[f.name for f in all_files]}")

        for file in all_files:
            # Use filename as version - simple and consistent
            version = version_of(file)

            # Check if this specific version+type combination has been applied
            migration_key = (version, self.migration_type)
            is_applied = migration_key in applied_keys

            logger.info(f"File: {file.name}, Version: {version}, Type: {self.migration_type}, Applied: {is_applied}")

            if not is_applied:
                migration_files.append(file)

        logger.info(f"Pending migrations to apply: {[f.name for f in migration_files]}")
        return migration_files

    async def verify_checksums(self) -> None:
        """
        Refuse to run when an already-applied migration file has changed.

        Editing a migration that has already run leaves databases that applied
        the old version permanently out of step with ones applying the new,
        with nothing to detect the difference later.

        Only the up script is checked. The down script is compared at
        rollback time and a change there warns rather than blocks, since the
        database already holds the result of the up script and the down
        script can legitimately be written or fixed later.

        Migrations recorded without a checksum (applied before checksums were
        written) cannot be verified and are skipped.
        """
        applied = {
            m.version: m.checksum
            for m in await self.get_applied_migrations()
        }
        if not self.migration_path.exists():
            return

        for migration_file in self._up_files():
            version = version_of(migration_file)
            recorded = applied.get(version)
            if not recorded:
                continue
            current = compute_checksum(migration_file)
            if current != recorded:
                raise MigrationChecksumMismatch(
                    f"Migration {version} was applied with checksum "
                    f"{recorded} but the file now hashes to {current}; "
                    f"refusing to run migrations."
                )

    async def run_pending_migrations(self) -> bool:
        """Run all pending migrations."""
        # Initialize migration table
        if not await self.initialize_migration_table():
            logger.error("Failed to initialize migration table")
            return False

        # Stop before applying anything if an already-applied file changed
        await self.verify_checksums()

        # Get pending migrations
        pending_migrations = await self.get_pending_migrations()

        if not pending_migrations:
            logger.info("No pending migrations")
            return True

        logger.info(f"Found {len(pending_migrations)} pending migrations")

        # Run each migration
        for migration_file in pending_migrations:
            success = await self.run_migration_file(migration_file)
            if not success:
                logger.error(f"Migration failed, stopping execution: {migration_file}")
                return False

        logger.info("All pending migrations completed successfully")
        return True

    async def run_specific_migration(self, version: str) -> bool:
        """Run a specific migration by version."""
        if not self.migration_path.exists():
            logger.error(f"Migration path does not exist: {self.migration_path}")
            return False

        # Find migration file
        migration_file = None
        for file in self._up_files():
            if version_of(file) == version:
                migration_file = file
                break

        if not migration_file:
            logger.error(f"Migration file not found for version: {version}")
            return False

        # Initialize migration table
        if not await self.initialize_migration_table():
            logger.error("Failed to initialize migration table")
            return False

        # Run migration
        return await self.run_migration_file(migration_file)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def applied(self) -> List[MigrationRecord]:
        """Applied migrations of this runner's type, oldest first."""
        if not await self.initialize_migration_table():
            raise MigrationRollbackError("Failed to initialize migration table")
        return await self.get_applied_migrations()

    async def pending(self) -> List[Path]:
        """Migration files not yet applied, in the order they would run."""
        if not await self.initialize_migration_table():
            raise MigrationRollbackError("Failed to initialize migration table")
        return await self.get_pending_migrations()

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def _resolve_version(self, version: str, applied: List[MigrationRecord]) -> str:
        """
        Match ``version`` to an applied migration.

        An exact match on the recorded version wins. Failing that, a bare
        prefix such as ``"002"`` or ``"V002"`` matches the one migration
        whose version starts with it followed by a separator, so callers
        can name a migration by number without typing its description.
        """
        versions = [m.version for m in applied]
        if version in versions:
            return version
        prefixed = [
            v for v in versions
            if v.startswith(version) and len(v) > len(version) and not v[len(version)].isalnum()
        ]
        if len(prefixed) == 1:
            return prefixed[0]
        if len(prefixed) > 1:
            raise MigrationRollbackError(
                f"Version {version!r} is ambiguous; it matches {prefixed}"
            )
        raise MigrationRollbackError(
            f"Version {version!r} is not an applied {self.migration_type} migration; "
            f"applied: {versions}"
        )

    def _step_for(self, record: MigrationRecord) -> RollbackStep:
        filename = record.filename or f"{record.version}.sql"
        up_file = self.migration_path / filename
        if up_file.exists():
            source = load_migration(up_file)
            return RollbackStep(
                version=record.version,
                filename=filename,
                down_source=source.down_source,
                down_path=source.down_path,
                recorded_down_checksum=record.down_checksum,
                current_down_checksum=source.down_checksum,
            )

        # The up file is gone; a sibling down file on its own still counts
        sibling = down_file_for(up_file)
        if sibling.exists():
            down = sibling.read_bytes()
            return RollbackStep(
                version=record.version,
                filename=filename,
                down_source="file",
                down_path=sibling,
                recorded_down_checksum=record.down_checksum,
                current_down_checksum=sha256_hex(down),
            )
        return RollbackStep(record.version, filename, None, None, record.down_checksum, None)

    async def plan_rollback(
        self,
        steps: Optional[int] = None,
        to: Optional[str] = None
    ) -> List[RollbackStep]:
        """
        Describe what a rollback would undo, newest first, without running it.

        Pass ``steps`` to undo that many of the most recent migrations, or
        ``to`` to undo everything applied after that version (which itself
        stays applied). With neither, one step is planned.

        Raises MigrationRollbackError if any planned migration has no down
        script, so a rollback never stops half way for want of one. A down
        script that changed since the migration was applied is logged as a
        warning and left in the plan.
        """
        if steps is not None and to is not None:
            raise ValueError("Pass either steps or to, not both")
        if steps is None and to is None:
            steps = 1
        if steps is not None and steps < 0:
            raise ValueError("steps must not be negative")

        if not await self.initialize_migration_table():
            raise MigrationRollbackError("Failed to initialize migration table")

        # Newest first, by version: the order the up run used, reversed
        applied = sorted(
            await self.get_applied_migrations(), key=lambda m: m.version, reverse=True
        )

        if to is not None:
            target = self._resolve_version(to, applied)
            selected = []
            for record in applied:
                if record.version == target:
                    break
                selected.append(record)
        else:
            selected = applied[:steps]

        plan = [self._step_for(record) for record in selected]

        missing = [step.version for step in plan if not step.has_down]
        if missing:
            raise MigrationRollbackError(
                f"Cannot roll back: no down script for {missing}. Add a "
                f"<version>.down.sql file or a -- down section to the migration."
            )

        for step in plan:
            if step.down_changed:
                logger.warning(
                    f"Down script for {step.version} changed since it was applied "
                    f"(recorded {step.recorded_down_checksum}, now "
                    f"{step.current_down_checksum}); running the current one"
                )
        return plan

    async def rollback(self, steps: int = 1) -> List[str]:
        """
        Undo the most recent ``steps`` migrations, newest first.

        Returns the versions rolled back. Nothing runs unless every planned
        migration has a down script.
        """
        return await self._run_rollback(await self.plan_rollback(steps=steps))

    async def rollback_to(self, version: str) -> List[str]:
        """
        Undo every migration applied after ``version``, newest first.

        ``version`` is the migration as recorded (its filename without .sql)
        or an unambiguous numeric prefix of it. It stays applied. Returns the
        versions rolled back.
        """
        return await self._run_rollback(await self.plan_rollback(to=version))

    @contextmanager
    def _atomic(self):
        """
        One transaction around a down script and the removal of its row.

        DatabaseManager exposes transaction(); a bare DatabaseInterface may
        not, in which case the two run back to back as the up path does.
        """
        begin = getattr(self.database, 'transaction', None)
        if begin is None:
            yield
            return
        with begin():
            yield

    async def _run_rollback(self, plan: List[RollbackStep]) -> List[str]:
        done: List[str] = []
        for step in plan:
            down_sql = step.down_path.read_bytes()
            if step.down_source == "marker":
                down_sql = split_down_marker(down_sql)[1]
            logger.info(f"Rolling back migration {step.version} ({self.migration_type})")
            try:
                with self._atomic():
                    result = await self.database.execute_script(down_sql.decode('utf-8'))
                    if not result.success:
                        err_msg = getattr(result, 'error_message', None) or str(result)
                        raise MigrationRollbackError(
                            f"Down script for {step.version} failed: {err_msg}"
                        )
                    self.database.execute(
                        f"DELETE FROM {self._migrations_table} "
                        f"WHERE version = :version AND migration_type = :migration_type",
                        {"version": step.version, "migration_type": self.migration_type},
                    )
            except MigrationRollbackError:
                raise
            except Exception as e:
                raise MigrationRollbackError(
                    f"Rollback of {step.version} failed: {e}"
                ) from e
            done.append(step.version)
            logger.info(f"Rolled back migration {step.version}")
        return done
