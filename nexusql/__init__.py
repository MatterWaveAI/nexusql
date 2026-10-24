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
from .sharding import (
    HashRouter,
    KeyRouter,
    RangeRouter,
    ShardedDatabaseManager,
    ShardingError,
)

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
    'ShardedDatabaseManager',
    'KeyRouter',
    'HashRouter',
    'RangeRouter',
    'ShardingError',
]

if PSYCOPG_AVAILABLE:
    __all__ += ['AsyncPostgresBackend']

# feature/migration-rollback

from .migrations import (
    MigrationRollbackError,
    MigrationRecord,
    RollbackStep,
)

__all__ += [
    'MigrationRollbackError',
    'MigrationRecord',
    'RollbackStep',
]

# feature/test-fixtures

_TESTING_EXPORTS = ('Factory', 'Sequence', 'SubFactory', 'Snapshot', 'snapshot', 'truncate')

__all__ += list(_TESTING_EXPORTS)

def __getattr__(name):
    if name in _TESTING_EXPORTS:
        from . import testing
        return getattr(testing, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# feature/backup-recovery

from .backup import (
    BackupError,
    BackupToolNotFound,
)

__all__ += [
    'BackupError',
    'BackupToolNotFound',
]

# feature/change-data-capture

from .cdc import (
    ChangeDataCapture,
    AsyncChangeDataCapture,
)

__all__ += [
    'ChangeDataCapture',
    'AsyncChangeDataCapture',
]

# feature/full-text-search

from .fts import (
    FullTextSearch,
    AsyncFullTextSearch,
)

__all__ += [
    'FullTextSearch',
    'AsyncFullTextSearch',
]

# feature/schema-validation

from .schema import (
    ColumnInfo,
    ColumnSpec,
    IndexInfo,
    IndexSpec,
    SchemaValidationError,
    TableInfo,
    TableNotFoundError,
    TableSpec,
    ValidationReport,
)

__all__ += [
    'ColumnInfo',
    'ColumnSpec',
    'IndexInfo',
    'IndexSpec',
    'SchemaValidationError',
    'TableInfo',
    'TableNotFoundError',
    'TableSpec',
    'ValidationReport',
]

# feature/query-builder

from .query import (
    Q,
    QueryError,
    Expr,
    Col,
    Param,
    Raw,
    Statement,
    Select,
    Insert,
    Update,
    Delete,
)

__all__ += [
    'Q',
    'QueryError',
    'Expr',
    'Col',
    'Param',
    'Raw',
    'Statement',
    'Select',
    'Insert',
    'Update',
    'Delete',
]

# feature/row-level-security

from .tenancy import (
    PG_TENANT_SETTING,
    TENANT_POLICY_NAME,
    disable_row_security_statements,
    row_security_statements,
    scoped_name,
)

__all__ += [
    'PG_TENANT_SETTING',
    'TENANT_POLICY_NAME',
    'disable_row_security_statements',
    'row_security_statements',
    'scoped_name',
]

# feature/encryption-compliance

from .crypto import (
    CRYPTOGRAPHY_AVAILABLE,
    DecryptionError,
    FieldEncryptor,
    KeyRing,
    blind_index,
    erase,
    mask,
    token_key_id,
)

__all__ += [
    'CRYPTOGRAPHY_AVAILABLE',
    'DecryptionError',
    'FieldEncryptor',
    'KeyRing',
    'blind_index',
    'erase',
    'mask',
    'token_key_id',
]

# feature/query-performance

from .performance import (
    QueryStat,
    QueryTiming,
)

__all__ += [
    'QueryStat',
    'QueryTiming',
]
