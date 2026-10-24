"""
A small, type-safe query builder: canonical SQL with ``:name`` parameters.

The builder emits the same PostgreSQL-flavoured SQL a caller would write by
hand for ``DatabaseManager.execute``, so the manager's translation and
parameter conversion apply unchanged and the builder itself knows nothing
about dialects. It exists for the queries that are assembled from optional
pieces (search filters, admin listings, sort orders chosen by the client),
where string concatenation is where injection bugs come from.

Two rules make it safe to hand user input to:

- Python values never reach the SQL text. Every literal becomes a ``:pN``
  parameter, so ``Q.col("name") == user_input`` is always ``name = :p1``.
- Identifiers (tables, columns, aliases) must match
  ``^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)?$`` and are rejected
  otherwise. ``Q.raw()`` is the one way to put arbitrary text into a query
  and is for SQL the author wrote, never for input.

Statements and expressions are immutable: each chained call returns a new
object, so a base query can be shared and specialised without copying.

    q = (Q.select("users").columns("id", "email")
         .where(Q.col("age") >= Q.p("min_age", 18))
         .order_by("-created_at").limit(10).offset(20))
    sql, params = q.build()
    rows = db.run(q)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Union

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .manager import DatabaseManager

__all__ = [
    "Q",
    "QueryError",
    "Expr",
    "Col",
    "Param",
    "Raw",
    "Statement",
    "Select",
    "Insert",
    "Update",
    "Delete",
]


class QueryError(ValueError):
    """A statement cannot be built: bad identifier, missing clause, or a
    parameter named twice with different values."""


# A bare or once-dotted identifier. Everything that will appear unquoted in
# the SQL text goes through this check, so the translator regexes downstream
# see plain words and nothing an attacker typed can close a quote or end a
# statement.
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_JOIN_KINDS = {
    "inner": "INNER JOIN",
    "left": "LEFT JOIN",
    "right": "RIGHT JOIN",
    "full": "FULL JOIN",
    "cross": "CROSS JOIN",
}


def _check_identifier(name: Any, what: str = "identifier", dotted: bool = True) -> str:
    if not isinstance(name, str):
        raise QueryError(f"{what} must be a string, got {type(name).__name__}")
    pattern = IDENTIFIER if dotted else _SIMPLE
    if not pattern.match(name):
        allowed = "letters, digits and underscores" + (
            ", with at most one dot" if dotted else ""
        )
        raise QueryError(
            f"invalid {what} {name!r}: only {allowed} are accepted "
            "(quotes, spaces and punctuation are rejected rather than escaped)"
        )
    return name


def _render_identifier(name: str, quote: bool) -> str:
    """Quote a validated identifier only on request.

    Unquoted is the default because the manager's translation regexes match
    plain words; quoting exists for tables created with case-sensitive names.
    """
    if not quote:
        return name
    return ".".join(f'"{part}"' for part in name.split("."))


class _Ctx:
    """Parameter sink for one build().

    Auto-named parameters are numbered in the order they appear in the SQL
    text, skipping every name an explicit ``Q.p()`` takes anywhere in the
    statement, so the two styles mix without collisions. build() renders
    twice: a probing pass that only collects the explicit names, then the
    real pass with those names reserved. Rendering is pure, so the second
    pass costs nothing but time.
    """

    def __init__(self, reserved: frozenset[str] = frozenset(), probing: bool = False) -> None:
        self.params: dict[str, Any] = {}
        self.explicit: set[str] = set()
        self._reserved = reserved
        self._probing = probing
        self._next = 1

    def add(self, name: Optional[str], value: Any) -> str:
        if name is None:
            if self._probing:
                return ":?"
            while True:
                name = f"p{self._next}"
                self._next += 1
                if name not in self.params and name not in self._reserved:
                    break
        else:
            self.explicit.add(name)
            if name in self.params and self.params[name] is not value \
                    and self.params[name] != value:
                raise QueryError(
                    f"parameter :{name} is given two different values "
                    f"({self.params[name]!r} and {value!r})"
                )
        self.params[name] = value
        return f":{name}"


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class Expr:
    """Base of every SQL fragment the builder can render."""

    def _render(self, ctx: _Ctx) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def __bool__(self) -> bool:
        # ``a and b`` on two expressions would silently keep only ``b``;
        # fail loudly so the caller reaches for & | ~ instead.
        raise TypeError(
            "SQL expressions cannot be used as booleans; combine them with "
            "&, | and ~ (and compare with `is None`, not truthiness)"
        )

    # Boolean combinators
    def __and__(self, other: Expr) -> Expr:
        return BoolOp("AND", (self, _expr(other)))

    def __or__(self, other: Expr) -> Expr:
        return BoolOp("OR", (self, _expr(other)))

    def __invert__(self) -> Expr:
        return Not(self)

    # Comparisons. ``== None`` reads as IS NULL since ``= NULL`` is never
    # what anyone means.
    def __eq__(self, other: Any) -> Expr:  # type: ignore[override]
        if other is None:
            return self.is_null()
        return BinOp(self, "=", _operand(other))

    def __ne__(self, other: Any) -> Expr:  # type: ignore[override]
        if other is None:
            return self.is_not_null()
        return BinOp(self, "<>", _operand(other))

    def __lt__(self, other: Any) -> Expr:
        return BinOp(self, "<", _operand(other))

    def __le__(self, other: Any) -> Expr:
        return BinOp(self, "<=", _operand(other))

    def __gt__(self, other: Any) -> Expr:
        return BinOp(self, ">", _operand(other))

    def __ge__(self, other: Any) -> Expr:
        return BinOp(self, ">=", _operand(other))

    # Arithmetic, for ``SET views = views + 1`` and the like
    def __add__(self, other: Any) -> Expr:
        return BinOp(self, "+", _operand(other))

    def __sub__(self, other: Any) -> Expr:
        return BinOp(self, "-", _operand(other))

    def __mul__(self, other: Any) -> Expr:
        return BinOp(self, "*", _operand(other))

    def __truediv__(self, other: Any) -> Expr:
        return BinOp(self, "/", _operand(other))

    # Predicates
    def like(self, pattern: Any) -> Expr:
        return BinOp(self, "LIKE", _operand(pattern))

    def not_like(self, pattern: Any) -> Expr:
        return BinOp(self, "NOT LIKE", _operand(pattern))

    def ilike(self, pattern: Any) -> Expr:
        """Case-insensitive LIKE.

        Rendered as ``LOWER(x) LIKE LOWER(pattern)`` rather than ``ILIKE``:
        ILIKE is PostgreSQL-only and the translator does not rewrite it,
        while LOWER/LIKE run everywhere.
        """
        return Func("LOWER", (self,)).like(Func("LOWER", (_operand(pattern),)))

    def in_(self, values: Iterable[Any]) -> Expr:
        return InList(self, tuple(values), negate=False)

    def not_in(self, values: Iterable[Any]) -> Expr:
        return InList(self, tuple(values), negate=True)

    def is_null(self) -> Expr:
        return Postfix(self, "IS NULL")

    def is_not_null(self) -> Expr:
        return Postfix(self, "IS NOT NULL")

    def between(self, low: Any, high: Any) -> Expr:
        return Between(self, _operand(low), _operand(high))

    # Projection and ordering helpers
    def as_(self, alias: str) -> Expr:
        return Aliased(self, _check_identifier(alias, "alias", dotted=False))

    def asc(self) -> Ordering:
        return Ordering(self, "ASC")

    def desc(self) -> Ordering:
        return Ordering(self, "DESC")


@dataclass(frozen=True, eq=False)
class Col(Expr):
    """A column (or table) reference, validated on creation.

    ``quote=True`` emits ``"Name"`` for objects created with case-sensitive
    names; the default is unquoted so translation still sees plain words.
    """

    name: str
    quote: bool = False

    def __post_init__(self) -> None:
        _check_identifier(self.name, "identifier")

    def _render(self, ctx: _Ctx) -> str:
        return _render_identifier(self.name, self.quote)


@dataclass(frozen=True, eq=False)
class Param(Expr):
    """A bound value. ``name=None`` gets a ``pN`` name at build time."""

    name: Optional[str]
    value: Any

    def __post_init__(self) -> None:
        if self.name is not None:
            _check_identifier(self.name, "parameter name", dotted=False)

    def _render(self, ctx: _Ctx) -> str:
        return ctx.add(self.name, self.value)


@dataclass(frozen=True, eq=False)
class Raw(Expr):
    """Verbatim SQL: the escape hatch.

    The text goes into the statement exactly as written, so it is for SQL
    the author typed (``COUNT(*)``, ``NOW() - INTERVAL '1 day'``), never for
    anything that came from a request. Values it needs are passed as keyword
    parameters and referenced as ``:name`` in the text.
    """

    sql: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sql, str):
            raise QueryError(f"Q.raw() takes a SQL string, got {type(self.sql).__name__}")
        for name in self.params:
            _check_identifier(name, "parameter name", dotted=False)

    def _render(self, ctx: _Ctx) -> str:
        for name, value in self.params.items():
            ctx.add(name, value)
        return self.sql


@dataclass(frozen=True, eq=False)
class Func(Expr):
    name: str
    args: tuple[Expr, ...]

    def _render(self, ctx: _Ctx) -> str:
        return f"{self.name}({', '.join(a._render(ctx) for a in self.args)})"


@dataclass(frozen=True, eq=False)
class BinOp(Expr):
    left: Expr
    op: str
    right: Expr

    def _render(self, ctx: _Ctx) -> str:
        return f"{_group(self.left, ctx)} {self.op} {_group(self.right, ctx)}"


@dataclass(frozen=True, eq=False)
class Postfix(Expr):
    operand: Expr
    op: str

    def _render(self, ctx: _Ctx) -> str:
        return f"{_group(self.operand, ctx)} {self.op}"


@dataclass(frozen=True, eq=False)
class InList(Expr):
    operand: Expr
    values: tuple[Any, ...]
    negate: bool

    def _render(self, ctx: _Ctx) -> str:
        if not self.values:
            # ``IN ()`` is a syntax error on every backend. An empty list
            # matches nothing, so emit the constant with that meaning.
            return "1 = 1" if self.negate else "1 = 0"
        items = ", ".join(_operand(v)._render(ctx) for v in self.values)
        op = "NOT IN" if self.negate else "IN"
        return f"{_group(self.operand, ctx)} {op} ({items})"


@dataclass(frozen=True, eq=False)
class Between(Expr):
    operand: Expr
    low: Expr
    high: Expr

    def _render(self, ctx: _Ctx) -> str:
        return (
            f"{_group(self.operand, ctx)} BETWEEN "
            f"{self.low._render(ctx)} AND {self.high._render(ctx)}"
        )


@dataclass(frozen=True, eq=False)
class BoolOp(Expr):
    op: str
    parts: tuple[Expr, ...]

    def _render(self, ctx: _Ctx) -> str:
        rendered = []
        for part in self._flattened():
            text = part._render(ctx)
            # A nested AND/OR of the other kind needs its own parentheses;
            # everything else binds tighter than both.
            if isinstance(part, BoolOp):
                text = f"({text})"
            rendered.append(text)
        return f" {self.op} ".join(rendered)

    def _flattened(self) -> Iterable[Expr]:
        for part in self.parts:
            if isinstance(part, BoolOp) and part.op == self.op:
                yield from part._flattened()
            else:
                yield part


@dataclass(frozen=True, eq=False)
class Not(Expr):
    operand: Expr

    def _render(self, ctx: _Ctx) -> str:
        return f"NOT ({self.operand._render(ctx)})"


@dataclass(frozen=True, eq=False)
class Aliased(Expr):
    expr: Expr
    alias: str

    def _render(self, ctx: _Ctx) -> str:
        return f"{self.expr._render(ctx)} AS {self.alias}"


@dataclass(frozen=True, eq=False)
class Ordering(Expr):
    expr: Expr
    direction: str

    def _render(self, ctx: _Ctx) -> str:
        text = self.expr._render(ctx)
        return text if self.direction == "ASC" else f"{text} {self.direction}"


@dataclass(frozen=True, eq=False)
class Star(Expr):
    """``*`` or ``table.*`` in a column list."""

    prefix: Optional[str] = None

    def _render(self, ctx: _Ctx) -> str:
        return f"{self.prefix}.*" if self.prefix else "*"


def _expr(value: Any) -> Expr:
    if not isinstance(value, Expr):
        raise QueryError(
            f"expected an expression, got {type(value).__name__}; wrap literal "
            "values with Q.p() or compare a column against them"
        )
    return value


def _operand(value: Any) -> Expr:
    """An expression stays as written; anything else becomes a parameter."""
    if isinstance(value, Expr):
        return value
    return Param(None, value)


def _group(expr: Expr, ctx: _Ctx) -> str:
    """Parenthesise compound operands so operator precedence cannot bite."""
    text = expr._render(ctx)
    if isinstance(expr, (BinOp, BoolOp)):
        return f"({text})"
    return text


def _column(value: Union[str, Expr]) -> Expr:
    if isinstance(value, Expr):
        return value
    if value == "*":
        return Star()
    if isinstance(value, str) and value.endswith(".*"):
        return Star(_check_identifier(value[:-2], "table", dotted=False))
    return Col(value)


def _ordering(value: Union[str, Expr]) -> Expr:
    """``"-name"`` sorts descending, ``"name"`` or ``"+name"`` ascending."""
    if isinstance(value, Expr):
        return value
    if isinstance(value, str) and value[:1] in ("-", "+"):
        return Ordering(Col(value[1:]), "DESC" if value[0] == "-" else "ASC")
    return Ordering(Col(value), "ASC")


def _table(value: Union[str, Col]) -> Col:
    if isinstance(value, Col):
        return value
    return Col(_check_identifier(value, "table name"))


def _page_size(value: Any, what: str) -> int:
    # LIMIT and OFFSET are inlined as digits, not parameters, because the
    # SQL Server translation rewrites ``LIMIT n OFFSET m`` into
    # OFFSET/FETCH by matching literal numbers. Validating them as
    # non-negative ints is what makes inlining safe.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryError(f"{what} must be a non-negative int, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class Statement:
    """Common behaviour: build() and the run-it-on-a-manager conveniences."""

    def build(self) -> tuple[str, dict[str, Any]]:
        """The canonical SQL and its ``:name`` parameters."""
        probe = _Ctx(probing=True)
        self._render(probe)
        ctx = _Ctx(reserved=frozenset(probe.explicit))
        return self._render(ctx), ctx.params

    def _render(self, ctx: _Ctx) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def execute(self, db: "DatabaseManager", return_id: bool = False) -> Any:
        """Run on a ``DatabaseManager``: rows for SELECT, ``[]`` for writes,
        the generated key with ``return_id=True`` on an INSERT."""
        sql, params = self.build()
        return db.execute(sql, params, return_id=return_id)

    def fetch_one(self, db: "DatabaseManager") -> Optional[dict]:
        sql, params = self.build()
        return db.fetch_one(sql, params)

    def __str__(self) -> str:
        return self.build()[0]


@dataclass(frozen=True, eq=False)
class Join:
    kind: str
    table: Col
    on: Optional[Expr]
    alias: Optional[str]

    def _render(self, ctx: _Ctx) -> str:
        text = f"{_JOIN_KINDS[self.kind]} {self.table._render(ctx)}"
        if self.alias:
            text += f" AS {self.alias}"
        if self.on is not None:
            text += f" ON {self.on._render(ctx)}"
        return text


@dataclass(frozen=True, eq=False)
class Select(Statement):
    table: Col
    alias: Optional[str] = None
    _columns: tuple[Expr, ...] = ()
    _distinct: bool = False
    _joins: tuple[Join, ...] = ()
    _where: Optional[Expr] = None
    _group_by: tuple[Expr, ...] = ()
    _having: Optional[Expr] = None
    _order_by: tuple[Expr, ...] = ()
    _limit: Optional[int] = None
    _offset: Optional[int] = None

    def columns(self, *columns: Union[str, Expr]) -> Select:
        """Columns to return; none means ``*``. Strings are validated
        identifiers (``"*"`` and ``"t.*"`` allowed); expressions pass
        through, so ``Q.raw("COUNT(*)").as_("n")`` works."""
        return replace(self, _columns=self._columns + tuple(_column(c) for c in columns))

    def distinct(self) -> Select:
        return replace(self, _distinct=True)

    def join(
        self,
        table: Union[str, Col],
        on: Optional[Expr] = None,
        kind: str = "inner",
        alias: Optional[str] = None,
    ) -> Select:
        """``kind`` is one of inner, left, right, full, cross."""
        kind = kind.lower()
        if kind not in _JOIN_KINDS:
            raise QueryError(
                f"unknown join kind {kind!r}; expected one of {sorted(_JOIN_KINDS)}"
            )
        if kind == "cross":
            if on is not None:
                raise QueryError("a cross join takes no ON condition")
        elif on is None:
            raise QueryError(f"a {kind} join needs an ON condition")
        else:
            _expr(on)
        if alias is not None:
            _check_identifier(alias, "alias", dotted=False)
        return replace(self, _joins=self._joins + (Join(kind, _table(table), on, alias),))

    def where(self, condition: Expr) -> Select:
        """Add a condition; repeated calls are ANDed together."""
        return replace(self, _where=_and(self._where, condition))

    def group_by(self, *columns: Union[str, Expr]) -> Select:
        return replace(self, _group_by=self._group_by + tuple(_column(c) for c in columns))

    def having(self, condition: Expr) -> Select:
        return replace(self, _having=_and(self._having, condition))

    def order_by(self, *columns: Union[str, Expr]) -> Select:
        """``"name"`` sorts ascending, ``"-name"`` descending; expressions
        may carry ``.asc()`` / ``.desc()``."""
        return replace(self, _order_by=self._order_by + tuple(_ordering(c) for c in columns))

    def limit(self, count: int) -> Select:
        return replace(self, _limit=_page_size(count, "limit"))

    def offset(self, count: int) -> Select:
        return replace(self, _offset=_page_size(count, "offset"))

    def _render(self, ctx: _Ctx) -> str:
        parts = ["SELECT"]
        if self._distinct:
            parts.append("DISTINCT")
        columns = self._columns or (Star(),)
        parts.append(", ".join(c._render(ctx) for c in columns))
        parts.append(f"FROM {self.table._render(ctx)}")
        if self.alias:
            parts.append(f"AS {self.alias}")
        for join in self._joins:
            parts.append(join._render(ctx))
        if self._where is not None:
            parts.append(f"WHERE {self._where._render(ctx)}")
        if self._group_by:
            parts.append("GROUP BY " + ", ".join(c._render(ctx) for c in self._group_by))
        if self._having is not None:
            parts.append(f"HAVING {self._having._render(ctx)}")
        if self._order_by:
            parts.append("ORDER BY " + ", ".join(c._render(ctx) for c in self._order_by))
        if self._limit is not None:
            parts.append(f"LIMIT {self._limit}")
        if self._offset is not None:
            parts.append(f"OFFSET {self._offset}")
        return " ".join(parts)


@dataclass(frozen=True, eq=False)
class Insert(Statement):
    table: Col
    _values: tuple[tuple[str, Expr], ...] = ()
    _returning: tuple[Expr, ...] = ()

    def values(self, mapping: Optional[Mapping[str, Any]] = None, **columns: Any) -> Insert:
        """Column values, as a mapping and/or keywords. A column named again
        in a later call takes the new value."""
        merged = dict(self._values)
        for name, value in {**(mapping or {}), **columns}.items():
            merged[_check_identifier(name, "column name", dotted=False)] = _operand(value)
        return replace(self, _values=tuple(merged.items()))

    def returning(self, *columns: Union[str, Expr]) -> Insert:
        """Append ``RETURNING``. PostgreSQL and SQLite (3.35+) honour it;
        for a portable generated key use ``execute(db, return_id=True)``."""
        return replace(self, _returning=self._returning + tuple(_column(c) for c in columns))

    def _render(self, ctx: _Ctx) -> str:
        if not self._values:
            raise QueryError("INSERT needs at least one column; call .values()")
        names = ", ".join(name for name, _ in self._values)
        values = ", ".join(expr._render(ctx) for _, expr in self._values)
        sql = f"INSERT INTO {self.table._render(ctx)} ({names}) VALUES ({values})"
        if self._returning:
            sql += " RETURNING " + ", ".join(c._render(ctx) for c in self._returning)
        return sql


@dataclass(frozen=True, eq=False)
class Update(Statement):
    table: Col
    _set: tuple[tuple[str, Expr], ...] = ()
    _where: Optional[Expr] = None
    _all_rows: bool = False

    def set(self, mapping: Optional[Mapping[str, Any]] = None, **columns: Any) -> Update:
        merged = dict(self._set)
        for name, value in {**(mapping or {}), **columns}.items():
            merged[_check_identifier(name, "column name", dotted=False)] = _operand(value)
        return replace(self, _set=tuple(merged.items()))

    def where(self, condition: Expr) -> Update:
        return replace(self, _where=_and(self._where, condition))

    def all_rows(self) -> Update:
        """Opt in to an UPDATE with no WHERE clause."""
        return replace(self, _all_rows=True)

    def _render(self, ctx: _Ctx) -> str:
        if not self._set:
            raise QueryError("UPDATE needs at least one column; call .set()")
        _require_where(self, "UPDATE")
        assignments = ", ".join(f"{name} = {expr._render(ctx)}" for name, expr in self._set)
        sql = f"UPDATE {self.table._render(ctx)} SET {assignments}"
        if self._where is not None:
            sql += f" WHERE {self._where._render(ctx)}"
        return sql


@dataclass(frozen=True, eq=False)
class Delete(Statement):
    table: Col
    _where: Optional[Expr] = None
    _all_rows: bool = False

    def where(self, condition: Expr) -> Delete:
        return replace(self, _where=_and(self._where, condition))

    def all_rows(self) -> Delete:
        """Opt in to a DELETE with no WHERE clause."""
        return replace(self, _all_rows=True)

    def _render(self, ctx: _Ctx) -> str:
        _require_where(self, "DELETE")
        sql = f"DELETE FROM {self.table._render(ctx)}"
        if self._where is not None:
            sql += f" WHERE {self._where._render(ctx)}"
        return sql


def _and(current: Optional[Expr], condition: Any) -> Expr:
    condition = _expr(condition)
    return condition if current is None else BoolOp("AND", (current, condition))


def _require_where(stmt: Any, verb: str) -> None:
    # A filter that evaluates to None (``.where(cond) if cond else ...``
    # gone wrong) would otherwise touch the whole table; make that explicit.
    if stmt._where is None and not stmt._all_rows:
        raise QueryError(
            f"{verb} without WHERE would affect every row; add .where(...) "
            "or call .all_rows() to say that is intended"
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

class Q:
    """Entry points for the builder; every method returns an immutable object."""

    @staticmethod
    def select(table: Union[str, Col], alias: Optional[str] = None) -> Select:
        if alias is not None:
            _check_identifier(alias, "alias", dotted=False)
        return Select(_table(table), alias)

    @staticmethod
    def insert(table: Union[str, Col]) -> Insert:
        return Insert(_table(table))

    @staticmethod
    def update(table: Union[str, Col]) -> Update:
        return Update(_table(table))

    @staticmethod
    def delete(table: Union[str, Col]) -> Delete:
        return Delete(_table(table))

    @staticmethod
    def col(name: str, quote: bool = False) -> Col:
        """A column or table reference; ``quote=True`` for case-sensitive names."""
        return Col(name, quote)

    @staticmethod
    def p(name: str, value: Any) -> Param:
        """An explicitly named parameter, ``:name`` in the SQL."""
        return Param(name, value)

    @staticmethod
    def raw(sql: str, **params: Any) -> Raw:
        """Verbatim SQL (see ``Raw``): for text the author wrote, never input."""
        return Raw(sql, params)
