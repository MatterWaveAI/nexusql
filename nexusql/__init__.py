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
]

if PSYCOPG_AVAILABLE:
    __all__ += ['AsyncPostgresBackend']

# Test helpers (nexusql.testing) resolve on first use, so applications that
# never test through nexusql do not pay for importing them.
_TESTING_EXPORTS = ('Factory', 'Sequence', 'SubFactory', 'Snapshot', 'snapshot', 'truncate')
__all__ += list(_TESTING_EXPORTS)


def __getattr__(name):
    if name in _TESTING_EXPORTS:
        from . import testing
        return getattr(testing, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
