"""
NexusQL - Multi-database abstraction layer with unified API
"""

from .manager import DatabaseManager
from .pool import ConnectionPool, GenericPool, NullPool, PostgresPool, PoolTimeout
from .interfaces import (
    ConnectionConfig,
    DatabaseType,
    QueryResult,
    DatabaseInterface,
    create_query_result,
    create_error_result
)

from .migrations import MigrationRunner, MigrationChecksumMismatch
from .cdc import ChangeDataCapture, AsyncChangeDataCapture

try:
    from .async_backend import PSYCOPG_AVAILABLE
    if PSYCOPG_AVAILABLE:
        from .async_backend import AsyncPostgresBackend
except ImportError:  # postgresql extra not installed: sync-only package
    PSYCOPG_AVAILABLE = False

__version__ = "0.3.1"

__all__ = [
    'DatabaseManager',
    'ConnectionPool',
    'GenericPool',
    'NullPool',
    'PostgresPool',
    'PoolTimeout',
    'ConnectionConfig',
    'DatabaseType',
    'QueryResult',
    'DatabaseInterface',
    'create_query_result',
    'create_error_result',
    'MigrationRunner',
    'MigrationChecksumMismatch',
    'ChangeDataCapture',
    'AsyncChangeDataCapture',
]

if PSYCOPG_AVAILABLE:
    __all__ += ['AsyncPostgresBackend']
