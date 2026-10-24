"""
Schema introspection and validation.

Three things live here:

- Introspection: ``TableInfo`` for a table as the database reports it, with
  each column's raw dialect type normalised to a small set of *kinds*
  (integer, bigint, float, decimal, text, boolean, timestamp, date, json,
  uuid, bytes, other) so the same code can reason about a table whether it
  sits in PostgreSQL, MySQL, SQL Server or SQLite.
- Validation: a ``TableSpec`` describes what the application expects;
  ``build_report`` compares it with the introspected tables and lists every
  difference in plain language.
- Parameter checking: ``check_params`` compares a ``:name`` params dict with
  the introspected column kinds before a query runs, so a wrong-typed value
  is reported with its key rather than as a driver error deep in a stack.

Kinds are compared through the translator's equivalences, not literally.
PostgreSQL is the canonical dialect and ``DatabaseManager._translate_sql``
collapses several canonical types into one on the other backends (BOOLEAN
becomes INTEGER on SQLite, UUID becomes CHAR(36) on MySQL, JSONB becomes
NVARCHAR(MAX) on SQL Server, and so on). A spec written against the
canonical types therefore validates on every backend: an ``integer`` column
on SQLite satisfies a ``boolean`` expectation because that is exactly what
the translator produced.

Catalog sources: ``information_schema.columns`` plus key constraints on
PostgreSQL, MySQL and SQL Server (``pg_get_serial_sequence`` and
``pg_index`` where information_schema is silent), and the table-valued
``pragma_table_info`` / ``pragma_index_list`` / ``pragma_index_info``
functions on SQLite. The table-valued forms are used because they accept a
bound parameter and return rows through the ordinary SELECT path.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import re
import uuid as _uuid
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .interfaces import DatabaseType

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .manager import DatabaseManager


KINDS: Tuple[str, ...] = (
    'integer', 'bigint', 'float', 'decimal', 'text', 'boolean',
    'timestamp', 'date', 'json', 'uuid', 'bytes', 'other',
)


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnInfo:
    """One column as the database reports it."""
    name: str
    raw_type: str
    kind: str
    nullable: bool
    default: Optional[str] = None
    is_primary_key: bool = False
    is_auto_increment: bool = False


@dataclass(frozen=True)
class IndexInfo:
    """One index; ``primary`` marks the index that backs the primary key."""
    name: str
    columns: Tuple[str, ...]
    unique: bool = False
    primary: bool = False


@dataclass
class TableInfo:
    """A table's columns and indexes."""
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)
    indexes: List[IndexInfo] = field(default_factory=list)

    @property
    def primary_key(self) -> List[str]:
        return [c.name for c in self.columns if c.is_primary_key]

    def column(self, name: str) -> Optional[ColumnInfo]:
        """
        Look a column up by name, falling back to a case-insensitive match.

        PostgreSQL folds unquoted identifiers to lower case while SQL Server
        and MySQL keep the declared spelling, so a spec written once must
        find the column on all of them.
        """
        for col in self.columns:
            if col.name == name:
                return col
        lowered = name.lower()
        for col in self.columns:
            if col.name.lower() == lowered:
                return col
        return None


@dataclass
class ColumnSpec:
    """
    What a column is expected to be.

    ``kind`` is a normalised kind name or a canonical PostgreSQL type such
    as ``SERIAL`` or ``VARCHAR(255)``; either spelling is normalised when
    the spec is built. ``auto_increment`` is only checked when set.
    """
    name: str
    kind: str
    nullable: bool = True
    primary_key: bool = False
    auto_increment: Optional[bool] = None

    def __post_init__(self) -> None:
        self.kind = normalize_kind(self.kind, DatabaseType.POSTGRESQL)

    @classmethod
    def from_dict(cls, data: Union[str, Mapping[str, Any]], name: Optional[str] = None) -> 'ColumnSpec':
        """Build from ``{"name": .., "kind": ..}`` or, given ``name``, a bare kind string."""
        if isinstance(data, str):
            if name is None:
                raise ValueError("a column given as a bare kind string needs a name")
            return cls(name=name, kind=data)
        fields = dict(data)
        if name is not None:
            fields.setdefault('name', name)
        return cls(**fields)


@dataclass
class IndexSpec:
    """
    An index expected on ``columns``, in order.

    Matched on columns rather than name: the same canonical DDL yields
    ``sqlite_autoindex_users_1`` on SQLite and ``users_email_key`` on
    PostgreSQL. A unique index satisfies a non-unique expectation, and the
    primary key counts as a unique index on its columns.
    """
    columns: Tuple[str, ...]
    unique: bool = False
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.columns, str):
            self.columns = (self.columns,)
        self.columns = tuple(self.columns)

    @classmethod
    def from_dict(cls, data: Union[str, Sequence[str], Mapping[str, Any]]) -> 'IndexSpec':
        if isinstance(data, Mapping):
            return cls(**dict(data))
        return cls(columns=data)  # type: ignore[arg-type]


@dataclass
class TableSpec:
    """Expected shape of one table."""
    name: str
    columns: List[ColumnSpec] = field(default_factory=list)
    indexes: List[IndexSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], name: Optional[str] = None) -> 'TableSpec':
        """
        Build from the dict form::

            {"name": "users",
             "columns": [{"name": "id", "kind": "integer", "primary_key": True}, ...],
             "indexes": [{"columns": ["email"], "unique": True}]}

        ``columns`` may also be a mapping of name to kind string (or to a
        dict of the remaining fields), and ``indexes`` entries may be a bare
        column list.
        """
        table_name = data.get('name', name)
        if table_name is None:
            raise ValueError("table spec needs a name")

        raw_columns = data.get('columns', [])
        if isinstance(raw_columns, Mapping):
            columns = [ColumnSpec.from_dict(v, name=k) for k, v in raw_columns.items()]
        else:
            columns = [c if isinstance(c, ColumnSpec) else ColumnSpec.from_dict(c)
                       for c in raw_columns]

        indexes = [i if isinstance(i, IndexSpec) else IndexSpec.from_dict(i)
                   for i in data.get('indexes', [])]
        return cls(name=table_name, columns=columns, indexes=indexes)


ExpectedSchema = Union[
    Sequence[Union[TableSpec, Mapping[str, Any]]],
    Mapping[str, Union[TableSpec, Mapping[str, Any]]],
]


def coerce_specs(expected: ExpectedSchema) -> List[TableSpec]:
    """Accept TableSpec objects, their dict form, or a ``{name: spec}`` mapping."""
    if isinstance(expected, TableSpec):
        return [expected]
    if isinstance(expected, Mapping):
        return [
            spec if isinstance(spec, TableSpec) else TableSpec.from_dict(spec, name=name)
            for name, spec in expected.items()
        ]
    return [
        spec if isinstance(spec, TableSpec) else TableSpec.from_dict(spec)
        for spec in expected
    ]


@dataclass
class ValidationReport:
    """
    Outcome of ``validate_schema``.

    ``errors`` is the full list of differences in reading order; the typed
    lists break the same findings out for code that wants to act on them.
    ``warnings`` holds unexpected columns when ``strict`` was off.
    """
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_tables: List[str] = field(default_factory=list)
    missing_columns: List[Tuple[str, str]] = field(default_factory=list)
    unexpected_columns: List[Tuple[str, str]] = field(default_factory=list)
    missing_indexes: List[Tuple[str, Tuple[str, ...]]] = field(default_factory=list)
    tables_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        noun = 'table' if self.tables_checked == 1 else 'tables'
        if self.ok:
            head = f"schema OK ({self.tables_checked} {noun} checked)"
        else:
            count = len(self.errors)
            head = (f"schema validation failed with {count} "
                    f"{'error' if count == 1 else 'errors'} "
                    f"({self.tables_checked} {noun} checked):")
        lines = [head] + [f"  - {e}" for e in self.errors]
        if self.warnings:
            lines.append("warnings:")
            lines += [f"  - {w}" for w in self.warnings]
        return '\n'.join(lines)


class SchemaValidationError(ValueError):
    """
    Raised by ``check_params`` when a params dict does not fit the table.

    ``keys`` names the offending parameters; ``errors`` says what is wrong
    with each one.
    """

    def __init__(self, table: str, errors: Sequence[str], keys: Sequence[str]):
        self.table = table
        self.errors = list(errors)
        self.keys = list(keys)
        super().__init__(
            f"params do not fit table {table}: " + '; '.join(self.errors)
        )


class TableNotFoundError(LookupError):
    """``introspect`` was asked about a table the database does not have."""


# ---------------------------------------------------------------------------
# Kind normalisation
# ---------------------------------------------------------------------------

_KIND_BY_BASE: Dict[str, str] = {}
for _kind, _names in {
    'integer': ('int', 'integer', 'int4', 'smallint', 'int2', 'mediumint',
                'tinyint', 'serial', 'smallserial', 'serial4', 'serial2'),
    'bigint': ('bigint', 'int8', 'bigserial', 'serial8'),
    'float': ('real', 'float', 'float4', 'float8', 'double', 'double precision'),
    'decimal': ('numeric', 'decimal', 'dec', 'money', 'smallmoney', 'number'),
    'text': ('text', 'varchar', 'character varying', 'char', 'character',
             'bpchar', 'nvarchar', 'nchar', 'ntext', 'tinytext', 'mediumtext',
             'longtext', 'clob', 'string', 'enum', 'set', 'citext', 'name',
             'varchar2', 'nvarchar2'),
    'boolean': ('boolean', 'bool', 'bit'),
    'timestamp': ('timestamp', 'timestamp without time zone',
                  'timestamp with time zone', 'timestamptz', 'datetime',
                  'datetime2', 'datetimeoffset', 'smalldatetime'),
    'date': ('date',),
    'json': ('json', 'jsonb'),
    'uuid': ('uuid', 'uniqueidentifier'),
    'bytes': ('bytea', 'blob', 'binary', 'varbinary', 'image', 'tinyblob',
              'mediumblob', 'longblob', 'raw'),
}.items():
    for _name in _names:
        _KIND_BY_BASE[_name] = _kind

_TYPE_SHAPE = re.compile(r'^\s*([a-z][a-z0-9_]*(?:\s+[a-z][a-z0-9_]*)*)\s*(?:\(([^)]*)\))?')


def normalize_kind(raw_type: Optional[str], db_type: DatabaseType) -> str:
    """
    Map a dialect type name to one of ``KINDS``.

    Already-normalised kind names pass through, so a spec can use either
    spelling. The dialect matters for a few spellings that mean different
    things in different places: ``tinyint(1)`` is MySQL's BOOLEAN while a
    plain ``tinyint`` is a small integer, and ``bit`` is a boolean on SQL
    Server and MySQL but a bit string wider than one bit elsewhere.
    """
    if not raw_type:
        return 'other'
    lowered = raw_type.strip().lower()
    if lowered in KINDS:
        return lowered

    match = _TYPE_SHAPE.match(lowered)
    if match is None:
        return 'other'
    base = ' '.join(match.group(1).split())
    args = (match.group(2) or '').strip()

    # MySQL appends modifiers after the length: ``int(11) unsigned``.
    for modifier in (' unsigned', ' zerofill'):
        if base.endswith(modifier):
            base = base[:-len(modifier)]

    if base == 'tinyint':
        return 'boolean' if args == '1' else 'integer'
    if base == 'bit':
        return 'boolean' if args in ('', '1') else 'other'

    kind = _KIND_BY_BASE.get(base)
    if kind is None:
        # "timestamp(6) with time zone" and "varchar(10) character set x"
        # carry their meaning in the first word.
        kind = _KIND_BY_BASE.get(base.split(' ')[0])
    return kind or 'other'


# What the translator collapses into each dialect's types. A column of the
# left-hand kind on that backend may have been declared as any of the
# canonical kinds on the right, so each is accepted as a match. Where the
# translator produces one specific shape (JSONB is NVARCHAR(MAX) on SQL
# Server, UUID is CHAR(36) on MySQL) the equivalence is limited to that
# shape, so a dict is not waved through for an NVARCHAR(255) name column.
# SQLite has only TEXT, so there every text column could have been any of
# them.
_COLLAPSED_KINDS: Dict[DatabaseType, Dict[str, FrozenSet[str]]] = {
    DatabaseType.SQLITE: {
        'integer': frozenset({'integer', 'boolean'}),
        'text': frozenset({'text', 'uuid', 'json', 'timestamp'}),
    },
    DatabaseType.POSTGRESQL: {},
}

_COLLAPSED_SHAPES: Dict[DatabaseType, Dict[str, str]] = {
    DatabaseType.MYSQL: {'char(36)': 'uuid'},
    DatabaseType.MSSQL: {'nvarchar(max)': 'json'},
}


def kinds_satisfied_by(actual_kind: str, db_type: DatabaseType,
                       raw_type: Optional[str] = None) -> FrozenSet[str]:
    """
    Canonical kinds a column of ``actual_kind`` may stand for on ``db_type``.

    ``raw_type`` is the dialect's declared type; it decides the equivalences
    that hinge on a specific shape rather than on the kind alone.
    """
    kinds = _COLLAPSED_KINDS.get(db_type, {}).get(actual_kind)
    if kinds is not None:
        return kinds
    shape = ' '.join((raw_type or '').lower().split())
    extra = _COLLAPSED_SHAPES.get(db_type, {}).get(shape)
    if extra is not None:
        return frozenset({actual_kind, extra})
    return frozenset({actual_kind})


def kind_matches(expected_kind: str, actual_kind: str, db_type: DatabaseType,
                 raw_type: Optional[str] = None) -> bool:
    return expected_kind in kinds_satisfied_by(actual_kind, db_type, raw_type)


# ---------------------------------------------------------------------------
# Identifier handling
# ---------------------------------------------------------------------------

def split_qualified_name(name: str) -> Tuple[Optional[str], str]:
    """
    ``schema.table`` into its parts, dropping ``"``, ``[]`` or backtick
    quoting. A bare table name gives ``(None, table)``.
    """
    parts = re.findall(r'"([^"]+)"|\[([^\]]+)\]|`([^`]+)`|([^.\s"\[\]`]+)', name.strip())
    names = [next(p for p in part if p) for part in parts]
    if not names:
        raise ValueError(f"not a table name: {name!r}")
    if len(names) == 1:
        return None, names[0]
    return names[-2], names[-1]


def _is_quoted(name: str) -> bool:
    name = name.strip()
    return name.startswith('"') and name.endswith('"')


def pg_resolve_name(name: str) -> Tuple[Optional[str], str]:
    """
    Schema and table as PostgreSQL stores them: unquoted identifiers fold
    to lower case, which is how the CREATE TABLE that made them was read.
    """
    schema, table = split_qualified_name(name)
    quoted = _is_quoted(name.split('.')[-1])
    if not quoted:
        table = table.lower()
        schema = schema.lower() if schema is not None else None
    return schema, table


# ---------------------------------------------------------------------------
# Catalog queries
#
# Each returns (sql, params) so the sync manager and the async backend can
# run the same statement. Column aliases are explicit and lower case so the
# rows look alike whatever the driver does with catalog column casing.
# ---------------------------------------------------------------------------

Query = Tuple[str, Dict[str, Any]]


def pg_tables_query() -> Query:
    return (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {},
    )


def _pg_schema_filter(alias: str, schema: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if schema is None:
        return f"{alias}.table_schema = current_schema()", {}
    return f"{alias}.table_schema = :schema", {'schema': schema}


def pg_columns_query(schema: Optional[str], table: str) -> Query:
    where, params = _pg_schema_filter('c', schema)
    params['table'] = table
    sql = f"""
        SELECT c.column_name AS name,
               c.data_type AS data_type,
               c.udt_name AS udt_name,
               c.character_maximum_length AS char_length,
               c.is_nullable AS is_nullable,
               c.column_default AS column_default,
               c.is_identity AS is_identity,
               pg_get_serial_sequence(
                   quote_ident(c.table_schema) || '.' || quote_ident(c.table_name),
                   c.column_name) IS NOT NULL AS has_sequence,
               EXISTS (
                   SELECT 1
                   FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu
                     ON kcu.constraint_name = tc.constraint_name
                    AND kcu.constraint_schema = tc.constraint_schema
                    AND kcu.table_name = tc.table_name
                   WHERE tc.constraint_type = 'PRIMARY KEY'
                     AND tc.table_schema = c.table_schema
                     AND tc.table_name = c.table_name
                     AND kcu.column_name = c.column_name
               ) AS is_primary_key
        FROM information_schema.columns c
        WHERE {where} AND c.table_name = :table
        ORDER BY c.ordinal_position
    """
    return sql, params


def pg_indexes_query(schema: Optional[str], table: str) -> Query:
    if schema is None:
        schema_filter, params = "n.nspname = current_schema()", {}
    else:
        schema_filter, params = "n.nspname = :schema", {'schema': schema}
    params['table'] = table
    # Expression indexes have no attribute behind a key position and so
    # drop out of the join; only column indexes are reported.
    sql = f"""
        SELECT ic.relname AS name,
               i.indisunique AS is_unique,
               i.indisprimary AS is_primary,
               array_agg(a.attname ORDER BY k.ord) AS columns
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class tc ON tc.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = tc.relnamespace
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE {schema_filter} AND tc.relname = :table
        GROUP BY ic.relname, i.indisunique, i.indisprimary
        ORDER BY ic.relname
    """
    return sql, params


def pg_table_info(name: str, column_rows: Iterable[Mapping[str, Any]],
                  index_rows: Iterable[Mapping[str, Any]]) -> TableInfo:
    """Assemble a TableInfo from the rows of the two PostgreSQL queries."""
    columns = []
    for row in column_rows:
        data_type = row['data_type']
        if data_type in ('ARRAY', 'USER-DEFINED'):
            raw = row['udt_name']
        else:
            raw = data_type
            if row.get('char_length') is not None:
                raw = f"{raw}({row['char_length']})"
        columns.append(ColumnInfo(
            name=row['name'],
            raw_type=raw,
            kind=normalize_kind(raw, DatabaseType.POSTGRESQL),
            nullable=row['is_nullable'] == 'YES',
            default=_text_or_none(row.get('column_default')),
            is_primary_key=bool(row['is_primary_key']),
            is_auto_increment=bool(row['has_sequence']) or row.get('is_identity') == 'YES',
        ))
    indexes = [
        IndexInfo(name=row['name'], columns=tuple(row['columns']),
                  unique=bool(row['is_unique']), primary=bool(row['is_primary']))
        for row in index_rows
    ]
    return TableInfo(name=name, columns=columns, indexes=indexes)


def mysql_tables_query() -> Query:
    return (
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {},
    )


def _mysql_schema_filter(schema: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if schema is None:
        return "table_schema = DATABASE()", {}
    return "table_schema = :schema", {'schema': schema}


def mysql_columns_query(schema: Optional[str], table: str) -> Query:
    where, params = _mysql_schema_filter(schema)
    params['table'] = table
    sql = f"""
        SELECT column_name AS name,
               column_type AS raw_type,
               is_nullable AS is_nullable,
               column_default AS column_default,
               column_key AS column_key,
               extra AS extra
        FROM information_schema.columns
        WHERE {where} AND table_name = :table
        ORDER BY ordinal_position
    """
    return sql, params


def mysql_indexes_query(schema: Optional[str], table: str) -> Query:
    where, params = _mysql_schema_filter(schema)
    params['table'] = table
    sql = f"""
        SELECT index_name AS name,
               column_name AS column_name,
               non_unique AS non_unique,
               seq_in_index AS seq_in_index
        FROM information_schema.statistics
        WHERE {where} AND table_name = :table
        ORDER BY index_name, seq_in_index
    """
    return sql, params


def mysql_table_info(name: str, column_rows: Iterable[Mapping[str, Any]],
                     index_rows: Iterable[Mapping[str, Any]]) -> TableInfo:
    columns = []
    for row in column_rows:
        raw = _as_text(row['raw_type'])
        extra = _as_text(row.get('extra')) or ''
        columns.append(ColumnInfo(
            name=_as_text(row['name']),
            raw_type=raw,
            kind=normalize_kind(raw, DatabaseType.MYSQL),
            nullable=_as_text(row['is_nullable']).upper() == 'YES',
            default=_text_or_none(row.get('column_default')),
            is_primary_key=_as_text(row.get('column_key')).upper() == 'PRI',
            is_auto_increment='auto_increment' in extra.lower(),
        ))
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in index_rows:
        index_name = _as_text(row['name'])
        entry = grouped.setdefault(index_name, {'columns': [], 'unique': True})
        entry['columns'].append((int(row['seq_in_index']), _as_text(row['column_name'])))
        entry['unique'] = entry['unique'] and not int(row['non_unique'])
    indexes = [
        IndexInfo(name=index_name,
                  columns=tuple(col for _, col in sorted(entry['columns'])),
                  unique=entry['unique'],
                  primary=index_name.upper() == 'PRIMARY')
        for index_name, entry in grouped.items()
    ]
    return TableInfo(name=name, columns=columns, indexes=indexes)


def mssql_tables_query() -> Query:
    return (
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = SCHEMA_NAME() AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        {},
    )


def _mssql_schema_filter(alias: str, schema: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if schema is None:
        return f"{alias}.table_schema = SCHEMA_NAME()", {}
    return f"{alias}.table_schema = :schema", {'schema': schema}


def mssql_columns_query(schema: Optional[str], table: str) -> Query:
    where, params = _mssql_schema_filter('c', schema)
    params['table'] = table
    # SQL Server has no boolean expressions in a select list, hence CASE.
    sql = f"""
        SELECT c.column_name AS name,
               c.data_type AS data_type,
               c.character_maximum_length AS char_length,
               c.numeric_precision AS numeric_precision,
               c.numeric_scale AS numeric_scale,
               c.is_nullable AS is_nullable,
               c.column_default AS column_default,
               COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.table_schema) + '.' + QUOTENAME(c.table_name)),
                              c.column_name, 'IsIdentity') AS is_identity,
               CASE WHEN EXISTS (
                   SELECT 1
                   FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu
                     ON kcu.constraint_name = tc.constraint_name
                    AND kcu.constraint_schema = tc.constraint_schema
                    AND kcu.table_name = tc.table_name
                   WHERE tc.constraint_type = 'PRIMARY KEY'
                     AND tc.table_schema = c.table_schema
                     AND tc.table_name = c.table_name
                     AND kcu.column_name = c.column_name
               ) THEN 1 ELSE 0 END AS is_primary_key
        FROM information_schema.columns c
        WHERE {where} AND c.table_name = :table
        ORDER BY c.ordinal_position
    """
    return sql, params


def mssql_indexes_query(schema: Optional[str], table: str) -> Query:
    qualified = f"{schema}.{table}" if schema else table
    sql = """
        SELECT i.name AS name,
               i.is_unique AS is_unique,
               i.is_primary_key AS is_primary,
               col.name AS column_name,
               ic.key_ordinal AS key_ordinal
        FROM sys.indexes i
        JOIN sys.index_columns ic
          ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns col
          ON col.object_id = ic.object_id AND col.column_id = ic.column_id
        WHERE i.object_id = OBJECT_ID(:qualified)
          AND i.type > 0
          AND i.is_hypothetical = 0
          AND ic.is_included_column = 0
        ORDER BY i.name, ic.key_ordinal
    """
    return sql, {'qualified': qualified}


def mssql_table_info(name: str, column_rows: Iterable[Mapping[str, Any]],
                     index_rows: Iterable[Mapping[str, Any]]) -> TableInfo:
    columns = []
    for row in column_rows:
        raw = row['data_type']
        length = row.get('char_length')
        if length is not None:
            raw = f"{raw}({'max' if length == -1 else length})"
        elif raw.lower() in ('decimal', 'numeric') and row.get('numeric_precision') is not None:
            raw = f"{raw}({row['numeric_precision']},{row.get('numeric_scale') or 0})"
        columns.append(ColumnInfo(
            name=row['name'],
            raw_type=raw,
            kind=normalize_kind(raw, DatabaseType.MSSQL),
            nullable=str(row['is_nullable']).upper() == 'YES',
            default=_text_or_none(row.get('column_default')),
            is_primary_key=bool(row['is_primary_key']),
            is_auto_increment=bool(row.get('is_identity')),
        ))
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in index_rows:
        entry = grouped.setdefault(row['name'], {
            'columns': [], 'unique': bool(row['is_unique']),
            'primary': bool(row['is_primary']),
        })
        entry['columns'].append((int(row['key_ordinal']), row['column_name']))
    indexes = [
        IndexInfo(name=index_name,
                  columns=tuple(col for _, col in sorted(entry['columns'])),
                  unique=entry['unique'], primary=entry['primary'])
        for index_name, entry in grouped.items()
    ]
    return TableInfo(name=name, columns=columns, indexes=indexes)


def sqlite_tables_query() -> Query:
    return (
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        {},
    )


def sqlite_columns_query(table: str) -> Query:
    return (
        'SELECT name, type AS raw_type, "notnull" AS not_null, '
        'dflt_value AS column_default, pk AS pk '
        'FROM pragma_table_info(:table) ORDER BY cid',
        {'table': table},
    )


def sqlite_index_list_query(table: str) -> Query:
    return (
        'SELECT name, "unique" AS is_unique, origin AS origin '
        'FROM pragma_index_list(:table) ORDER BY seq',
        {'table': table},
    )


def sqlite_index_columns_query(index: str) -> Query:
    return (
        'SELECT name FROM pragma_index_info(:index) ORDER BY seqno',
        {'index': index},
    )


def sqlite_table_info(name: str, column_rows: Iterable[Mapping[str, Any]],
                      index_rows: Iterable[Tuple[Mapping[str, Any], List[Mapping[str, Any]]]]) -> TableInfo:
    """
    Assemble a TableInfo from pragma rows.

    ``index_rows`` pairs each ``pragma_index_list`` row with its
    ``pragma_index_info`` rows, since SQLite answers one index at a time.
    """
    column_rows = list(column_rows)
    pk_count = sum(1 for row in column_rows if int(row['pk'] or 0) > 0)
    columns = []
    for row in column_rows:
        raw = row['raw_type'] or ''
        is_pk = int(row['pk'] or 0) > 0
        # A lone INTEGER PRIMARY KEY is the rowid alias, which is what the
        # translator makes of SERIAL PRIMARY KEY; it assigns its own values.
        rowid_alias = is_pk and pk_count == 1 and raw.strip().upper() == 'INTEGER'
        columns.append(ColumnInfo(
            name=row['name'],
            raw_type=raw,
            kind=normalize_kind(raw, DatabaseType.SQLITE),
            # pragma reports a PRIMARY KEY column without NOT NULL as
            # nullable; the canonical dialect never has a nullable key, so
            # keys are reported the way PostgreSQL would.
            nullable=not (int(row['not_null'] or 0) or is_pk),
            default=_text_or_none(row.get('column_default')),
            is_primary_key=is_pk,
            is_auto_increment=rowid_alias,
        ))
    indexes = []
    for index_row, info_rows in index_rows:
        cols = tuple(r['name'] for r in info_rows if r.get('name'))
        if not cols:
            continue
        indexes.append(IndexInfo(
            name=index_row['name'], columns=cols,
            unique=bool(int(index_row['is_unique'] or 0)),
            primary=(index_row.get('origin') == 'pk'),
        ))
    return TableInfo(name=name, columns=columns, indexes=indexes)


def _as_text(value: Any) -> str:
    """pymysql hands some information_schema values back as bytes."""
    if value is None:
        return ''
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    return str(value)


def _text_or_none(value: Any) -> Optional[str]:
    return None if value is None else _as_text(value)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _index_candidates(info: TableInfo) -> List[Tuple[Tuple[str, ...], bool]]:
    """Column tuples (lower-cased) that can satisfy an index expectation."""
    candidates = [(tuple(c.lower() for c in ix.columns), ix.unique) for ix in info.indexes]
    pk = tuple(c.lower() for c in info.primary_key)
    if pk:
        candidates.append((pk, True))
    return candidates


def validate_table(spec: TableSpec, info: TableInfo, db_type: DatabaseType,
                   strict: bool, report: ValidationReport) -> None:
    """Compare one table against its spec, appending findings to ``report``."""
    table = spec.name
    expected_names = set()
    for col_spec in spec.columns:
        expected_names.add(col_spec.name.lower())
        col = info.column(col_spec.name)
        where = f"{table}.{col_spec.name}"
        if col is None:
            report.missing_columns.append((table, col_spec.name))
            report.errors.append(f"{where}: missing column")
            continue

        if not kind_matches(col_spec.kind, col.kind, db_type, col.raw_type):
            report.errors.append(
                f"{where}: expected kind {col_spec.kind}, found {col.kind} "
                f"(declared {col.raw_type})"
            )

        expect_nullable = col_spec.nullable and not col_spec.primary_key
        if expect_nullable != col.nullable:
            report.errors.append(
                f"{where}: expected {'nullable' if expect_nullable else 'NOT NULL'}, "
                f"found {'nullable' if col.nullable else 'NOT NULL'}"
            )

        if col_spec.primary_key and not col.is_primary_key:
            report.errors.append(f"{where}: expected a primary key column")

        if col_spec.auto_increment is not None and col_spec.auto_increment != col.is_auto_increment:
            report.errors.append(
                f"{where}: expected {'an' if col_spec.auto_increment else 'no'} "
                f"auto-increment column, found "
                f"{'one' if col.is_auto_increment else 'none'}"
            )

    for col in info.columns:
        if col.name.lower() not in expected_names:
            report.unexpected_columns.append((table, col.name))
            message = f"{table}.{col.name}: unexpected column ({col.raw_type})"
            if strict:
                report.errors.append(message)
            else:
                report.warnings.append(message)

    candidates = _index_candidates(info)
    for ix_spec in spec.indexes:
        wanted = tuple(c.lower() for c in ix_spec.columns)
        found = any(cols == wanted and (unique or not ix_spec.unique)
                    for cols, unique in candidates)
        if not found:
            report.missing_indexes.append((table, tuple(ix_spec.columns)))
            label = 'unique index' if ix_spec.unique else 'index'
            report.errors.append(
                f"{table}: missing {label} on ({', '.join(ix_spec.columns)})"
            )


def build_report(specs: Sequence[TableSpec], tables: Mapping[str, Optional[TableInfo]],
                 db_type: DatabaseType, strict: bool = False) -> ValidationReport:
    """
    Compare specs with introspected tables.

    ``tables`` maps each spec's name to its TableInfo, or None when the
    table is absent. Keeping this pure lets the sync manager and the async
    backend gather the tables their own way and share the comparison.
    """
    report = ValidationReport(tables_checked=len(specs))
    for spec in specs:
        info = tables.get(spec.name)
        if info is None:
            report.missing_tables.append(spec.name)
            report.errors.append(f"{spec.name}: missing table")
            continue
        validate_table(spec, info, db_type, strict, report)
    return report


# ---------------------------------------------------------------------------
# Parameter checking
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_json(value: Any) -> bool:
    return isinstance(value, (dict, list, str))


_ACCEPTS: Dict[str, Callable[[Any], bool]] = {
    'integer': _is_int,
    'bigint': _is_int,
    'float': lambda v: _is_int(v) or isinstance(v, (float, decimal.Decimal)),
    'decimal': lambda v: _is_int(v) or isinstance(v, (float, decimal.Decimal)),
    'text': lambda v: isinstance(v, str),
    'boolean': lambda v: isinstance(v, bool) or v in (0, 1),
    'timestamp': lambda v: isinstance(v, (_dt.datetime, str)),
    'date': lambda v: isinstance(v, (_dt.date, str)),
    'json': _is_json,
    'uuid': lambda v: isinstance(v, (_uuid.UUID, str)),
    'bytes': lambda v: isinstance(v, (bytes, bytearray, memoryview)),
    'other': lambda v: True,
}


def value_fits(value: Any, column: ColumnInfo, db_type: DatabaseType) -> bool:
    """
    True if ``value`` is a plausible Python value for ``column``.

    The check is by kind, through the translator's equivalences: a bool is
    fine for an integer column on SQLite because BOOLEAN became INTEGER
    there, but not on PostgreSQL where the driver would reject it. Strings
    are accepted for timestamps, dates, uuids and json because callers
    routinely pass them pre-serialised.
    """
    if value is None:
        return column.nullable
    return any(_ACCEPTS[kind](value)
               for kind in kinds_satisfied_by(column.kind, db_type, column.raw_type))


def check_params_against(table: str, info: TableInfo, params: Mapping[str, Any],
                         db_type: DatabaseType) -> None:
    """Raise SchemaValidationError listing every key that does not fit."""
    errors: List[str] = []
    keys: List[str] = []
    for key, value in params.items():
        column = info.column(key)
        if column is None:
            errors.append(f"{key}: no such column")
            keys.append(key)
        elif not value_fits(value, column, db_type):
            if value is None:
                errors.append(f"{key}: None for NOT NULL column")
            else:
                errors.append(
                    f"{key}: {type(value).__name__} value for {column.kind} column "
                    f"({column.raw_type})"
                )
            keys.append(key)
    if errors:
        raise SchemaValidationError(table, errors, keys)


# ---------------------------------------------------------------------------
# Sync inspector
# ---------------------------------------------------------------------------

class SchemaInspector:
    """
    Runs the catalog queries through a ``DatabaseManager`` and caches the
    result per table for ``check_params``.

    The manager's own methods delegate here; it is a separate object so the
    manager picks up one attribute rather than a page of catalog code.
    """

    def __init__(self, db: 'DatabaseManager'):
        self._db = db
        self._cache: Dict[str, TableInfo] = {}

    @property
    def db_type(self) -> DatabaseType:
        return self._db.config.database_type

    def _run(self, query: Query) -> List[Dict[str, Any]]:
        sql, params = query
        return self._db.execute(sql, params or None)

    def list_tables(self) -> List[str]:
        db_type = self.db_type
        if db_type == DatabaseType.POSTGRESQL:
            rows = self._run(pg_tables_query())
        elif db_type == DatabaseType.MYSQL:
            rows = self._run(mysql_tables_query())
        elif db_type == DatabaseType.MSSQL:
            rows = self._run(mssql_tables_query())
        elif db_type == DatabaseType.SQLITE:
            rows = self._run(sqlite_tables_query())
        else:
            raise NotImplementedError(f"list_tables is not supported on {db_type.value}")
        return [_as_text(next(iter(row.values()))) for row in rows]

    def introspect(self, table: str) -> TableInfo:
        """Query the catalog for ``table`` and refresh its cache entry."""
        db_type = self.db_type
        if db_type == DatabaseType.POSTGRESQL:
            schema, name = pg_resolve_name(table)
            columns = self._run(pg_columns_query(schema, name))
            if not columns:
                raise TableNotFoundError(f"table {table} does not exist")
            info = pg_table_info(table, columns, self._run(pg_indexes_query(schema, name)))

        elif db_type == DatabaseType.MYSQL:
            schema, name = split_qualified_name(table)
            columns = self._run(mysql_columns_query(schema, name))
            if not columns:
                raise TableNotFoundError(f"table {table} does not exist")
            info = mysql_table_info(table, columns, self._run(mysql_indexes_query(schema, name)))

        elif db_type == DatabaseType.MSSQL:
            schema, name = split_qualified_name(table)
            columns = self._run(mssql_columns_query(schema, name))
            if not columns:
                raise TableNotFoundError(f"table {table} does not exist")
            info = mssql_table_info(table, columns, self._run(mssql_indexes_query(schema, name)))

        elif db_type == DatabaseType.SQLITE:
            _, name = split_qualified_name(table)
            columns = self._run(sqlite_columns_query(name))
            if not columns:
                raise TableNotFoundError(f"table {table} does not exist")
            index_rows = [
                (row, self._run(sqlite_index_columns_query(row['name'])))
                for row in self._run(sqlite_index_list_query(name))
            ]
            info = sqlite_table_info(table, columns, index_rows)

        else:
            raise NotImplementedError(f"introspect is not supported on {db_type.value}")

        self._cache[table] = info
        return info

    def cached(self, table: str) -> TableInfo:
        info = self._cache.get(table)
        if info is None:
            info = self.introspect(table)
        return info

    def invalidate(self, table: Optional[str] = None) -> None:
        if table is None:
            self._cache.clear()
        else:
            self._cache.pop(table, None)

    def validate_schema(self, expected: ExpectedSchema, strict: bool = False) -> ValidationReport:
        specs = coerce_specs(expected)
        tables: Dict[str, Optional[TableInfo]] = {}
        for spec in specs:
            try:
                tables[spec.name] = self.introspect(spec.name)
            except TableNotFoundError:
                tables[spec.name] = None
        return build_report(specs, tables, self.db_type, strict=strict)

    def check_params(self, table: str, params: Mapping[str, Any]) -> None:
        check_params_against(table, self.cached(table), params, self.db_type)


__all__ = [
    'KINDS',
    'ColumnInfo', 'IndexInfo', 'TableInfo',
    'ColumnSpec', 'IndexSpec', 'TableSpec',
    'ValidationReport', 'SchemaValidationError', 'TableNotFoundError',
    'SchemaInspector',
    'normalize_kind', 'kinds_satisfied_by', 'kind_matches',
    'coerce_specs', 'build_report', 'validate_table',
    'value_fits', 'check_params_against',
    'split_qualified_name', 'pg_resolve_name',
    'pg_tables_query', 'pg_columns_query', 'pg_indexes_query', 'pg_table_info',
    'mysql_tables_query', 'mysql_columns_query', 'mysql_indexes_query', 'mysql_table_info',
    'mssql_tables_query', 'mssql_columns_query', 'mssql_indexes_query', 'mssql_table_info',
    'sqlite_tables_query', 'sqlite_columns_query', 'sqlite_index_list_query',
    'sqlite_index_columns_query', 'sqlite_table_info',
]
