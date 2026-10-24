"""
Sharding router: one manager-shaped object in front of several databases.

``ShardedDatabaseManager`` holds a named ``DatabaseManager`` per shard and
decides, per statement, which of them runs it. It offers the same calls as
``DatabaseManager`` (execute / fetch_one / their async forms / transaction /
table_exists / execute_script / connect / disconnect / context manager), so
code written against one database can be pointed at several without its
query calls changing.

A statement is placed in this order:

1. ``shard=`` on the call, when given.
2. The router, which reads the shard key out of the statement's params.
3. ``default_shard``, when configured.
4. Otherwise the statement is unrouted. A read fans out to every shard and
   the rows are concatenated in shard order. A write raises, because
   silently writing the same row to every shard is never what was meant;
   ``all_shards=True`` opts a write (a schema change, say) into the fan-out.

Consistency model
-----------------
Each shard is an independent database and nothing here changes that.

- No cross-shard transactions. ``transaction()`` pins its block to one
  shard, chosen up front with ``shard=`` or by the first statement inside
  it, and a later statement that routes elsewhere raises rather than
  quietly running outside the transaction.
- No distributed joins. A fan-out runs the same SQL on each shard and
  concatenates the rows, so a JOIN only sees rows that live on the same
  shard, and an aggregate (``COUNT``, ``SUM``) comes back once per shard
  rather than combined.
- ``LIMIT`` in fanned-out SQL applies per shard. The ``limit=`` and
  ``order_by=`` arguments are applied client-side to the concatenated rows
  and are the way to get a global ordering or cap.
- A fan-out is not atomic across shards: a later shard failing leaves the
  earlier ones' writes committed.

Thread safety is that of the underlying managers. Each shard's manager
tracks its transaction per thread, and so does this class, so two threads
can run independent transactions through one sharded manager.
"""

import asyncio
import bisect
import hashlib
import logging
import threading
from contextlib import ExitStack, contextmanager
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

from .interfaces import QueryResult
from .manager import DatabaseManager, reject_transaction_statement

logger = logging.getLogger(__name__)

Params = Optional[Dict[str, Any]]
RouterFn = Callable[[Params], Optional[str]]
OrderBy = Sequence[Tuple[str, str]]


class ShardingError(RuntimeError):
    """A statement could not be placed on exactly one shard."""


class Router(Protocol):
    """Anything with ``shard_for(params)`` can route; a bare callable works too."""

    def shard_for(self, params: Params) -> Optional[str]: ...


# --- routers ------------------------------------------------------------------
#
# Each router reads one named parameter out of the statement's params and
# turns its value into a shard name. Returning None means "unrouted", which
# the manager then resolves through default_shard or a fan-out. A missing
# key is unrouted rather than an error so that statements without the key
# (schema changes, global reads) can still be run through the same object.


class KeyRouter:
    """
    Explicit value-to-shard mapping.

        KeyRouter("region", {"eu": "shard_eu", "us": "shard_us"})

    A value absent from the mapping leaves the statement unrouted.
    """

    def __init__(self, param_name: str, mapping: Mapping[Any, str]) -> None:
        self.param_name = param_name
        self.mapping: Dict[Any, str] = dict(mapping)

    def shard_for(self, params: Params) -> Optional[str]:
        if not params or self.param_name not in params:
            return None
        return self.mapping.get(params[self.param_name])


class HashRouter:
    """
    Stable hash of the key, modulo the number of shards.

        HashRouter("user_id", ["shard_0", "shard_1", "shard_2"])

    The hash is BLAKE2b over ``str(value)``, so it is the same across
    processes and Python versions, which Python's own ``hash()`` is not
    (it is salted per process for str). Hashing the string form also
    means ``7`` and ``"7"`` land on the same shard, so a key read from a
    URL path routes like the same key read back from a table.

    Shard order matters: the index into ``shard_names`` is the hash
    bucket, so reordering or resizing the list moves rows between shards.
    """

    def __init__(self, param_name: str, shard_names: Sequence[str]) -> None:
        if not shard_names:
            raise ValueError("HashRouter needs at least one shard name")
        self.param_name = param_name
        self.shard_names: Tuple[str, ...] = tuple(shard_names)

    @staticmethod
    def bucket(value: Any, bucket_count: int) -> int:
        """Bucket index for ``value``; exposed so callers can precompute placements."""
        digest = hashlib.blake2b(str(value).encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % bucket_count

    def shard_for(self, params: Params) -> Optional[str]:
        if not params or params.get(self.param_name) is None:
            return None
        return self.shard_names[self.bucket(params[self.param_name], len(self.shard_names))]


class RangeRouter:
    """
    Contiguous key ranges, each ending at an exclusive upper bound.

        RangeRouter("user_id", [(1000, "shard_a"), (2000, "shard_b"), (None, "shard_c")])

    places ``user_id < 1000`` on ``shard_a``, ``1000 <= user_id < 2000`` on
    ``shard_b`` and everything from 2000 up on ``shard_c``. The ``None``
    upper bound is the optional open-ended tail and must come last. Bounds
    must ascend. A key beyond the last finite bound with no tail is
    unrouted.
    """

    def __init__(self, param_name: str, boundaries: Sequence[Tuple[Any, str]]) -> None:
        self.param_name = param_name
        uppers: List[Any] = []
        names: List[str] = []
        tail: Optional[str] = None
        for upper, name in boundaries:
            if tail is not None:
                raise ValueError("the open-ended (None) range must be the last boundary")
            if upper is None:
                tail = name
                continue
            if uppers and not uppers[-1] < upper:
                raise ValueError("RangeRouter boundaries must ascend")
            uppers.append(upper)
            names.append(name)
        if not uppers and tail is None:
            raise ValueError("RangeRouter needs at least one boundary")
        self._uppers = uppers
        self._names = names
        self._tail = tail

    def shard_for(self, params: Params) -> Optional[str]:
        if not params or params.get(self.param_name) is None:
            return None
        # bisect_right makes the upper bound exclusive: a key equal to a
        # bound belongs to the next range.
        index = bisect.bisect_right(self._uppers, params[self.param_name])
        if index < len(self._names):
            return self._names[index]
        return self._tail


# --- statement classification and client-side shaping ------------------------

# Anything that does not open with one of these is treated as a write, so
# DDL and unfamiliar statements err on the side of refusing to fan out.
# A WITH is read as a query; a data-modifying CTE needs shard= or
# all_shards= to say where it goes.
_READ_PREFIXES = ("SELECT", "WITH", "SHOW", "EXPLAIN", "PRAGMA", "DESCRIBE", "VALUES")


def _is_write(query: str) -> bool:
    return not query.lstrip().lstrip("(").upper().startswith(_READ_PREFIXES)


def _sort_rows(rows: List[Dict[str, Any]], order_by: OrderBy) -> List[Dict[str, Any]]:
    """
    Sort concatenated rows the way ORDER BY would have.

    Python's sort is stable, so sorting by the last key first and the first
    key last yields the multi-column ordering. NULLs cannot be compared with
    values, so the key tuple sorts them before everything else ascending
    and after everything descending, matching PostgreSQL's default.
    """
    ordered = list(rows)
    for column, direction in reversed(list(order_by)):
        descending = direction.lower() == "desc"
        if not descending and direction.lower() != "asc":
            raise ValueError(f"order_by direction must be 'asc' or 'desc', not {direction!r}")
        try:
            ordered.sort(
                key=lambda row: (row[column] is not None, row[column]),
                reverse=descending,
            )
        except KeyError:
            raise ShardingError(
                f"order_by column {column!r} is not in the result rows; "
                "select it so the rows can be ordered client-side"
            ) from None
    return ordered


class ShardedDatabaseManager:
    """
    Routes statements across several ``DatabaseManager`` instances.

        db = ShardedDatabaseManager(
            {"eu": "postgresql://.../eu", "us": "postgresql://.../us"},
            router=KeyRouter("region", {"eu": "eu", "us": "us"}),
        )
        db.connect()
        db.execute("INSERT INTO users (region, name) VALUES (:region, :name)",
                   {"region": "eu", "name": "Ada"})           # routed to eu
        db.execute("SELECT * FROM users ORDER BY name",
                   order_by=[("name", "asc")], limit=10)      # fan-out, merged

    Shards given as URLs are wrapped in ``DatabaseManager(url,
    **manager_kwargs)``; a ``DatabaseManager`` instance is used as given
    and should not be connected yet, since ``connect()`` connects every
    shard. Dict order is shard order, which fixes the order of fan-out
    results and the order ``fetch_one`` searches in.

    See the module docstring for the routing order and consistency model.
    """

    def __init__(
        self,
        shards: Mapping[str, Union[str, DatabaseManager]],
        router: Optional[Union[Router, RouterFn]] = None,
        default_shard: Optional[str] = None,
        **manager_kwargs: Any,
    ) -> None:
        if not shards:
            raise ValueError("ShardedDatabaseManager needs at least one shard")
        managers: Dict[str, DatabaseManager] = {}
        for name, target in shards.items():
            if isinstance(target, DatabaseManager):
                managers[name] = target
            else:
                managers[name] = DatabaseManager(target, **manager_kwargs)
        self._shards = managers
        self._view: Mapping[str, DatabaseManager] = MappingProxyType(managers)

        if default_shard is not None and default_shard not in managers:
            raise ValueError(f"default_shard {default_shard!r} is not one of {list(managers)}")
        self.default_shard = default_shard

        self.router = router
        self._route: Optional[RouterFn]
        if router is None:
            self._route = None
        elif hasattr(router, "shard_for"):
            self._route = router.shard_for  # type: ignore[union-attr]
        elif callable(router):
            self._route = router
        else:
            raise TypeError("router must have a shard_for(params) method or be callable")

        # Transaction state per thread, mirroring DatabaseManager: the
        # shard a block is pinned to, the nesting depth, and the ExitStack
        # holding the underlying manager's transaction once it is entered.
        self._state = threading.local()

    # ------------------------------------------------------------------
    # Shard inspection
    # ------------------------------------------------------------------

    @property
    def shards(self) -> Mapping[str, DatabaseManager]:
        """Read-only view of shard name to manager, in shard order."""
        return self._view

    def for_each_shard(self) -> Iterator[Tuple[str, DatabaseManager]]:
        """Iterate ``(name, manager)`` in shard order."""
        return iter(self._shards.items())

    def shard_for(self, params: Params) -> Optional[str]:
        """
        Shard these params route to, falling back to ``default_shard``.

        None means the statement would be unrouted: a read fans out, a
        write raises.
        """
        name = self._routed(params)
        return self.default_shard if name is None else name

    def _routed(self, params: Params) -> Optional[str]:
        """The router's answer alone, validated against the shard names."""
        if self._route is None:
            return None
        name = self._route(params)
        if name is not None:
            self._check(name)
        return name

    def _check(self, name: str) -> None:
        if name not in self._shards:
            raise ShardingError(f"unknown shard {name!r}; shards are {list(self._shards)}")

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    @property
    def _in_transaction(self) -> bool:
        return getattr(self._state, "tx_depth", 0) > 0

    def _target(self, params: Params, shard: Optional[str], all_shards: bool,
                is_write: bool) -> Optional[str]:
        """
        Name of the single shard a statement runs on, or None for a fan-out.

        Inside a transaction this also pins the block to the chosen shard
        the first time through, and refuses anything that would leave it.
        """
        if shard is not None:
            self._check(shard)
            if all_shards:
                raise ShardingError("shard= and all_shards=True contradict each other")

        if self._in_transaction:
            if all_shards:
                raise ShardingError(
                    "all_shards=True is not allowed inside a transaction, which spans one shard")
            name = shard if shard is not None else self._routed(params)
            if name is None:
                name = self._state.tx_shard or self.default_shard
            if name is None:
                raise ShardingError(
                    "the first statement of a transaction could not be routed; "
                    "pass transaction(shard=...) or include the shard key in params")
            return self._pin(name)

        if all_shards:
            return None
        if shard is not None:
            return shard
        name = self.shard_for(params)
        if name is not None:
            return name
        if is_write:
            raise ShardingError(
                "write could not be routed to a shard: pass shard=, include the "
                "shard key in params, configure default_shard, or pass "
                "all_shards=True to run it on every shard")
        return None

    def _pin(self, name: str) -> str:
        state = self._state
        pinned: Optional[str] = state.tx_shard
        if pinned is None:
            state.tx_stack.enter_context(self._shards[name].transaction())
            state.tx_shard = name
            return name
        if pinned != name:
            raise ShardingError(
                f"statement routes to shard {name!r} inside a transaction pinned to "
                f"{pinned!r}; a transaction spans one shard")
        return pinned

    @staticmethod
    def _shape(rows: Any, order_by: Optional[OrderBy], limit: Optional[int]) -> Any:
        if order_by:
            rows = _sort_rows(rows, order_by)
        if limit is not None:
            rows = rows[:limit]
        return rows

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def execute(self, query: str, params: Params = None, return_id: bool = False, *,
                shard: Optional[str] = None, all_shards: bool = False,
                order_by: Optional[OrderBy] = None, limit: Optional[int] = None) -> Any:
        """
        Run a statement on its shard, or fan a read out to every shard.

        Same contract as ``DatabaseManager.execute`` on the shard it lands
        on. An unrouted read runs on every shard and the rows are
        concatenated in shard order; ``order_by=[("col", "asc" | "desc"),
        ...]`` and ``limit=`` are then applied client-side, which is how a
        global ordering or cap is obtained (a ``LIMIT`` in the SQL applies
        per shard). An unrouted write raises unless ``all_shards=True``.
        ``return_id`` needs a single shard, since it returns one key.
        """
        reject_transaction_statement(query)
        target = self._target(params, shard, all_shards, is_write=return_id or _is_write(query))
        if target is not None:
            result = self._shards[target].execute(query, params, return_id=return_id)
            return result if return_id else self._shape(result, order_by, limit)
        if return_id:
            raise ShardingError("return_id=True needs a single shard; pass shard= or a routable key")

        rows: List[Dict[str, Any]] = []
        for manager in self._shards.values():
            rows.extend(manager.execute(query, params))
        return self._shape(rows, order_by, limit)

    def fetch_one(self, query: str, params: Params = None, *,
                  shard: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetch one row from its shard.

        Unrouted, the shards are asked in order and the first row found is
        returned, so an unrouted lookup costs up to one query per shard.
        """
        target = self._target(params, shard, False, is_write=_is_write(query))
        if target is not None:
            return self._shards[target].fetch_one(query, params)
        for manager in self._shards.values():
            row = manager.fetch_one(query, params)
            if row is not None:
                return row
        return None

    async def execute_async(self, query: str, params: Params = None, return_id: bool = False, *,
                            shard: Optional[str] = None, all_shards: bool = False,
                            order_by: Optional[OrderBy] = None,
                            limit: Optional[int] = None) -> Any:
        """
        ``execute`` without blocking the event loop.

        A fan-out runs the shards concurrently and still concatenates their
        rows in shard order. Routing happens on the calling thread, so
        transaction pinning behaves as it does for ``execute``.
        """
        reject_transaction_statement(query)
        target = self._target(params, shard, all_shards, is_write=return_id or _is_write(query))
        if target is not None:
            result = await self._shards[target].execute_async(query, params, return_id=return_id)
            return result if return_id else self._shape(result, order_by, limit)
        if return_id:
            raise ShardingError("return_id=True needs a single shard; pass shard= or a routable key")

        parts = await asyncio.gather(
            *(manager.execute_async(query, params) for manager in self._shards.values()))
        rows = [row for part in parts for row in part]
        return self._shape(rows, order_by, limit)

    async def fetch_one_async(self, query: str, params: Params = None, *,
                              shard: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """``fetch_one`` without blocking the event loop; an unrouted lookup asks every shard at once."""
        target = self._target(params, shard, False, is_write=_is_write(query))
        if target is not None:
            return await self._shards[target].fetch_one_async(query, params)
        rows = await asyncio.gather(
            *(manager.fetch_one_async(query, params) for manager in self._shards.values()))
        for row in rows:
            if row is not None:
                return row
        return None

    @contextmanager
    def transaction(self, shard: Optional[str] = None) -> Iterator["ShardedDatabaseManager"]:
        """
        Run a block inside one shard's transaction.

        The shard is ``shard=`` when given, otherwise whichever shard the
        first statement in the block routes to (falling back to
        ``default_shard``). Every later statement must land on that same
        shard; one that routes elsewhere raises ``ShardingError`` and the
        transaction rolls back. Statements inside the block that carry no
        shard key run on the pinned shard. Nested use joins the outer
        block, as with ``DatabaseManager``.

        The underlying manager's transaction is entered when the shard
        is known, so a block that runs no statement opens no transaction.
        """
        state = self._state
        if self._in_transaction:
            if shard is not None:
                self._check(shard)
                self._pin(shard)
            state.tx_depth += 1
            try:
                yield self
            finally:
                state.tx_depth -= 1
            return

        state.tx_depth = 1
        state.tx_shard = None
        state.tx_stack = ExitStack()
        try:
            # The ExitStack owns the shard's transaction: on a clean exit it
            # commits, and an exception raised in the block reaches it and
            # rolls back, exactly as if the block were on the manager.
            with state.tx_stack:
                if shard is not None:
                    self._check(shard)
                    self._pin(shard)
                yield self
        finally:
            state.tx_depth = 0
            state.tx_shard = None
            state.tx_stack = None

    def table_exists(self, table_name: str, *, shard: Optional[str] = None) -> bool:
        """
        True when the table exists on the given shard, or on every shard.

        A sharded table is only usable through this object if each shard
        has it, so the unqualified check asks all of them. Use ``shard=``
        for a table that lives on one shard only.
        """
        if shard is not None:
            self._check(shard)
            return self._shards[shard].table_exists(table_name)
        return all(manager.table_exists(table_name) for manager in self._shards.values())

    async def execute_script(self, script: str, *, shard: Optional[str] = None,
                             all_shards: bool = False) -> QueryResult:
        """
        Run a script on one shard, or with ``all_shards=True`` on each in turn.

        A script is a write, so without ``shard=``, ``default_shard`` or
        ``all_shards=True`` it raises rather than guessing. Across shards
        the first failure stops the run and is returned, named with its
        shard; shards already done keep their changes.
        """
        target = self._target(None, shard, all_shards, is_write=True)
        if target is not None:
            return await self._shards[target].execute_script(script)
        for name, manager in self._shards.items():
            result = await manager.execute_script(script)
            if not result.success:
                result.error_message = f"shard {name!r}: {result.error_message}"
                return result
        return QueryResult(success=True, data=[], row_count=0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Connect every shard; False if any fails, with the rest disconnected.

        Half a shard set is worse than none: reads would silently miss
        rows and writes would land on whichever shards happened to be up,
        so a single failure brings the whole thing down cleanly.
        """
        connected: List[DatabaseManager] = []
        for name, manager in self._shards.items():
            try:
                ok = manager.connect()
            except Exception as exc:
                logger.error(f"shard {name!r} failed to connect: {exc}")
                ok = False
            if not ok:
                logger.error(f"shard {name!r} is unavailable; disconnecting the other shards")
                for done in connected:
                    done.disconnect()
                return False
            connected.append(manager)
        return True

    def disconnect(self) -> None:
        """Disconnect every shard, continuing past one that fails to."""
        for name, manager in self._shards.items():
            try:
                manager.disconnect()
            except Exception as exc:
                logger.error(f"shard {name!r} failed to disconnect: {exc}")

    async def close(self) -> None:
        """Async-compatible disconnect, matching ``DatabaseManager.close``."""
        self.disconnect()

    def __enter__(self) -> "ShardedDatabaseManager":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.disconnect()
