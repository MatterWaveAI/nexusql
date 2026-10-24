"""
Database manager implementation
"""

import sqlite3
import logging
import re
import threading

from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Any, Dict, List
from .interfaces import ConnectionConfig, DatabaseType, QueryResult
from .pool import ConnectionPool, GenericPool, NullPool, PostgresPool

try:
    import psycopg
    from psycopg.rows import dict_row
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False

logger = logging.getLogger(__name__)


# Matches a complete ``:name`` parameter token.
#
# The negative lookbehind excludes PostgreSQL ``::`` casts, so ``value::text``
# is not mistaken for a parameter named ``text``. The greedy ``\w+`` matches
# the whole name, so ``:id`` does not match the prefix of ``:id_org``.
#
# PostgreSQL is the canonical source dialect, so every backend has to read
# ``:name`` the same way: a query that works against PostgreSQL must not be
# reinterpreted somewhere else.
PARAM_TOKEN = r'(?<!:):(\w+)'


# --- generated-ID retrieval ---------------------------------------------------
#
# Each driver hands back an auto-generated key differently: SQLite and
# pymysql expose cursor.lastrowid, PostgreSQL wants a RETURNING clause, and
# SQL Server wants SCOPE_IDENTITY() read in the same batch as the INSERT.
# execute(..., return_id=True) hides that behind one call. The helpers below
# are shared with the async PostgreSQL backend so both paths behave alike.

# Leading INSERT (or REPLACE) and the table it targets. Accepts the SQLite
# ``INSERT OR REPLACE`` and MySQL ``INSERT IGNORE`` modifiers, and a table
# name that is bare, schema-qualified, or double-quoted.
_INSERT_TARGET = re.compile(
    r'^\s*(?:INSERT|REPLACE)\b'
    r'(?:\s+(?:OR\s+\w+|IGNORE|LOW_PRIORITY|HIGH_PRIORITY|DELAYED))*'
    r'\s+INTO\s+'
    r'((?:"[^"]+"|\w+)(?:\.(?:"[^"]+"|\w+))?)',
    re.IGNORECASE,
)

# Column(s) of a table's primary key. The table name is resolved through
# regclass, so an unquoted name case-folds and a schema prefix is honoured,
# exactly as the INSERT itself resolves it.
PG_PRIMARY_KEY_SQL = """
    SELECT a.attname
    FROM pg_index i
    JOIN pg_attribute a
      ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = %(table)s::regclass
      AND i.indisprimary
    ORDER BY a.attnum
"""


def is_insert_statement(query: str) -> bool:
    """True for INSERT and REPLACE; the only statements that generate a key."""
    return _INSERT_TARGET.match(query) is not None


def insert_target_table(query: str) -> str:
    """
    The table an INSERT writes to, as written in the query.

    Raises ValueError when the statement is not an INSERT whose target can
    be read off the leading clause.
    """
    match = _INSERT_TARGET.match(query)
    if match is None:
        raise ValueError(
            "return_id=True needs an INSERT statement; could not find "
            f"'INSERT INTO <table>' at the start of: {query.strip()[:80]!r}"
        )
    return match.group(1)


def single_primary_key(table: str, columns: List[str]) -> str:
    """
    The one primary-key column of ``table``, or a ValueError saying why
    the generated ID is ambiguous (no key, or a composite one).
    """
    if len(columns) == 1:
        return columns[0]
    if not columns:
        detail = "has no primary key"
    else:
        detail = f"has a composite primary key ({', '.join(columns)})"
    raise ValueError(
        f"return_id=True cannot pick the generated ID: table {table} {detail}. "
        "Add an explicit RETURNING <column> to the INSERT instead."
    )


def with_returning(query: str, column: str) -> str:
    """Append ``RETURNING "column"`` to an INSERT, dropping a trailing ';'."""
    quoted = '"' + column.replace('"', '""') + '"'
    return f"{strip_trailing_semicolon(query)} RETURNING {quoted}"


def strip_trailing_semicolon(query: str) -> str:
    """The statement without its terminating ';' so a clause can follow it."""
    return query.rstrip().rstrip(';').rstrip()


def first_value(row) -> Any:
    """
    The first column of a driver row, whatever shape the driver uses.

    psycopg (dict_row) and pymysql (DictCursor) return dicts; sqlite3.Row,
    pyodbc.Row and plain tuples index by position.
    """
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


# Statements that used to be intercepted by execute() to drive transactions.
# With a pool there is no single connection for them to act on, so both the
# sync manager and the async backend reject them rather than silently send
# them to the driver, where they would open a transaction nobody tracks.
TRANSACTION_STATEMENTS = {
    'BEGIN', 'BEGIN TRANSACTION', 'START TRANSACTION',
    'COMMIT', 'COMMIT TRANSACTION',
    'ROLLBACK', 'ROLLBACK TRANSACTION',
}


def reject_transaction_statement(query: str) -> None:
    """Raise if the caller used the removed execute("BEGIN") protocol."""
    normalized = query.strip().upper().rstrip(';').strip()
    normalized = ' '.join(normalized.split())
    if normalized in TRANSACTION_STATEMENTS:
        raise RuntimeError(
            f"execute({query.strip()!r}) is no longer supported. "
            f"Use `with db.transaction():` instead."
        )


class DatabaseManager:
    """Database manager for handling database operations"""

    def __init__(self, database_url_or_config, pool_min: int = 1,
                 pool_max: int = 10, pool_timeout: float = 30.0,
                 connect_timeout: float = 10.0):
        """
        Initialize DatabaseManager.

        Args:
            database_url_or_config: Either a database URL string or a ConnectionConfig object
            pool_min: Connections opened eagerly when connecting
            pool_max: Ceiling on concurrent connections. Set to 1 for the
                single-connection behavior of earlier versions.
            pool_timeout: Seconds to wait for a free connection before
                raising PoolTimeout
            connect_timeout: Seconds to wait for the pool to open when
                connecting. Separate from pool_timeout so an unreachable
                server is reported promptly rather than after a full
                checkout timeout.
        """
        if isinstance(database_url_or_config, ConnectionConfig):
            self.config = database_url_or_config
            self.database_url = database_url_or_config.database_url
        else:
            self.database_url = database_url_or_config
            self.config = ConnectionConfig.from_url(database_url_or_config)

        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool_timeout = pool_timeout
        self._connect_timeout = connect_timeout
        self._pool: Optional[ConnectionPool] = None

        # Per-thread connection and transaction state. A pool hands out
        # several connections, so a manager-wide "in transaction" flag could
        # not say which connection held the transaction. Each thread tracks
        # its own, which also lets two threads run independent transactions
        # through one manager.
        self._state = threading.local()

        # PostgreSQL primary-key column per table, looked up once for
        # execute(..., return_id=True) and reused for later inserts.
        self._pk_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Per-thread connection state
    # ------------------------------------------------------------------

    @property
    def _pinned(self):
        """Connection this thread holds for a transaction, if any."""
        return getattr(self._state, 'pinned', None)

    @_pinned.setter
    def _pinned(self, conn):
        self._state.pinned = conn

    @property
    def _in_transaction(self) -> bool:
        """True while this thread is inside an explicit transaction."""
        return getattr(self._state, 'depth', 0) > 0

    @property
    def _connection(self):
        """
        Connection this thread is currently using.

        Returns the pinned connection during a transaction, the one borrowed
        for an in-flight operation if there is one, and otherwise checks one
        out and keeps it for this thread.

        That last case exists because callers reach for this directly to set
        driver-level options such as ``autocommit``; returning None between
        operations would break them. A connection held this way is returned
        by ``disconnect()``.
        """
        if self._pinned is not None:
            return self._pinned

        current = getattr(self._state, 'current', None)
        if current is not None:
            return current

        if self._pool is None:
            return None

        conn = self._pool.acquire(self._pool_timeout)
        self._state.current = conn
        self._state.held = True
        return conn

    @_connection.setter
    def _connection(self, conn):
        # Assigning None is how disconnect() clears state.
        self._state.current = conn
        if conn is None:
            self._state.pinned = None

    @contextmanager
    def _borrow(self):
        """
        Yield a connection for one operation.

        Inside a transaction this is the pinned connection and it is not
        returned to the pool. Outside one, a connection is checked out and
        released when the operation finishes.
        """
        if self._pinned is not None:
            yield self._pinned
            return

        if self._pool is None:
            raise RuntimeError("Database not connected; call connect() first")

        # Reuse a connection the _connection property checked out for this
        # thread, so reading that attribute and then running a query do not
        # end up on two different connections.
        held = getattr(self._state, 'held', False)
        if held:
            conn = self._state.current
            release_when_done = False
        else:
            conn = self._pool.acquire(self._pool_timeout)
            self._state.current = conn
            release_when_done = True

        discard = False
        try:
            yield conn
        except Exception:
            discard = self._is_connection_broken(conn)
            raise
        finally:
            if release_when_done:
                self._state.current = None
                self._pool.release(conn, discard=discard)
            elif discard:
                # Held connection went bad: drop it so the next access gets
                # a fresh one.
                self._state.current = None
                self._state.held = False
                self._pool.release(conn, discard=True)

    def _is_connection_broken(self, conn) -> bool:
        """True if a failure left the connection unusable for the next caller."""
        if self.config.database_type == DatabaseType.POSTGRESQL:
            return bool(getattr(conn, 'closed', 0))
        return False

    @contextmanager
    def transaction(self):
        """
        Run a block inside a database transaction.

        Commits on clean exit, rolls back if the block raises. The connection
        is pinned to the calling thread for the duration, so every statement
        in the block runs on the same connection. Nested use joins the
        outermost transaction rather than opening a second one.

            with db.transaction():
                db.execute("INSERT INTO ...")
                db.execute("UPDATE ...")
        """
        if self._pinned is not None:
            # Already inside a transaction on this thread: join it.
            self._state.depth += 1
            try:
                yield self
            finally:
                self._state.depth -= 1
            return

        if self._pool is None:
            raise RuntimeError("Database not connected; call connect() first")

        conn = self._pool.acquire(self._pool_timeout)
        self._state.pinned = conn
        self._state.depth = 1
        discard = False
        try:
            yield self
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                discard = True
            raise
        finally:
            self._state.pinned = None
            self._state.depth = 0
            self._state.current = None
            self._pool.release(conn, discard=discard)


    async def initialize(self, apply_schema: bool = True, app_migration_paths: Optional[List[str]] = None) -> bool:
        """
        Initialize the database connection and optionally apply schema migrations
        
        Args:
            apply_schema: Whether to apply migrations
            app_migration_paths: List of paths to app-specific migration directories
        """
        if not self.connect():
            return False
        
        if not apply_schema:
            return True
        
        try:
            # Import migrations module here to avoid circular imports
            from .migrations import MigrationRunner

            # Run system migrations using DatabaseManager directly
            system_migration_path = Path(__file__).parent / "migrations"
            system_runner = MigrationRunner(self, system_migration_path)
            
            logger.info("Running system migrations...")
            if not await system_runner.run_pending_migrations():
                logger.error("System migrations failed")
                return False
            
            # Run app-specific migrations if provided
            if app_migration_paths:
                for app_path_str in app_migration_paths:
                    app_path = Path(app_path_str)
                    if not app_path.exists():
                        logger.warning(f"App migration path does not exist: {app_path}")
                        continue
                    
                    logger.info(f"Running app migrations from: {app_path}")
                    app_runner = MigrationRunner(self, app_path)
                    
                    if not await app_runner.run_pending_migrations():
                        logger.error(f"App migrations failed for: {app_path}")
                        return False
            
            logger.info("All migrations completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Migration execution failed: {e}")
            return False
        
    def _make_sqlite_connection(self):
        """Open a SQLite connection."""
        # Extract path from sqlite:///path or just use as path
        if self.database_url.startswith("sqlite:///"):
            db_path = self.database_url[10:]  # Remove "sqlite:///"
        elif self.database_url.startswith("sqlite://"):
            db_path = self.database_url[9:]   # Remove "sqlite://"
        else:
            db_path = self.database_url

        # Create directory if it doesn't exist
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _make_mysql_connection(self):
        """Open a MySQL connection."""
        import pymysql
        import pymysql.cursors
        from urllib.parse import urlparse

        # Parse the database URL
        parsed = urlparse(self.database_url)

        conn = pymysql.connect(
            host=self.config.host or parsed.hostname or 'localhost',
            port=self.config.port or parsed.port or 3306,
            user=self.config.username or parsed.username,
            password=self.config.password or parsed.password,
            database=self.config.database_name or parsed.path.lstrip('/'),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False
        )
        return conn

    def _make_mssql_connection(self):
        """Open an MSSQL connection."""
        import pyodbc
        from urllib.parse import urlparse, parse_qs

        # Check if this is a raw ODBC connection string (starts with DRIVER={...})
        if self.database_url.startswith('DRIVER=') or self.database_url.startswith('Driver='):
            # Raw ODBC connection string provided
            conn = pyodbc.connect(self.database_url)
        else:
            # Parse the database URL (SQLAlchemy format: mssql+pyodbc://user:pass@host:port/db?driver=...)
            parsed = urlparse(self.database_url)
            query_params = parse_qs(parsed.query) if parsed.query else {}

            # Get driver from query params or auto-detect
            driver = None
            if 'driver' in query_params:
                driver = query_params['driver'][0]
            else:
                # Detect available driver (prefer 17 over 18, as 18 has TLS issues)
                available_drivers = pyodbc.drivers()
                for preferred in ['ODBC Driver 17 for SQL Server', 'ODBC Driver 18 for SQL Server', 'SQL Server']:
                    if preferred in available_drivers:
                        driver = preferred
                        break

            if not driver:
                raise RuntimeError("No MSSQL ODBC driver found. Install ODBC Driver 17 or 18 for SQL Server")

            # Build ODBC connection string
            conn_str_parts = [
                f"DRIVER={{{driver}}}",
                f"SERVER={self.config.host or parsed.hostname or 'localhost'},{self.config.port or parsed.port or 1433}",
                f"DATABASE={self.config.database_name or parsed.path.lstrip('/') or 'master'}",
                f"UID={self.config.username or parsed.username}",
                f"PWD={self.config.password or parsed.password}"
            ]

            # Add additional query params (like TrustServerCertificate)
            for key, values in query_params.items():
                if key.lower() != 'driver':  # Skip driver, already added
                    conn_str_parts.append(f"{key}={values[0]}")

            # If TrustServerCertificate not provided, add it by default
            if 'TrustServerCertificate' not in query_params and 'trustservercertificate' not in query_params:
                conn_str_parts.append("TrustServerCertificate=yes")

            conn_str = ";".join(conn_str_parts)
            logger.debug(f"MSSQL connection string: {conn_str!r}")
            conn = pyodbc.connect(conn_str)

        conn.autocommit = False
        return conn

    def _validate_connection(self, conn) -> bool:
        """Cheap liveness check run when a pooled connection is checked out."""
        try:
            if self.config.database_type == DatabaseType.MYSQL:
                conn.ping(reconnect=True)
                return True
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchall()
            return True
        except Exception:
            return False

    def connect(self) -> bool:
        """
        Build the connection pool.

        SQLite gets a NullPool: its connections are bound to the creating
        thread and the target is a local file, so pooling would break it
        without buying throughput. The server backends get a real pool.
        """
        try:
            if self.config.database_type == DatabaseType.SQLITE:
                self._pool = NullPool(self._make_sqlite_connection)
                # Surface connection errors here rather than on first query.
                self._pool.acquire()
                logger.info("Connected to SQLite database")
                return True

            elif self.config.database_type == DatabaseType.POSTGRESQL:
                if not PSYCOPG_AVAILABLE:
                    logger.error("psycopg not installed. Install with: pip install 'psycopg[binary,pool]'")
                    return False

                logger.info(f"Connecting to PostgreSQL: {self.database_url}")
                self._pool = PostgresPool(
                    self.database_url,
                    min_size=self._pool_min,
                    max_size=self._pool_max,
                    timeout=self._pool_timeout,
                    connect_timeout=self._connect_timeout,
                    row_factory=dict_row,
                )
                logger.info("Connected to PostgreSQL database")
                return True

            elif self.config.database_type == DatabaseType.MYSQL:
                self._pool = GenericPool(
                    self._make_mysql_connection,
                    min_size=self._pool_min,
                    max_size=self._pool_max,
                    timeout=self._pool_timeout,
                    validate=self._validate_connection,
                )
                logger.info("Connected to MySQL database")
                return True

            elif self.config.database_type == DatabaseType.MSSQL:
                self._pool = GenericPool(
                    self._make_mssql_connection,
                    min_size=self._pool_min,
                    max_size=self._pool_max,
                    timeout=self._pool_timeout,
                    validate=self._validate_connection,
                )
                logger.info("Connected to MSSQL database")
                return True

            else:
                logger.error(f"Database type {self.config.database_type} not implemented yet")
                return False
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            self._pool = None
            return False

    def disconnect(self):
        """Close the pool and every connection in it"""
        if self._pool is not None:
            self._pool.close()
            self._pool = None
            self._state.current = None
            self._state.held = False
            self._state.pinned = None
            self._state.depth = 0
            logger.info("Disconnected from database")
    
    async def close(self):
        """Async-compatible close method"""
        self.disconnect()
    
    def _translate_sql(self, sql: str) -> str:
        """
        Translate SQL from PostgreSQL syntax to target database syntax.

        PostgreSQL is the canonical source syntax for migrations.
        This method provides automatic translation to other database dialects.

        Supported translations:
        - Data types: SERIAL, BOOLEAN, VARCHAR, UUID, JSONB, TIMESTAMP
        - Functions: NOW(), gen_random_uuid()
        - Type casting: ::type syntax
        - Constraints: PostgreSQL-specific constraint syntax

        Returns:
            Translated SQL string for the target database
        """
        if self.config.database_type == DatabaseType.POSTGRESQL:
            # Translate SQLite-style to PostgreSQL if needed

            result = sql

            # INTEGER PRIMARY KEY (SQLite auto-increment) → SERIAL PRIMARY KEY (PostgreSQL)
            result = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b', 'SERIAL PRIMARY KEY', result, flags=re.IGNORECASE)
            result = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\b', 'SERIAL PRIMARY KEY', result, flags=re.IGNORECASE)

            # GROUP_CONCAT(expr) → STRING_AGG(expr::text, ',')
            result = re.sub(r'\bGROUP_CONCAT\(([^)]+)\)', r"STRING_AGG(\1::text, ',')", result, flags=re.IGNORECASE)

            return result

        if self.config.database_type == DatabaseType.SQLITE:
            
            result = sql

            # Remove transaction statements (executescript handles transactions)
            result = re.sub(r'^\s*BEGIN\s+TRANSACTION\s*;', '', result, flags=re.IGNORECASE | re.MULTILINE)
            result = re.sub(r'^\s*COMMIT\s*;', '', result, flags=re.IGNORECASE | re.MULTILINE)
            result = re.sub(r'^\s*ROLLBACK\s*;', '', result, flags=re.IGNORECASE | re.MULTILINE)

            # Data types - order matters!
            # SERIAL PRIMARY KEY must be replaced before SERIAL alone
            result = result.replace('SERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT')
            result = re.sub(r'\bSERIAL\b', 'INTEGER', result)

            # BOOLEAN → INTEGER (SQLite uses 0/1 for boolean)
            result = re.sub(r'\bBOOLEAN\b', 'INTEGER', result)

            # Boolean literals TRUE/FALSE → 1/0
            result = re.sub(r'\bTRUE\b', '1', result)
            result = re.sub(r'\bFALSE\b', '0', result)

            # VARCHAR/CHAR → TEXT (SQLite has flexible TEXT type)
            result = re.sub(r'\bVARCHAR\s*\(\s*\d+\s*\)', 'TEXT', result)
            result = re.sub(r'\bVARCHAR\b', 'TEXT', result)
            result = re.sub(r'\bCHAR\s*\(\s*\d+\s*\)', 'TEXT', result)

            # JSONB/JSON → TEXT (SQLite stores JSON as TEXT)
            result = re.sub(r'\bJSONB\b', 'TEXT', result)
            result = re.sub(r'\bJSON\b', 'TEXT', result)

            # UUID → TEXT (SQLite stores UUIDs as TEXT)
            result = re.sub(r'\bUUID\b', 'TEXT', result)

            # TIMESTAMP → TEXT (SQLite uses TEXT for timestamps)
            result = re.sub(r'\bTIMESTAMP\b', 'TEXT', result)

            # Functions
            result = re.sub(r'\bNOW\(\)', 'CURRENT_TIMESTAMP', result)
            result = re.sub(r'\bCURRENT_DATE\b', "date('now')", result)
            result = re.sub(r'\bCURRENT_TIME\b', "time('now')", result)

            # STRING_AGG(expr, ',') → GROUP_CONCAT(expr)
            result = re.sub(r"\bSTRING_AGG\(([^,]+),\s*'[^']*'\)", r'GROUP_CONCAT(\1)', result, flags=re.IGNORECASE)

            # gen_random_uuid() → remove (SQLite doesn't support function defaults in DDL)
            # Applications should generate UUIDs before insert
            result = re.sub(r'DEFAULT\s+gen_random_uuid\(\)', '', result)
            result = re.sub(r'gen_random_uuid\(\)', "lower(hex(randomblob(16)))", result)

            # Translate PostgreSQL JSON operators to SQLite JSON functions
            # Pattern: (column::jsonb)->>'key' → JSON_EXTRACT(column, '$.key')
            result = re.sub(r'\((\w+(?:\.\w+)*)::jsonb\)->>\'(\w+)\'', r"JSON_EXTRACT(\1, '$.\2')", result)
            # Pattern: column::jsonb->>'key' → JSON_EXTRACT(column, '$.key')
            result = re.sub(r'(\w+(?:\.\w+)*)::jsonb->>\'(\w+)\'', r"JSON_EXTRACT(\1, '$.\2')", result)

            # PostgreSQL type casting (::type) → remove for SQLite, which has
            # no cast syntax and is loosely typed anyway.
            # Examples: '{}'::jsonb, 'text'::varchar, id::int
            result = re.sub(r"'([^']*)'::jsonb", r"'\1'", result)
            # Strip any ::type, not a fixed list of them. An unlisted cast
            # would otherwise survive translation and reach SQLite as a
            # syntax error, or be read as a :parameter.
            result = re.sub(r'::\w+(\s*\[\s*\])?', '', result)

            # Remove SQL comments (-- style) before collapsing whitespace
            result = re.sub(r'--[^\n]*\n', '\n', result)

            # Clean up multiple spaces/tabs but KEEP newlines (needed for executescript)
            result = re.sub(r'[ \t]+', ' ', result)
            result = re.sub(r'\n\s+', '\n', result)

            return result

        # MySQL translation
        if self.config.database_type == DatabaseType.MYSQL:
            result = sql

            # AUTO_INCREMENT instead of SERIAL
            result = result.replace('SERIAL PRIMARY KEY', 'INT PRIMARY KEY AUTO_INCREMENT')
            result = re.sub(r'\bSERIAL\b', 'INT AUTO_INCREMENT', result)

            # Note: Do NOT add AUTO_INCREMENT to INTEGER PRIMARY KEY
            # In PostgreSQL canonical syntax, INTEGER PRIMARY KEY does NOT auto-increment
            # Only SERIAL PRIMARY KEY auto-increments

            # TINYINT(1) instead of BOOLEAN
            result = re.sub(r'\bBOOLEAN\b', 'TINYINT(1)', result)

            # Boolean literals TRUE/FALSE → 1/0
            result = re.sub(r'\bTRUE\b', '1', result)
            result = re.sub(r'\bFALSE\b', '0', result)

            # JSON instead of JSONB
            result = re.sub(r'\bJSONB\b', 'JSON', result)

            # UUID → CHAR(36) (MySQL stores UUIDs as strings)
            result = re.sub(r'\bUUID\b', 'CHAR(36)', result)

            # MySQL doesn't support DEFAULT with functions except for TIMESTAMP columns
            # Remove DEFAULT gen_random_uuid() - apps must provide UUIDs
            result = re.sub(r'\s+DEFAULT\s+gen_random_uuid\(\)', '', result, flags=re.IGNORECASE)

            # MySQL doesn't support DEFAULT on JSON/TEXT columns
            # Remove DEFAULT for JSON columns (common pattern: DEFAULT '{}'::jsonb or DEFAULT '{}')
            result = re.sub(r'\s+DEFAULT\s+\'[^\']*\'::jsonb', '', result, flags=re.IGNORECASE)
            result = re.sub(r'(\bJSON\b[^,\)]*)\s+DEFAULT\s+\'[^\']*\'', r'\1', result, flags=re.IGNORECASE)

            # TIMESTAMP handling (MySQL has different default behavior)
            # Keep TIMESTAMP as-is, it works in MySQL

            # Functions (for queries, not for defaults)
            result = re.sub(r'gen_random_uuid\(\)', 'UUID()', result)
            # NOW() works in MySQL, keep it

            # PostgreSQL type casting (::type) → remove for MySQL.
            # Strip any ::type, not a fixed list. An unlisted cast would
            # otherwise survive translation and be read as a :parameter.
            result = re.sub(r"'([^']*)'::jsonb", r"'\1'", result)
            result = re.sub(r'::\w+(\s*\[\s*\])?', '', result)

            # MySQL doesn't support CREATE INDEX IF NOT EXISTS
            # Remove IF NOT EXISTS from index creation
            result = re.sub(r'CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+', 'CREATE INDEX ', result, flags=re.IGNORECASE)

            return result

        # MSSQL translation
        if self.config.database_type == DatabaseType.MSSQL:
            result = sql

            # IDENTITY instead of SERIAL
            result = result.replace('SERIAL PRIMARY KEY', 'INT PRIMARY KEY IDENTITY(1,1)')
            result = re.sub(r'\bSERIAL\b', 'INT IDENTITY(1,1)', result)

            # Note: Do NOT add IDENTITY to INTEGER PRIMARY KEY
            # In PostgreSQL canonical syntax, INTEGER PRIMARY KEY does NOT auto-increment
            # Only SERIAL PRIMARY KEY auto-increments

            # BIT instead of BOOLEAN
            result = re.sub(r'\bBOOLEAN\b', 'BIT', result)

            # Boolean literals TRUE/FALSE → 1/0
            result = re.sub(r'\bTRUE\b', '1', result)
            result = re.sub(r'\bFALSE\b', '0', result)

            # NVARCHAR(MAX) instead of JSONB/JSON
            result = re.sub(r'\bJSONB\b', 'NVARCHAR(MAX)', result)
            result = re.sub(r'\bJSON\b', 'NVARCHAR(MAX)', result)

            # UNIQUEIDENTIFIER instead of UUID
            result = re.sub(r'\bUUID\b', 'UNIQUEIDENTIFIER', result)

            # VARCHAR → NVARCHAR for better Unicode support
            result = re.sub(r'\bVARCHAR\s*\((\d+)\)', r'NVARCHAR(\1)', result)
            result = re.sub(r'\bVARCHAR\b', 'NVARCHAR(MAX)', result)
            result = re.sub(r'\bTEXT\b', 'NVARCHAR(MAX)', result)

            # TIMESTAMP → DATETIME2
            result = re.sub(r'\bTIMESTAMP\b', 'DATETIME2', result)

            # Functions
            result = re.sub(r'\bNOW\(\)', 'GETDATE()', result)
            result = re.sub(r'gen_random_uuid\(\)', 'NEWID()', result)
            result = re.sub(r'\bCURRENT_TIMESTAMP\b', 'GETDATE()', result)

            # PostgreSQL type casting (::type) → remove for MSSQL.
            # Strip any ::type, not a fixed list. An unlisted cast would
            # otherwise survive translation and be read as a :parameter.
            result = re.sub(r"'([^']*)'::jsonb", r"'\1'", result)
            result = re.sub(r'::\w+(\s*\[\s*\])?', '', result)

            # MSSQL doesn't support ON DELETE SET NULL for self-referencing FKs with CASCADE
            # Change to NO ACTION to avoid conflicts
            result = re.sub(r'ON\s+DELETE\s+SET\s+NULL', 'ON DELETE NO ACTION', result, flags=re.IGNORECASE)
            result = re.sub(r'ON\s+DELETE\s+CASCADE', 'ON DELETE NO ACTION', result, flags=re.IGNORECASE)

            # CREATE TABLE IF NOT EXISTS → MSSQL conditional syntax
            # MSSQL doesn't support IF NOT EXISTS in CREATE TABLE
            # Use a line-by-line approach to wrap each CREATE TABLE statement
            lines = result.split('\n')
            processed_lines = []
            i = 0
            while i < len(lines):
                line = lines[i]
                # Check if this line starts a CREATE TABLE IF NOT EXISTS
                if re.match(r'^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)', line, re.IGNORECASE):
                    table_name_match = re.search(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)', line, re.IGNORECASE)
                    table_name = table_name_match.group(1)

                    # Remove IF NOT EXISTS from this line
                    line = re.sub(r'IF\s+NOT\s+EXISTS\s+', '', line, flags=re.IGNORECASE)

                    # Add the IF NOT EXISTS wrapper
                    processed_lines.append(f"IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[{table_name}]') AND type = 'U')")
                    processed_lines.append("BEGIN")
                    processed_lines.append("    " + line.strip())

                    # Collect all lines until we find the closing );
                    i += 1
                    paren_count = line.count('(') - line.count(')')
                    while i < len(lines):
                        current_line = lines[i]
                        paren_count += current_line.count('(') - current_line.count(')')
                        processed_lines.append("    " + current_line.strip())

                        # Check if we've reached the end of the CREATE TABLE statement
                        if paren_count <= 0 and (');' in current_line or current_line.strip().endswith(')')):
                            # Remove semicolon if present
                            if processed_lines[-1].rstrip().endswith(');'):
                                processed_lines[-1] = processed_lines[-1].rstrip()[:-1]  # Remove the semicolon
                            elif processed_lines[-1].rstrip().endswith(';'):
                                processed_lines[-1] = processed_lines[-1].rstrip()[:-1]  # Remove the semicolon
                            processed_lines.append("END")
                            break
                        i += 1
                else:
                    processed_lines.append(line)
                i += 1

            result = '\n'.join(processed_lines)

            # Also handle CREATE INDEX IF NOT EXISTS (MSSQL doesn't support it either)
            result = re.sub(r'CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+', 'CREATE INDEX ', result, flags=re.IGNORECASE)

            # LIMIT/OFFSET → OFFSET/FETCH NEXT
            # MSSQL uses: OFFSET x ROWS FETCH NEXT y ROWS ONLY
            # PostgreSQL uses: LIMIT y OFFSET x

            # Handle LIMIT with OFFSET
            result = re.sub(
                r'\bLIMIT\s+(\d+)\s+OFFSET\s+(\d+)',
                r'OFFSET \2 ROWS FETCH NEXT \1 ROWS ONLY',
                result,
                flags=re.IGNORECASE
            )

            # Handle just LIMIT (no OFFSET)
            result = re.sub(
                r'\bLIMIT\s+(\d+)(?!\s+OFFSET)',
                r'OFFSET 0 ROWS FETCH NEXT \1 ROWS ONLY',
                result,
                flags=re.IGNORECASE
            )

            return result

        # For unknown databases, return as-is and hope for the best
        logger.warning(f"No SQL translation rules for {self.config.database_type}, using SQL as-is")
        return sql

    def _convert_params(self, query: str, params: Optional[Dict] = None):
        """
        Convert named parameters (:param) to database-specific format.

        PostgreSQL: :param → %(param)s with dict params
        MySQL: :param → %s with tuple params (positional)
        MSSQL: :param → ? with tuple params (positional)
        SQLite: :param → ? with tuple params (positional)

        Returns: (converted_query, converted_params)
        """
        if not params:
            return query, None

        # Handle case where params is passed as tuple (legacy code)
        if isinstance(params, (tuple, list)):
            import traceback
            logger.error(f"Parameters passed as {type(params).__name__}, expected dict")
            logger.error(f"Query: {query[:200]}")
            logger.error(f"Params: {params}")
            logger.error(f"Stack trace:\n{''.join(traceback.format_stack())}")
            # For backwards compatibility with positional params
            if self.config.database_type in [DatabaseType.SQLITE, DatabaseType.MYSQL, DatabaseType.MSSQL]:
                return query, tuple(params) if isinstance(params, list) else params
            else:
                raise TypeError(f"Parameters must be a dict for {self.config.database_type}, got {type(params).__name__}")

        if self.config.database_type == DatabaseType.POSTGRESQL:
            # PostgreSQL uses %(name)s format with dict. Unknown :name
            # tokens are left as-is.
            new_query = re.sub(
                PARAM_TOKEN,
                lambda match: f"%({match.group(1)})s" if match.group(1) in params else match.group(0),
                query
            )

            # PostgreSQL handles Python bool natively - no conversion needed
            # psycopg converts True/False to PostgreSQL's TRUE/FALSE automatically
            return new_query, params

        elif self.config.database_type == DatabaseType.MYSQL:
            # MySQL uses %s with a positional tuple. Order the values by
            # appearance in the query, not by dict iteration order.
            param_names_in_order = re.findall(PARAM_TOKEN, query)

            # Build param list in query order
            param_list = []
            for param_name in param_names_in_order:
                if param_name not in params:
                    raise ValueError(f"Parameter :{param_name} used in query but not provided in params")
                value = params[param_name]
                # PostgreSQL handles booleans natively, MySQL uses TINYINT
                # Both accept True/False Python values correctly
                param_list.append(value)

            # Replace all :param_name with %s in one pass
            new_query = re.sub(PARAM_TOKEN, '%s', query)

            return new_query, tuple(param_list)

        elif self.config.database_type in [DatabaseType.SQLITE, DatabaseType.MSSQL]:
            # SQLite and MSSQL use ? with a positional tuple. Order the
            # values by appearance in the query, not by dict iteration order.
            param_names_in_order = re.findall(PARAM_TOKEN, query)

            # Build param list in query order
            param_list = []
            for param_name in param_names_in_order:
                if param_name not in params:
                    raise ValueError(f"Parameter :{param_name} used in query but not provided in params")
                value = params[param_name]
                # Convert booleans to integers for SQLite
                if self.config.database_type == DatabaseType.SQLITE and isinstance(value, bool):
                    param_list.append(1 if value else 0)
                else:
                    param_list.append(value)

            # Replace all :param_name with ? in one pass
            new_query = re.sub(PARAM_TOKEN, '?', query)

            return new_query, tuple(param_list)

        else:
            return query, tuple(params.values()) if params else None

    async def execute_async(self, query: str, params: Optional[Dict] = None,
                            return_id: bool = False) -> Any:
        """
        Execute a query without blocking the event loop.

        Server backends run the blocking execute in a worker thread. SQLite
        connections are thread-bound, so those queries run inline; the target
        is a local file, so there is no network stall to offload.

        Args:
            query: SQL query with named parameters like :param_name
            params: Dict of parameters {"param_name": value}
            return_id: Return the generated key of an INSERT; see execute()
        """
        import asyncio
        if self.config.database_type == DatabaseType.SQLITE:
            return self.execute(query, params, return_id=return_id)
        return await asyncio.to_thread(self.execute, query, params, return_id)


    def _execute_raw(self, query: str, params: Optional[Dict] = None,
                     conn=None):
        """
        Internal method to execute query and return cursor.

        Args:
            query: SQL query with :param_name placeholders
            params: Dict like {"param_name": "value"}
            conn: Connection to run on. Callers pass the one they borrowed so
                the cursor and any follow-up commit share a connection.

        Returns:
            cursor object
        """
        if conn is None:
            conn = self._connection
        if not conn:
            raise RuntimeError("Database not connected")

        # Translate SQL to target database dialect
        translated_query = self._translate_sql(query)

        # Convert named params to database-specific format
        converted_query, converted_params = self._convert_params(translated_query, params)

        cursor = conn.cursor()
        if converted_params:
            cursor.execute(converted_query, converted_params)
        else:
            cursor.execute(converted_query)

        return cursor

    def execute(self, query: str, params: Optional[Dict] = None,
                return_id: bool = False):
        """
        Execute a query with named parameters.

        For SELECT queries, returns List[Dict] with results.
        For INSERT/UPDATE/DELETE, returns List[Dict] (empty) for success.

        With ``return_id=True`` the statement must be an INSERT, and the
        return value is the key the database generated for the new row
        (an int for SERIAL / AUTOINCREMENT / AUTO_INCREMENT / IDENTITY
        columns), or None when no row was inserted. The same call works on
        every backend:

            user_id = db.execute(
                "INSERT INTO users (name) VALUES (:name)",
                {"name": "John"},
                return_id=True,
            )

        If the INSERT already carries a RETURNING clause (or OUTPUT on
        SQL Server), the first column of its first row is returned instead.
        On PostgreSQL the table needs a single-column primary key; otherwise
        write the RETURNING clause yourself. The value describes one row, so
        for a multi-row INSERT prefer RETURNING: the drivers disagree on
        whether the first or the last generated key is reported.

        Args:
            query: SQL query with :param_name placeholders
            params: Dict like {"param_name": "value"}
            return_id: Return the generated key of an INSERT instead of rows

        Returns:
            List[Dict]: Query results (empty list for non-SELECT queries),
            or the generated key when return_id is True
        """
        self._reject_transaction_statement(query)

        with self._borrow() as conn:
            try:
                if return_id:
                    return self._execute_return_id(query, params, conn)

                cursor = self._execute_raw(query, params, conn)

                # Check if this is a query that returns results
                query_upper = query.strip().upper()
                is_select = query_upper.startswith('SELECT') or query_upper.startswith('SHOW') or query_upper.startswith('DESCRIBE')
                # MSSQL INSERT/UPDATE/DELETE with OUTPUT clause returns results
                has_output = 'OUTPUT' in query_upper and self.config.database_type == DatabaseType.MSSQL
                # MSSQL EXEC can return results from stored procedures
                is_exec = query_upper.startswith('EXEC') and self.config.database_type == DatabaseType.MSSQL

                # INSERT/UPDATE/DELETE with RETURNING also produce result rows
                has_returning = (not is_select
                                 and re.search(r'\bRETURNING\b', query_upper) is not None)

                if is_select or has_output or is_exec or has_returning:
                    # Fetch results
                    rows = cursor.fetchall()

                    # Convert rows to dicts before any commit: SQLite refuses
                    # to commit while a cursor still has statements in
                    # progress, and the driver rows may not outlive it.
                    if not rows:
                        result = []
                    elif self.config.database_type == DatabaseType.MSSQL:
                        # pyodbc needs cursor.description for column names
                        columns = [column[0] for column in cursor.description]
                        result = [dict(zip(columns, row)) for row in rows]
                    else:
                        # SQLite and MySQL return dict-like rows
                        result = [dict(row) for row in rows]

                    if has_returning and not self._in_transaction:
                        # The write already produced its rows; commit so the
                        # data is durable rather than discarded when the
                        # connection returns to the pool. Close the cursor
                        # first: SQLite will not commit while a statement is
                        # still in progress.
                        cursor.close()
                        conn.commit()

                    return result
                else:
                    # Writes commit immediately unless the caller opened a
                    # transaction, in which case transaction() owns the commit.
                    if not self._in_transaction:
                        conn.commit()

                    return []

            except Exception as e:
                # Roll back so the connection is clean for the next caller.
                # A failed statement leaves PostgreSQL in an aborted
                # transaction state, and without this every later query on
                # that pooled connection would fail too.
                try:
                    conn.rollback()
                    logger.debug("Rolled back transaction after error")
                except Exception:
                    pass  # Ignore rollback errors
                logger.error(f"Query execution failed: {e}")
                raise

    def _execute_return_id(self, query: str, params: Optional[Dict], conn) -> Any:
        """
        Run an INSERT and return the key generated for the new row.

        Runs inside execute()'s borrow/rollback handling. Commits unless the
        caller holds a transaction, matching the other write paths.
        """
        if not is_insert_statement(query):
            raise ValueError(
                "return_id=True only applies to INSERT statements, got: "
                f"{query.strip()[:80]!r}"
            )

        db_type = self.config.database_type
        query_upper = query.upper()
        has_returning = re.search(r'\bRETURNING\b', query_upper) is not None
        has_output = (db_type == DatabaseType.MSSQL
                      and re.search(r'\bOUTPUT\b', query_upper) is not None)

        if has_returning or has_output:
            # The caller already named the column; honour that.
            cursor = self._execute_raw(query, params, conn)
            row = cursor.fetchone()
            new_id = first_value(row) if row else None

        elif db_type == DatabaseType.POSTGRESQL:
            # psycopg has no lastrowid, and lastval() would report a stale
            # sequence value when this table has no sequence at all, so ask
            # the catalog for the primary key and RETURNING it.
            column = self._postgres_primary_key(query, conn)
            cursor = self._execute_raw(with_returning(query, column), params, conn)
            row = cursor.fetchone()
            new_id = first_value(row) if row else None

        elif db_type == DatabaseType.MSSQL:
            # SCOPE_IDENTITY() is scoped to the batch, so it has to travel
            # with the INSERT; a second execute() would read NULL. The cast
            # turns pyodbc's Decimal into an int.
            batch = (strip_trailing_semicolon(query)
                     + "; SELECT CAST(SCOPE_IDENTITY() AS BIGINT)")
            cursor = self._execute_raw(batch, params, conn)
            # The INSERT's own (row-count) result comes first; move past it.
            while cursor.description is None:
                if not cursor.nextset():
                    raise RuntimeError(
                        "SCOPE_IDENTITY() produced no result set after INSERT")
            row = cursor.fetchone()
            new_id = row[0] if row else None

        else:
            # sqlite3 and pymysql both expose the driver's last insert id.
            cursor = self._execute_raw(query, params, conn)
            new_id = cursor.lastrowid
            # Nothing inserted (INSERT OR IGNORE on a conflict, INSERT ..
            # SELECT matching no rows) leaves lastrowid stale on SQLite and
            # 0 on MySQL; 0 is also MySQL's "no AUTO_INCREMENT value".
            if cursor.rowcount == 0 or not new_id:
                new_id = None

        # Close before committing: SQLite will not commit while a statement
        # is still in progress.
        cursor.close()
        if not self._in_transaction:
            conn.commit()
        return new_id

    def _postgres_primary_key(self, query: str, conn) -> str:
        """
        Primary-key column of the table an INSERT targets, from the catalog.

        Cached per table name for the life of the manager, so each table
        costs one catalog round-trip rather than one per insert.
        """
        table = insert_target_table(query)
        column = self._pk_cache.get(table)
        if column is None:
            cursor = conn.cursor()
            try:
                cursor.execute(PG_PRIMARY_KEY_SQL, {'table': table})
                columns = [first_value(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
            column = single_primary_key(table, columns)
            self._pk_cache[table] = column
        return column

    _TRANSACTION_STATEMENTS = TRANSACTION_STATEMENTS

    def _reject_transaction_statement(self, query: str) -> None:
        """Raise if the caller used the removed execute("BEGIN") protocol."""
        reject_transaction_statement(query)

    def _is_write_statement(self, query: str) -> bool:
        """Check whether a query is a write (DML) statement"""
        query_upper = query.strip().upper()
        return (query_upper.startswith('INSERT')
                or query_upper.startswith('UPDATE')
                or query_upper.startswith('DELETE')
                or query_upper.startswith('MERGE')
                or query_upper.startswith('REPLACE'))

    def _commit_write_if_autocommit(self, query: str, conn):
        """
        Commit a write issued through the fetch_* methods.

        An INSERT .. RETURNING run via fetch_one returns its row
        without committing, so the write would be discarded when the
        connection returns to the pool. Inside an explicit transaction the
        caller owns the commit, so this does nothing.
        """
        if self._in_transaction or not self._is_write_statement(query):
            return
        conn.commit()

    def fetch_one(self, query: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """
        Fetch one row with named parameters.

        Args:
            query: SQL with :param_name placeholders
            params: Dict like {"param_name": "value"}
        """
        with self._borrow() as conn:
            try:
                cursor = self._execute_raw(query, params, conn)
                row = cursor.fetchone()
                self._commit_write_if_autocommit(query, conn)
                if not row:
                    return None

                # Handle different cursor types
                if self.config.database_type == DatabaseType.MSSQL:
                    # pyodbc Row object - convert to dict using column names
                    return {cursor.description[i][0]: row[i] for i in range(len(row))}
                else:
                    # PostgreSQL/MySQL dict cursor or SQLite Row
                    return dict(row)
            except Exception as e:
                logger.error(f"fetch_one failed: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                # Raise rather than returning None: a swallowed error is
                # indistinguishable from "no row matched"
                raise

    async def fetch_one_async(self, query: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """
        Fetch one row without blocking the event loop.

        Server backends run the blocking fetch in a worker thread. SQLite
        connections are thread-bound, so those queries run inline; the target
        is a local file, so there is no network stall to offload.

        Args:
            query: SQL with :param_name placeholders
            params: Dict like {"param_name": "value"}
        """
        import asyncio
        if self.config.database_type == DatabaseType.SQLITE:
            return self.fetch_one(query, params)
        return await asyncio.to_thread(self.fetch_one, query, params)

    def create_table(self, table_name: str, schema: str):
        """Create a table with the given schema"""
        query = f"CREATE TABLE IF NOT EXISTS {table_name} ({schema})"
        self.execute(query)
        logger.info(f"Created table: {table_name}")
    
    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists - works on all supported databases"""
        try:
            if self.config.database_type == DatabaseType.POSTGRESQL:
                result = self.fetch_one("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = :table_name
                    )
                """, {"table_name": table_name})
                return result and result.get('exists', False)

            elif self.config.database_type == DatabaseType.MYSQL:
                result = self.fetch_one("""
                    SELECT COUNT(*) as count FROM information_schema.tables
                    WHERE table_name = :table_name
                    AND table_schema = DATABASE()
                """, {"table_name": table_name})
                return result and result.get('count', 0) > 0

            elif self.config.database_type == DatabaseType.MSSQL:
                result = self.fetch_one("""
                    SELECT COUNT(*) as count FROM information_schema.tables
                    WHERE table_name = :table_name
                """, {"table_name": table_name})
                return result and result.get('count', 0) > 0

            else:  # SQLite
                result = self.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name",
                    {"table_name": table_name}
                )
                return result is not None

        except Exception as e:
            # fetch_one already rolled back and released the
            # connection, so there is nothing to clean up here.
            logger.error(f"table_exists failed: {e}")
            return False

    def _split_sql_statements(self, script: str) -> List[str]:
        """
        Split a SQL script into individual statements, properly handling:
        - Single-quoted strings with escaped quotes
        - Double-quoted identifiers with escaped quotes
        - Dollar-quoted strings (PostgreSQL)
        - Single-line comments (--)
        - Multi-line comments (/* */)

        Returns a list of non-empty SQL statements.
        """
        statements = []
        current_statement = []
        i = 0
        length = len(script)

        while i < length:
            char = script[i]

            # Handle single-line comments
            if char == '-' and i + 1 < length and script[i + 1] == '-':
                # Add comment to current statement
                comment_start = i
                i += 2
                while i < length and script[i] not in ('\n', '\r'):
                    i += 1
                current_statement.append(script[comment_start:i])
                continue

            # Handle multi-line comments
            if char == '/' and i + 1 < length and script[i + 1] == '*':
                comment_start = i
                i += 2
                while i < length - 1:
                    if script[i] == '*' and script[i + 1] == '/':
                        i += 2
                        break
                    i += 1
                current_statement.append(script[comment_start:i])
                continue

            # Handle single-quoted strings
            if char == "'":
                string_start = i
                i += 1
                while i < length:
                    if script[i] == "'":
                        # Check for escaped quote
                        if i + 1 < length and script[i + 1] == "'":
                            i += 2  # Skip escaped quote
                        else:
                            i += 1  # End of string
                            break
                    else:
                        i += 1
                current_statement.append(script[string_start:i])
                continue

            # Handle double-quoted identifiers
            if char == '"':
                string_start = i
                i += 1
                while i < length:
                    if script[i] == '"':
                        # Check for escaped quote
                        if i + 1 < length and script[i + 1] == '"':
                            i += 2  # Skip escaped quote
                        else:
                            i += 1  # End of identifier
                            break
                    else:
                        i += 1
                current_statement.append(script[string_start:i])
                continue

            # Handle dollar-quoted strings (PostgreSQL)
            if char == '$':
                # Try to match dollar quote pattern
                dollar_end = i + 1
                while dollar_end < length and (script[dollar_end].isalnum() or script[dollar_end] == '_'):
                    dollar_end += 1

                if dollar_end < length and script[dollar_end] == '$':
                    # Found opening dollar quote
                    tag = script[i:dollar_end + 1]
                    string_start = i
                    i = dollar_end + 1

                    # Find closing dollar quote
                    while i < length:
                        if script[i:i + len(tag)] == tag:
                            i += len(tag)
                            break
                        i += 1

                    current_statement.append(script[string_start:i])
                    continue

            # Handle statement terminator
            if char == ';':
                stmt = ''.join(current_statement).strip()
                if stmt:
                    statements.append(stmt)
                current_statement = []
                i += 1
                continue

            # Regular character
            current_statement.append(char)
            i += 1

        # Add final statement if exists
        stmt = ''.join(current_statement).strip()
        if stmt:
            statements.append(stmt)

        return statements

    async def execute_script(self, script: str) -> 'QueryResult':
        """Execute a SQL script (multiple statements)"""
        try:
            # Translate SQL to target database dialect
            translated_script = self._translate_sql(script)

            if self.config.database_type == DatabaseType.SQLITE:
                # SQLite has executescript() which handles multiple statements
                with self._borrow() as conn:
                    conn.executescript(translated_script)
                    conn.commit()
            else:
                # For PostgreSQL/MySQL/MSSQL, execute statements one by one
                statements = self._split_sql_statements(translated_script)
                for statement in statements:
                    # Skip statements that are only comments/whitespace
                    # Remove all comments and check if anything remains
                    test_stmt = statement
                    # Remove single-line comments
                    import re
                    test_stmt = re.sub(r'--[^\n]*', '', test_stmt)
                    # Remove multi-line comments
                    test_stmt = re.sub(r'/\*.*?\*/', '', test_stmt, flags=re.DOTALL)
                    # If nothing left after removing comments, skip
                    if not test_stmt.strip():
                        continue
                    self.execute(statement)

            return QueryResult(success=True, data=[], row_count=0)
        except Exception as e:
            # execute() rolls back and releases its own connection on error.
            logger.error(f"Script execution failed: {e}")
            return QueryResult(success=False, data=[], row_count=0, error_message=str(e))

    # ------------------------------------------------------------------
    # Backup and restore; the implementation lives in nexusql/backup.py
    # ------------------------------------------------------------------

    def backup(self, path: str, tables: Optional[List[str]] = None) -> str:
        """
        Write a native backup of this database to ``path`` and return it.

        SQLite uses the online backup API; PostgreSQL runs ``pg_dump``
        (custom format) and MySQL ``mysqldump``, with the password passed
        through the environment; SQL Server runs ``BACKUP DATABASE``, where
        ``path`` is a path on the server. ``tables`` narrows a PostgreSQL or
        MySQL dump and is rejected for the whole-database backends. Needs
        the client binary on PATH for the server backends; export_tables()
        is the portable alternative. See docs/BACKUP_RECOVERY.md.
        """
        from .backup import backup_database
        return backup_database(self, path, tables)

    def restore(self, path: str, *, replace: bool = False) -> None:
        """
        Restore a backup written by backup() into this database.

        ``replace`` drops objects that already exist before recreating
        them (``pg_restore --clean --if-exists``, SQL Server ``WITH
        REPLACE``). SQL Server needs exclusive access to the database and
        the restore is issued from a connection to master; this manager's
        pool is closed for the duration and reopened afterwards.
        """
        from .backup import restore_database
        restore_database(self, path, replace=replace)

    def export_tables(self, path: str, tables: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Portable export: one JSON-lines file per table plus a manifest,
        written under the directory ``path``. Needs no external binary and
        reads back on any backend. Returns the manifest.
        """
        from .backup import export_tables
        return export_tables(self, path, tables)

    def import_tables(self, path: str, truncate: bool = False,
                      tables: Optional[List[str]] = None) -> Dict[str, int]:
        """
        Load an export_tables() directory with parameterised INSERTs, one
        transaction per table. ``truncate`` deletes existing rows first.
        Tables must already exist. Returns rows inserted per table.
        """
        from .backup import import_tables
        return import_tables(self, path, truncate=truncate, tables=tables)

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.disconnect()