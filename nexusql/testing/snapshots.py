"""
Capture tables, put them back, or report what changed since.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Tuple, Union

from ..interfaces import DatabaseType
from .cleanup import mssql_has_identity, truncate

if TYPE_CHECKING:
    from ..manager import DatabaseManager

Row = Dict[str, Any]


@dataclass
class TableDiff:
    """
    Rows of one table that differ from a snapshot, keyed by primary key.

    ``changed`` maps a key to ``(before, after)``. Falsy when nothing
    differs, so ``assert not diff["users"]`` reads naturally.
    """
    added: Dict[Any, Row] = field(default_factory=dict)
    removed: Dict[Any, Row] = field(default_factory=dict)
    changed: Dict[Any, Tuple[Row, Row]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


class Snapshot:
    """
    In-memory copy of some tables, taken by :func:`snapshot`.

    ``tables`` keeps the order the caller gave, which must be insert order
    (parents before children); ``rows[table]`` holds that table's rows as
    dicts; ``pk[table]`` names its primary-key column.
    """

    def __init__(self, tables: List[str], rows: Dict[str, List[Row]],
                 pk: Dict[str, str]) -> None:
        self.tables = list(tables)
        self.rows = rows
        self.pk = pk

    def restore(self, db: 'DatabaseManager', reset_identity: bool = True) -> None:
        """
        Make the tables hold exactly the snapshotted rows again.

        Truncates them (children first) and re-inserts in the given order,
        keys included, inside one transaction (joining the caller's if one
        is open). The generated-key counter is then moved past the highest
        restored key where the database does not do that itself: PostgreSQL
        sequences are set with ``setval``; SQL Server identity columns are
        written under ``IDENTITY_INSERT`` and re-seed on their own; SQLite
        and MySQL track the maximum automatically.
        """
        dialect = db.config.database_type
        with db.transaction():
            truncate(db, self.tables, reset_identity=reset_identity)
            for table in self.tables:
                rows = self.rows[table]
                if not rows:
                    continue
                identity_insert = (dialect is DatabaseType.MSSQL
                                   and mssql_has_identity(db, table))
                if identity_insert:
                    db.execute(f"SET IDENTITY_INSERT {table} ON")
                for row in rows:
                    columns = list(row)
                    db.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) "
                        f"VALUES ({', '.join(':' + c for c in columns)})",
                        row)
                if identity_insert:
                    db.execute(f"SET IDENTITY_INSERT {table} OFF")
                if dialect is DatabaseType.POSTGRESQL:
                    _postgres_resync_sequence(db, table, self.pk[table])

    def diff(self, db: 'DatabaseManager') -> Dict[str, TableDiff]:
        """
        Compare the live tables with the snapshot.

        Returns one :class:`TableDiff` per table. A row is matched by
        primary key, so a changed row shows up under ``changed`` rather than
        as one removal and one addition.
        """
        result: Dict[str, TableDiff] = {}
        for table in self.tables:
            pk = self.pk[table]
            before = _by_key(self.rows[table], table, pk)
            after = _by_key(_read(db, table), table, pk)
            diff = TableDiff()
            for key, row in after.items():
                if key not in before:
                    diff.added[key] = row
                elif before[key] != row:
                    diff.changed[key] = (before[key], row)
            for key, row in before.items():
                if key not in after:
                    diff.removed[key] = row
            result[table] = diff
        return result

    def __repr__(self) -> str:
        counts = {t: len(self.rows[t]) for t in self.tables}
        return f"Snapshot({counts})"


def snapshot(db: 'DatabaseManager', tables: Union[str, Iterable[str]],
             pk: Union[str, Mapping[str, str]] = "id") -> Snapshot:
    """
    Read ``tables`` into memory.

    ``tables`` is given in insert order (parents first). ``pk`` is the
    primary-key column used to match rows in ``diff``: one name for every
    table, or a mapping from table to column for tables that differ. It is
    only consulted when the table has rows, so the snapshot does not fail on
    tables without that column, and the error names the table when it does.
    """
    names = [tables] if isinstance(tables, str) else list(tables)
    if isinstance(pk, str):
        keys = {t: pk for t in names}
    else:
        keys = {t: pk.get(t, "id") for t in names}
    rows = {t: _read(db, t) for t in names}
    return Snapshot(names, rows, keys)


def _read(db: 'DatabaseManager', table: str) -> List[Row]:
    return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]


def _by_key(rows: List[Row], table: str, pk: str) -> Dict[Any, Row]:
    keyed: Dict[Any, Row] = {}
    for row in rows:
        if pk not in row:
            raise KeyError(
                f"table {table!r} has no column {pk!r}; pass pk={{{table!r}: '<column>'}} "
                "to snapshot()")
        keyed[row[pk]] = row
    return keyed


def _postgres_resync_sequence(db: 'DatabaseManager', table: str, pk: str) -> None:
    """
    Point the key's sequence past the restored rows.

    ``TRUNCATE .. RESTART IDENTITY`` left it at 1, and the rows were inserted
    with explicit keys, so without this the next generated key would collide
    with a restored row. Tables whose key has no sequence (UUID defaults,
    natural keys) are left alone.
    """
    found = db.fetch_one(
        "SELECT pg_get_serial_sequence(:table, :column) AS seq",
        {"table": table, "column": pk})
    sequence = found["seq"] if found else None
    if not sequence:
        return
    db.execute(
        f"SELECT setval(:seq, COALESCE(MAX({pk}), 1), MAX({pk}) IS NOT NULL) FROM {table}",
        {"seq": sequence})
