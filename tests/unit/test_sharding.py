"""
ShardedDatabaseManager: routing, fan-out, single-shard transactions.

Three SQLite files stand in for three shards, so every path runs against
real drivers without a server. The connect-failure case uses an
unreachable PostgreSQL URL with a short connect_timeout, the same probe
tests/unit/test_pool.py uses.
"""

import hashlib

import pytest

from nexusql import (
    DatabaseManager,
    HashRouter,
    KeyRouter,
    RangeRouter,
    ShardedDatabaseManager,
    ShardingError,
)

SHARDS = ("a", "b", "c")
CREATE_USERS = (
    "CREATE TABLE users (id SERIAL PRIMARY KEY, region TEXT, "
    "user_id INTEGER, name TEXT)"
)
INSERT_USER = (
    "INSERT INTO users (region, user_id, name) VALUES (:region, :user_id, :name)"
)


@pytest.fixture
def shard_urls(tmp_path):
    return {name: f"sqlite:///{tmp_path / f'{name}.db'}" for name in SHARDS}


def _rows_on(url, sql="SELECT region, user_id, name FROM users ORDER BY id"):
    """Read a shard file directly, bypassing the router."""
    reader = DatabaseManager(url)
    assert reader.connect()
    try:
        return reader.execute(sql)
    finally:
        reader.disconnect()


@pytest.fixture
def db(shard_urls):
    """Key-routed manager over three shards, with the users table everywhere."""
    region_router = KeyRouter("region", {"eu": "a", "us": "b", "ap": "c"})
    manager = ShardedDatabaseManager(shard_urls, router=region_router)
    assert manager.connect()
    manager.execute(CREATE_USERS, all_shards=True)
    yield manager
    manager.disconnect()


def _seed(db):
    """One EU user, two US users, three AP users."""
    users = [("eu", 1, "ada"), ("us", 2, "bob"), ("us", 3, "cy"),
             ("ap", 4, "dee"), ("ap", 5, "eve"), ("ap", 6, "fay")]
    for region, user_id, name in users:
        db.execute(INSERT_USER, {"region": region, "user_id": user_id, "name": name})


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

class TestRouters:
    def test_key_router(self):
        router = KeyRouter("region", {"eu": "a", "us": "b"})
        assert router.shard_for({"region": "eu"}) == "a"
        assert router.shard_for({"region": "us"}) == "b"
        assert router.shard_for({"region": "mars"}) is None
        assert router.shard_for({"other": 1}) is None
        assert router.shard_for(None) is None

    def test_hash_router_is_stable_and_covers_every_shard(self):
        router = HashRouter("user_id", SHARDS)
        # Pinned to the formula, so a change in hashing shows up here
        # rather than as rows silently moving shards after an upgrade.
        digest = hashlib.blake2b(b"42", digest_size=8).digest()
        expected = SHARDS[int.from_bytes(digest, "big") % 3]
        assert router.shard_for({"user_id": 42}) == expected
        assert HashRouter("user_id", SHARDS).shard_for({"user_id": 42}) == expected
        # A key read from a URL (str) routes like the same key read back (int).
        assert router.shard_for({"user_id": "42"}) == expected
        assert router.shard_for({"user_id": None}) is None
        assert router.shard_for({}) is None
        hit = {router.shard_for({"user_id": i}) for i in range(100)}
        assert hit == set(SHARDS)

    def test_hash_router_needs_shards(self):
        with pytest.raises(ValueError):
            HashRouter("user_id", [])

    def test_range_router(self):
        router = RangeRouter("user_id", [(10, "a"), (20, "b"), (None, "c")])
        assert router.shard_for({"user_id": 0}) == "a"
        assert router.shard_for({"user_id": 9}) == "a"
        assert router.shard_for({"user_id": 10}) == "b"
        assert router.shard_for({"user_id": 19}) == "b"
        assert router.shard_for({"user_id": 20}) == "c"
        assert router.shard_for({"user_id": 10 ** 9}) == "c"
        assert router.shard_for({"name": "x"}) is None

    def test_range_router_without_tail_leaves_overflow_unrouted(self):
        router = RangeRouter("user_id", [(10, "a"), (20, "b")])
        assert router.shard_for({"user_id": 25}) is None

    def test_range_router_rejects_bad_boundaries(self):
        with pytest.raises(ValueError, match="ascend"):
            RangeRouter("k", [(20, "a"), (10, "b")])
        with pytest.raises(ValueError, match="last"):
            RangeRouter("k", [(None, "a"), (10, "b")])
        with pytest.raises(ValueError):
            RangeRouter("k", [])

    def test_callable_router(self, shard_urls):
        def by_parity(params):
            if not params or "user_id" not in params:
                return None
            return "a" if params["user_id"] % 2 else "b"

        db = ShardedDatabaseManager(shard_urls, router=by_parity)
        assert db.shard_for({"user_id": 3}) == "a"
        assert db.shard_for({"user_id": 4}) == "b"
        assert db.shard_for({}) is None

    def test_router_naming_unknown_shard_raises(self, shard_urls):
        db = ShardedDatabaseManager(shard_urls, router=lambda params: "nowhere")
        with pytest.raises(ShardingError, match="unknown shard 'nowhere'"):
            db.shard_for({"x": 1})

    def test_router_must_be_callable_or_have_shard_for(self, shard_urls):
        with pytest.raises(TypeError):
            ShardedDatabaseManager(shard_urls, router=object())


# ---------------------------------------------------------------------------
# Construction and inspection
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_shards_view_preserves_order_and_is_read_only(self, shard_urls):
        db = ShardedDatabaseManager(shard_urls)
        assert list(db.shards) == list(SHARDS)
        assert all(isinstance(m, DatabaseManager) for m in db.shards.values())
        with pytest.raises(TypeError):
            db.shards["d"] = DatabaseManager("sqlite:///:memory:")  # type: ignore[index]
        assert [name for name, _ in db.for_each_shard()] == list(SHARDS)

    def test_accepts_manager_instances(self, shard_urls):
        prebuilt = DatabaseManager(shard_urls["a"])
        db = ShardedDatabaseManager({"a": prebuilt, "b": shard_urls["b"]})
        assert db.shards["a"] is prebuilt

    def test_manager_kwargs_reach_url_shards(self, shard_urls):
        db = ShardedDatabaseManager(shard_urls, pool_max=1, connect_timeout=3)
        assert db.shards["a"]._pool_max == 1
        assert db.shards["a"]._connect_timeout == 3

    def test_rejects_empty_shards_and_unknown_default(self, shard_urls):
        with pytest.raises(ValueError):
            ShardedDatabaseManager({})
        with pytest.raises(ValueError, match="default_shard"):
            ShardedDatabaseManager(shard_urls, default_shard="zz")

    def test_shard_for_falls_back_to_default(self, shard_urls):
        router = KeyRouter("region", {"eu": "a"})
        db = ShardedDatabaseManager(shard_urls, router=router, default_shard="c")
        assert db.shard_for({"region": "eu"}) == "a"
        assert db.shard_for({"region": "xx"}) == "c"
        assert db.shard_for(None) == "c"


# ---------------------------------------------------------------------------
# Routing statements
# ---------------------------------------------------------------------------

class TestRouting:
    def test_key_routed_writes_land_on_their_shard(self, db, shard_urls):
        _seed(db)
        assert [r["name"] for r in _rows_on(shard_urls["a"])] == ["ada"]
        assert [r["name"] for r in _rows_on(shard_urls["b"])] == ["bob", "cy"]
        assert [r["name"] for r in _rows_on(shard_urls["c"])] == ["dee", "eve", "fay"]

    def test_routed_read_sees_only_its_shard(self, db):
        _seed(db)
        rows = db.execute("SELECT name FROM users WHERE region = :region", {"region": "us"})
        assert [r["name"] for r in rows] == ["bob", "cy"]
        # Explicit shard= wins over the router.
        rows = db.execute("SELECT name FROM users WHERE region = :region",
                          {"region": "us"}, shard="a")
        assert rows == []

    def test_fetch_one_routed_and_fanned_out(self, db):
        _seed(db)
        row = db.fetch_one("SELECT name FROM users WHERE region = :region AND user_id = :user_id",
                           {"region": "ap", "user_id": 5})
        assert row == {"name": "eve"}
        # No shard key: shards are searched in order until one answers.
        row = db.fetch_one("SELECT name FROM users WHERE name = :name", {"name": "cy"})
        assert row == {"name": "cy"}
        assert db.fetch_one("SELECT name FROM users WHERE name = :name", {"name": "zed"}) is None

    def test_hash_routed_rows_can_be_read_back(self, shard_urls):
        db = ShardedDatabaseManager(shard_urls, router=HashRouter("user_id", SHARDS))
        with db:
            db.execute(CREATE_USERS, all_shards=True)
            for user_id in range(30):
                db.execute(INSERT_USER, {"region": "x", "user_id": user_id, "name": f"u{user_id}"})
            for user_id in range(30):
                row = db.fetch_one("SELECT name FROM users WHERE user_id = :user_id",
                                   {"user_id": user_id})
                assert row == {"name": f"u{user_id}"}
            # Every shard received some rows and nothing was duplicated.
            per_shard = [len(m.execute("SELECT id FROM users")) for m in db.shards.values()]
            assert all(count > 0 for count in per_shard)
            assert sum(per_shard) == 30

    def test_range_routed_rows_land_in_their_band(self, shard_urls):
        router = RangeRouter("user_id", [(10, "a"), (20, "b"), (None, "c")])
        db = ShardedDatabaseManager(shard_urls, router=router)
        with db:
            db.execute(CREATE_USERS, all_shards=True)
            for user_id in (1, 10, 15, 20, 99):
                db.execute(INSERT_USER, {"region": "x", "user_id": user_id, "name": str(user_id)})
        assert [r["user_id"] for r in _rows_on(shard_urls["a"])] == [1]
        assert [r["user_id"] for r in _rows_on(shard_urls["b"])] == [10, 15]
        assert [r["user_id"] for r in _rows_on(shard_urls["c"])] == [20, 99]

    def test_return_id_is_per_shard(self, db):
        first = db.execute(INSERT_USER, {"region": "eu", "user_id": 1, "name": "a"}, return_id=True)
        second = db.execute(INSERT_USER, {"region": "us", "user_id": 2, "name": "b"}, return_id=True)
        # Each shard numbers its own rows, so a key only identifies a row
        # together with its shard.
        assert first == 1
        assert second == 1

    def test_default_shard_takes_unrouted_statements(self, shard_urls):
        router = KeyRouter("region", {"eu": "a"})
        db = ShardedDatabaseManager(shard_urls, router=router, default_shard="c")
        with db:
            db.execute(CREATE_USERS, all_shards=True)
            db.execute("INSERT INTO users (name) VALUES (:name)", {"name": "global"})
            assert db.execute("SELECT name FROM users") == [{"name": "global"}]
        assert _rows_on(shard_urls["a"]) == []
        assert [r["name"] for r in _rows_on(shard_urls["c"])] == ["global"]

    def test_unknown_explicit_shard_raises(self, db):
        with pytest.raises(ShardingError, match="unknown shard"):
            db.execute("SELECT 1", shard="zz")

    def test_shard_and_all_shards_contradict(self, db):
        with pytest.raises(ShardingError, match="contradict"):
            db.execute("SELECT 1", shard="a", all_shards=True)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

class TestFanOut:
    def test_unrouted_read_concatenates_in_shard_order(self, db):
        _seed(db)
        rows = db.execute("SELECT name FROM users")
        assert [r["name"] for r in rows] == ["ada", "bob", "cy", "dee", "eve", "fay"]

    def test_order_by_and_limit_apply_after_concatenation(self, db):
        _seed(db)
        rows = db.execute("SELECT user_id, name FROM users",
                          order_by=[("user_id", "desc")], limit=2)
        assert [r["name"] for r in rows] == ["fay", "eve"]
        rows = db.execute("SELECT region, name FROM users",
                          order_by=[("region", "asc"), ("name", "desc")])
        assert [(r["region"], r["name"]) for r in rows] == [
            ("ap", "fay"), ("ap", "eve"), ("ap", "dee"),
            ("eu", "ada"), ("us", "cy"), ("us", "bob"),
        ]

    def test_order_by_puts_nulls_first_ascending(self, db):
        db.execute("INSERT INTO users (region, name) VALUES (:region, :name)",
                   {"region": "eu", "name": "nobody"})
        _seed(db)
        rows = db.execute("SELECT user_id FROM users", order_by=[("user_id", "asc")], limit=2)
        assert [r["user_id"] for r in rows] == [None, 1]
        rows = db.execute("SELECT user_id FROM users", order_by=[("user_id", "desc")])
        assert rows[-1]["user_id"] is None

    def test_order_by_validation(self, db):
        _seed(db)
        with pytest.raises(ValueError, match="asc"):
            db.execute("SELECT name FROM users", order_by=[("name", "sideways")])
        with pytest.raises(ShardingError, match="not in the result rows"):
            db.execute("SELECT name FROM users", order_by=[("user_id", "asc")])

    def test_sql_limit_is_per_shard(self, db):
        _seed(db)
        rows = db.execute("SELECT name FROM users ORDER BY user_id LIMIT 1")
        assert [r["name"] for r in rows] == ["ada", "bob", "dee"]
        assert db.execute("SELECT name FROM users ORDER BY user_id LIMIT 1", limit=1) == [
            {"name": "ada"}]

    def test_aggregates_come_back_once_per_shard(self, db):
        _seed(db)
        counts = db.execute("SELECT COUNT(*) AS n FROM users")
        assert [c["n"] for c in counts] == [1, 2, 3]

    def test_write_without_shard_raises(self, db):
        with pytest.raises(ShardingError, match="write could not be routed"):
            db.execute("INSERT INTO users (name) VALUES (:name)", {"name": "x"})
        with pytest.raises(ShardingError, match="write could not be routed"):
            db.execute("DELETE FROM users")
        with pytest.raises(ShardingError, match="write could not be routed"):
            db.execute("CREATE TABLE t (id INTEGER)")
        # Nothing reached any shard.
        assert db.execute("SELECT COUNT(*) AS n FROM users") == [{"n": 0}] * 3
        assert not db.table_exists("t")

    def test_all_shards_opts_a_write_in(self, db, shard_urls):
        _seed(db)
        db.execute("UPDATE users SET name = :name", {"name": "same"}, all_shards=True)
        for url in shard_urls.values():
            assert {r["name"] for r in _rows_on(url)} == {"same"}

    def test_return_id_cannot_fan_out(self, db):
        with pytest.raises(ShardingError, match="single shard"):
            db.execute("INSERT INTO users (name) VALUES (:name)", {"name": "x"},
                       return_id=True, all_shards=True)

    def test_table_exists(self, db):
        assert db.table_exists("users")
        assert not db.table_exists("missing")
        db.execute("CREATE TABLE only_a (id INTEGER)", shard="a")
        assert db.table_exists("only_a", shard="a")
        assert not db.table_exists("only_a", shard="b")
        assert not db.table_exists("only_a")

    def test_removed_begin_protocol_still_rejected(self, db):
        with pytest.raises(RuntimeError, match="transaction"):
            db.execute("BEGIN")


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

class TestTransaction:
    def test_pinned_by_first_statement_and_committed(self, db, shard_urls):
        with db.transaction():
            db.execute(INSERT_USER, {"region": "us", "user_id": 1, "name": "one"})
            # No shard key: stays on the pinned shard rather than raising.
            db.execute("UPDATE users SET name = :name", {"name": "renamed"})
            # Not visible elsewhere yet: the block owns the commit.
            assert _rows_on(shard_urls["b"]) == []
        assert [r["name"] for r in _rows_on(shard_urls["b"])] == ["renamed"]
        assert _rows_on(shard_urls["a"]) == []

    def test_statement_routing_elsewhere_raises_and_rolls_back(self, db, shard_urls):
        with pytest.raises(ShardingError, match="pinned to 'b'"):
            with db.transaction():
                db.execute(INSERT_USER, {"region": "us", "user_id": 1, "name": "one"})
                db.execute(INSERT_USER, {"region": "eu", "user_id": 2, "name": "two"})
        assert _rows_on(shard_urls["a"]) == []
        assert _rows_on(shard_urls["b"]) == []

    def test_explicit_shard(self, db, shard_urls):
        with db.transaction(shard="c"):
            db.execute("INSERT INTO users (name) VALUES (:name)", {"name": "pinned"})
            assert db.execute("SELECT name FROM users") == [{"name": "pinned"}]
        assert [r["name"] for r in _rows_on(shard_urls["c"])] == ["pinned"]
        with pytest.raises(ShardingError, match="pinned to 'c'"):
            with db.transaction(shard="c"):
                db.execute(INSERT_USER, {"region": "eu", "user_id": 1, "name": "x"})

    def test_exception_rolls_back(self, db, shard_urls):
        with pytest.raises(RuntimeError, match="boom"):
            with db.transaction():
                db.execute(INSERT_USER, {"region": "ap", "user_id": 1, "name": "x"})
                raise RuntimeError("boom")
        assert _rows_on(shard_urls["c"]) == []

    def test_fan_out_is_refused_inside(self, db):
        with pytest.raises(ShardingError, match="all_shards"):
            with db.transaction(shard="a"):
                db.execute("DELETE FROM users", all_shards=True)
        with pytest.raises(ShardingError, match="first statement"):
            with db.transaction():
                db.execute("SELECT name FROM users")

    def test_default_shard_pins_an_unrouted_first_statement(self, shard_urls):
        router = KeyRouter("region", {"eu": "a"})
        db = ShardedDatabaseManager(shard_urls, router=router, default_shard="c")
        with db:
            db.execute(CREATE_USERS, all_shards=True)
            with db.transaction():
                db.execute("INSERT INTO users (name) VALUES (:name)", {"name": "g"})
                with pytest.raises(ShardingError, match="pinned to 'c'"):
                    db.execute(INSERT_USER, {"region": "eu", "user_id": 1, "name": "x"})

    def test_nested_joins_outer(self, db, shard_urls):
        with db.transaction():
            db.execute(INSERT_USER, {"region": "eu", "user_id": 1, "name": "outer"})
            with db.transaction():
                db.execute(INSERT_USER, {"region": "eu", "user_id": 2, "name": "inner"})
            with pytest.raises(ShardingError, match="pinned to 'a'"):
                with db.transaction(shard="b"):
                    pass
            assert _rows_on(shard_urls["a"]) == []
        assert [r["name"] for r in _rows_on(shard_urls["a"])] == ["outer", "inner"]

    def test_empty_block_opens_nothing_and_state_resets(self, db):
        with db.transaction():
            pass
        assert not db._in_transaction
        # Routing is back to normal afterwards.
        db.execute(INSERT_USER, {"region": "eu", "user_id": 1, "name": "x"})
        db.execute(INSERT_USER, {"region": "us", "user_id": 2, "name": "y"})
        assert len(db.execute("SELECT name FROM users")) == 2


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------

class TestAsync:
    async def test_execute_async_routes_and_fans_out(self, db):
        await db.execute_async(INSERT_USER, {"region": "eu", "user_id": 1, "name": "ada"})
        await db.execute_async(INSERT_USER, {"region": "ap", "user_id": 9, "name": "zed"})
        new_id = await db.execute_async(
            INSERT_USER, {"region": "ap", "user_id": 8, "name": "yan"}, return_id=True)
        assert new_id == 2
        rows = await db.execute_async("SELECT name FROM users WHERE region = :region",
                                      {"region": "ap"})
        assert [r["name"] for r in rows] == ["zed", "yan"]
        rows = await db.execute_async("SELECT user_id, name FROM users",
                                      order_by=[("user_id", "desc")], limit=2)
        assert [r["name"] for r in rows] == ["zed", "yan"]
        with pytest.raises(ShardingError, match="write could not be routed"):
            await db.execute_async("DELETE FROM users")

    async def test_fetch_one_async(self, db):
        _seed(db)
        row = await db.fetch_one_async(
            "SELECT name FROM users WHERE region = :region AND user_id = :user_id",
            {"region": "us", "user_id": 3})
        assert row == {"name": "cy"}
        row = await db.fetch_one_async("SELECT name FROM users WHERE name = :name", {"name": "fay"})
        assert row == {"name": "fay"}
        assert await db.fetch_one_async("SELECT name FROM users WHERE name = :name",
                                        {"name": "nobody"}) is None

    async def test_execute_script(self, db):
        script = """
        CREATE TABLE notes (id SERIAL PRIMARY KEY, body TEXT);
        INSERT INTO notes (body) VALUES ('hello');
        """
        with pytest.raises(ShardingError, match="write could not be routed"):
            await db.execute_script(script)
        result = await db.execute_script(script, all_shards=True)
        assert result.success
        assert db.table_exists("notes")
        assert len(db.execute("SELECT body FROM notes")) == 3

        result = await db.execute_script("CREATE TABLE one (id INTEGER)", shard="b")
        assert result.success
        assert db.table_exists("one", shard="b") and not db.table_exists("one", shard="a")

        failed = await db.execute_script("INSERT INTO nope VALUES (1)", all_shards=True)
        assert not failed.success
        assert failed.error_message.startswith("shard 'a':")

    async def test_close(self, db):
        await db.close()
        with pytest.raises(RuntimeError, match="not connected"):
            db.execute("SELECT 1", shard="a")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_context_manager_connects_and_disconnects(self, shard_urls):
        with ShardedDatabaseManager(shard_urls) as db:
            assert db.execute("SELECT 1 AS one") == [{"one": 1}] * 3
        with pytest.raises(RuntimeError, match="not connected"):
            db.execute("SELECT 1", shard="a")

    def test_connect_failure_disconnects_the_rest(self, shard_urls):
        shards = dict(shard_urls)
        shards["bad"] = "postgresql://nouser:nopass@127.0.0.1:1/nodb"
        db = ShardedDatabaseManager(shards, connect_timeout=2)
        assert db.connect() is False
        # The SQLite shards that had connected were closed again, so the
        # object is not left half-open.
        for name in SHARDS:
            with pytest.raises(RuntimeError, match="not connected"):
                db.execute("SELECT 1", shard=name)
        db.disconnect()
