"""
Database migration system for IA Modules.
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from .interfaces import DatabaseInterface, QueryResult


logger = logging.getLogger(__name__)


class MigrationChecksumMismatch(Exception):
    """A previously applied migration file changed on disk."""


def compute_checksum(path: Path) -> str:
    """SHA-256 hex digest of a migration file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class MigrationRecord:
    """Record of an applied migration."""
    version: str
    description: str
    applied_at: datetime
    checksum: Optional[str] = None


class MigrationRunner:
    """Handles database migration execution and tracking."""
    
    def __init__(
        self,
        database: DatabaseInterface,
        migration_path: Optional[Path] = None,
        migration_type: str = "app"
    ):
        """
        Initialize migration runner.
        
        Args:
            database: Database interface to run migrations against
            migration_path: Path to directory containing migration files
            migration_type: Type of migrations ('system' or 'app')
        """
        self.database = database
        self.migration_path = migration_path or Path(__file__).parent / "migrations"
        self.migration_type = migration_type
        self._migrations_table = "ia_migrations"
    
    async def initialize_migration_table(self) -> bool:
        """Create migrations tracking table if it doesn't exist."""
        try:
            exists = self.database.table_exists(self._migrations_table)
            if exists:
                return True

            # Use PostgreSQL canonical syntax - translates to all databases
            create_sql = """
            CREATE TABLE IF NOT EXISTS ia_migrations (
                id SERIAL PRIMARY KEY,
                version VARCHAR(255) NOT NULL,
                filename VARCHAR(500) NOT NULL,
                migration_type VARCHAR(50) NOT NULL,
                description TEXT,
                applied_at TIMESTAMP DEFAULT NOW(),
                checksum VARCHAR(64),
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
    
    async def get_applied_migrations(self) -> List[MigrationRecord]:
        """Get list of applied migrations for this migration type."""
        result = self.database.execute(
            f"SELECT version, filename, migration_type, description, applied_at, checksum FROM {self._migrations_table} WHERE migration_type = :migration_type ORDER BY version",
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
                checksum=row.get('checksum')
            ))

        return migrations
    
    async def record_migration(
        self,
        version: str,
        filename: str,
        migration_type: str,
        description: str,
        checksum: Optional[str] = None
    ) -> bool:
        """Record that a migration has been applied."""
        try:
            result = await self.database.execute_async(
                f"""
                INSERT INTO {self._migrations_table} (version, filename, migration_type, description, checksum)
                VALUES (:version, :filename, :migration_type, :description, :checksum)
                """,
                {
                    'version': version,
                    'filename': filename,
                    'migration_type': migration_type,
                    'description': description,
                    'checksum': checksum
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
            # Read migration content
            with open(migration_file, 'r', encoding='utf-8') as f:
                migration_sql = f.read()
            
            # Use filename as version - no complex parsing needed
            filename = migration_file.name
            version = filename.replace('.sql', '')
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
            # file being edited after it was applied
            success = await self.record_migration(
                version, filename, migration_type, description,
                checksum=compute_checksum(migration_file),
            )
            if not success:
                logger.error(f"Failed to record migration {version}")
                return False
            
            logger.info(f"Migration {version} completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error running migration {migration_file}: {e}")
            return False
    
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
        all_files = list(self.migration_path.glob("*.sql"))
        logger.info(f"All SQL files found: {[f.name for f in all_files]}")
        
        for file in all_files:
            # Use filename as version - simple and consistent
            filename = file.name
            version = filename.replace('.sql', '')
            
            # Check if this specific version+type combination has been applied
            migration_key = (version, self.migration_type)
            is_applied = migration_key in applied_keys
            
            logger.info(f"File: {file.name}, Version: {version}, Type: {self.migration_type}, Applied: {is_applied}")
            
            if not is_applied:
                migration_files.append(file)
        
        # Sort by version
        migration_files.sort(key=lambda f: f.stem)
        
        logger.info(f"Pending migrations to apply: {[f.name for f in migration_files]}")
        return migration_files
    
    async def verify_checksums(self) -> None:
        """
        Refuse to run when an already-applied migration file has changed.

        Editing a migration that has already run leaves databases that applied
        the old version permanently out of step with ones applying the new,
        with nothing to detect the difference later.

        Migrations recorded without a checksum (applied before checksums were
        written) cannot be verified and are skipped.
        """
        applied = {
            m.version: m.checksum
            for m in await self.get_applied_migrations()
        }
        if not self.migration_path.exists():
            return

        for migration_file in sorted(self.migration_path.glob("*.sql")):
            version = migration_file.name.replace('.sql', '')
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
        for file in self.migration_path.glob("*.sql"):
            filename = file.name
            file_version = filename.replace('.sql', '')
            
            if file_version == version:
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
