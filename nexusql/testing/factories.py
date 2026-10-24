"""
Data factories: build or insert rows from a declared shape plus overrides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

from ..interfaces import DatabaseType

if TYPE_CHECKING:
    from ..manager import DatabaseManager


class Sequence:
    """
    Default that produces a fresh value for every row.

    Takes a format string with an ``{n}`` placeholder, or a callable given
    ``n``. ``n`` counts from ``start`` and advances each time a row that uses
    this default is built, so two rows from the same factory never collide on
    a UNIQUE column:

        email = Sequence("user{n}@example.com")   # user1@..., user2@...
        code = Sequence(lambda n: f"C{n:04d}")    # C0001, C0002, ...
    """

    def __init__(self, template: Union[str, Callable[[int], Any]] = "{n}",
                 start: int = 1) -> None:
        self._template = template
        self._start = start
        self._next = start

    def next(self) -> Any:
        """Return the next value and advance the counter."""
        n = self._next
        self._next += 1
        if callable(self._template):
            return self._template(n)
        return self._template.format(n=n)

    def reset(self) -> None:
        """Start counting from ``start`` again."""
        self._next = self._start

    def __repr__(self) -> str:
        return f"Sequence({self._template!r}, start={self._start})"


class SubFactory:
    """
    Default for a foreign-key column: create the parent row, use its key.

    ``create()`` on the owning factory creates one parent through ``factory``
    (with ``overrides`` applied) and stores the parent's primary key, or the
    column named by ``key``, in the child row. ``build()`` has no database to
    create the parent in, so there is no key to reference and the column is
    left as None; pass the foreign key explicitly when building.
    """

    def __init__(self, factory: 'Factory', key: Optional[str] = None,
                 **overrides: Any) -> None:
        self.factory = factory
        self.key = key or factory.pk
        self.overrides = overrides

    def resolve(self, db: Optional['DatabaseManager']) -> Any:
        if db is None:
            return None
        return self.factory.create(db, **self.overrides)[self.key]

    def __repr__(self) -> str:
        return f"SubFactory({self.factory.table!r}, key={self.key!r})"


class Factory:
    """
    Row shape for one table: a column-to-default mapping.

    A default is used as-is unless it is a :class:`Sequence` (advanced per
    row), a :class:`SubFactory` (parent row created on ``create``), or any
    other callable (called with no arguments per row, so ``datetime.now``
    or ``uuid.uuid4`` give each row its own value). Overrides passed to
    ``build`` or ``create`` are resolved the same way and replace the
    default, in which case the default is not evaluated: an overridden
    Sequence does not advance.

        users = Factory("users", email=Sequence("user{n}@example.com"),
                        name="Test User", created_at=datetime.now)
        row = users.create(db, name="Alice")   # {'email': ..., 'id': 1, ...}

    ``pk`` names the generated primary key, read back through
    ``execute(..., return_id=True)`` after the insert. A row that already
    carries a non-None value for that column is inserted with it instead.
    Columns that collide with the ``table`` and ``pk`` parameter names can
    be added through the ``defaults`` dict after construction.
    """

    def __init__(self, table: str, pk: str = "id", **defaults: Any) -> None:
        self.table = table
        self.pk = pk
        self.defaults: Dict[str, Any] = dict(defaults)

    def build(self, **overrides: Any) -> Dict[str, Any]:
        """Resolve defaults and overrides into a row dict without inserting it."""
        return self._resolve(overrides, None)

    def create(self, db: 'DatabaseManager', **overrides: Any) -> Dict[str, Any]:
        """Insert one row and return it, including the generated primary key."""
        row = self._resolve(overrides, db)
        columns = [c for c in row if not (c == self.pk and row[c] is None)]
        params = {c: row[c] for c in columns}

        if columns:
            sql = (f"INSERT INTO {self.table} ({', '.join(columns)}) "
                   f"VALUES ({', '.join(':' + c for c in columns)})")
        elif db.config.database_type is DatabaseType.MYSQL:
            # MySQL has no DEFAULT VALUES clause; an empty list is its idiom.
            sql = f"INSERT INTO {self.table} () VALUES ()"
        else:
            sql = f"INSERT INTO {self.table} DEFAULT VALUES"

        if self.pk in columns:
            db.execute(sql, params)
        else:
            row[self.pk] = db.execute(sql, params, return_id=True)
        return row

    def create_batch(self, db: 'DatabaseManager', n: int,
                     **overrides: Any) -> List[Dict[str, Any]]:
        """Insert ``n`` rows; sequences and callables give each its own values."""
        return [self.create(db, **overrides) for _ in range(n)]

    def _resolve(self, overrides: Dict[str, Any],
                 db: Optional['DatabaseManager']) -> Dict[str, Any]:
        merged = dict(self.defaults)
        merged.update(overrides)
        return {column: _resolve_value(value, db) for column, value in merged.items()}

    def __repr__(self) -> str:
        return f"Factory({self.table!r}, pk={self.pk!r}, columns={list(self.defaults)})"


def _resolve_value(value: Any, db: Optional['DatabaseManager']) -> Any:
    if isinstance(value, Sequence):
        return value.next()
    if isinstance(value, SubFactory):
        return value.resolve(db)
    if callable(value):
        return value()
    return value
