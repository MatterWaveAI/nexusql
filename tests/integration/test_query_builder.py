"""
Query builder statements run against live backends.

The builder emits canonical PostgreSQL SQL with ``:name`` parameters, so the
same statement must translate and run on SQLite, PostgreSQL, MySQL and SQL
Server through the existing execute()/fetch_one() paths. Backends that are
not reachable are skipped at collection (see conftest).
"""

import os
import uuid

import pytest

from nexusql import DatabaseManager, Q, QueryError

BACKENDS = ["sqlite", "postgresql", "mysql", "mssql"]
C = Q.col


def table_names() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"qb_u_{suffix}", f"qb_o_{suffix}"


def create_tables(execute, users: str, orders: str) -> None:
    execute(
        f"CREATE TABLE {users} (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL, "
        "name VARCHAR(50), age INTEGER)"
    )
    execute(
        f"CREATE TABLE {orders} (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, "
        "amount INTEGER NOT NULL)"
    )


SEED_USERS = [("ann@example.com", "Ann", 30), ("bob@example.com", "Bob", 20), ("cy@example.com", None, 40)]
SEED_ORDERS = [(0, 10), (0, 20), (1, 5)]  # (index into the seeded users, amount)


@pytest.fixture
def tables(db_manager):
    users, orders = table_names()
    create_tables(db_manager.execute, users, orders)
    yield users, orders
    for name in (orders, users):
        try:
            db_manager.execute(f"DROP TABLE {name}")
        except Exception:
            pass


def seed(db, users, orders):
    ids = [
        Q.insert(users).values(email=email, name=name, age=age).execute(db, return_id=True)
        for email, name, age in SEED_USERS
    ]
    for index, amount in SEED_ORDERS:
        db.run(Q.insert(orders).values(user_id=ids[index], amount=amount))
    return ids


@pytest.mark.parametrize("db_manager", BACKENDS, indirect=True)
class TestQueryBuilder:
    def test_insert_and_select(self, db_manager, tables):
        users, orders = tables
        ids = seed(db_manager, users, orders)
        assert all(isinstance(i, int) for i in ids)
        assert ids[0] < ids[1] < ids[2]

        q = (
            Q.select(users).columns("id", "email")
            .where(C("age") >= Q.p("min_age", 25))
            .order_by("-age").limit(1).offset(1)
        )
        assert db_manager.run(q) == [{"id": ids[0], "email": "ann@example.com"}]
        assert db_manager.run_one(q) == {"id": ids[0], "email": "ann@example.com"}
        assert q.execute(db_manager) == db_manager.run(q)
        assert q.fetch_one(db_manager) == db_manager.run_one(q)
        assert db_manager.run_one(q.where(C("id") == -1)) is None

    def test_join_group_by_having(self, db_manager, tables):
        users, orders = tables
        ids = seed(db_manager, users, orders)
        q = (
            Q.select(users, alias="u").columns("u.id", Q.raw("COUNT(o.id)").as_("n"))
            .join(orders, on=C("o.user_id") == C("u.id"), kind="left", alias="o")
            .group_by("u.id").having(Q.raw("COUNT(o.id)") > 1).order_by("u.id")
        )
        assert db_manager.run(q) == [{"id": ids[0], "n": 2}]

        totals = (
            Q.select(orders).columns("user_id", Q.raw("SUM(amount)").as_("total"))
            .group_by("user_id").order_by("user_id")
        )
        rows = db_manager.run(totals)
        assert [(r["user_id"], int(r["total"])) for r in rows] == [(ids[0], 30), (ids[1], 5)]

    def test_update_and_delete(self, db_manager, tables):
        users, orders = tables
        ids = seed(db_manager, users, orders)

        db_manager.run(
            Q.update(users).set(name="Cy", age=C("age") + 1).where(C("name").is_null())
        )
        assert db_manager.run_one(
            Q.select(users).columns("name", "age").where(C("id") == ids[2])
        ) == {"name": "Cy", "age": 41}

        db_manager.run(Q.delete(orders).where(C("user_id").in_([ids[0], ids[1]])))
        count = db_manager.run_one(Q.select(orders).columns(Q.raw("COUNT(*)").as_("n")))
        assert count["n"] == 0

        db_manager.run(Q.delete(users).where(C("email").like("bob%")))
        remaining = db_manager.run(Q.select(users).columns("id").order_by("id"))
        assert [r["id"] for r in remaining] == [ids[0], ids[2]]

    def test_predicates(self, db_manager, tables):
        users, orders = tables
        ids = seed(db_manager, users, orders)
        q = Q.select(users).columns("id").order_by("id")

        def run(expr):
            return [r["id"] for r in db_manager.run(q.where(expr))]

        assert run(C("id").in_([ids[0], ids[2]])) == [ids[0], ids[2]]
        assert run(C("id").not_in([ids[0], ids[2]])) == [ids[1]]
        assert run(C("id").in_([])) == []
        assert run(C("id").not_in([])) == ids
        assert run(C("age").between(25, 35)) == [ids[0]]
        assert run(C("name").ilike("%ANN%")) == [ids[0]]
        assert run(C("name").not_like("A%") & C("name").is_not_null()) == [ids[1]]
        assert run(C("name") == None) == [ids[2]]  # noqa: E711
        assert run((C("age") < 25) | (C("age") > 35)) == [ids[1], ids[2]]
        assert run(~(C("age") == 30)) == [ids[1], ids[2]]

    def test_distinct(self, db_manager, tables):
        users, orders = tables
        ids = seed(db_manager, users, orders)
        q = Q.select(orders).columns("user_id").distinct().order_by("user_id")
        assert [r["user_id"] for r in db_manager.run(q)] == [ids[0], ids[1]]

    def test_hostile_values_arrive_as_parameters(self, db_manager, tables):
        users, orders = tables
        hostile_email = f"1; DROP TABLE {users}; --"
        hostile_name = "' OR 1=1 --"
        new_id = Q.insert(users).values(email=hostile_email, name=hostile_name, age=1).execute(
            db_manager, return_id=True
        )
        assert db_manager.table_exists(users)
        row = db_manager.run_one(
            Q.select(users).columns("id", "email", "name").where(C("email") == hostile_email)
        )
        assert row == {"id": new_id, "email": hostile_email, "name": hostile_name}
        # The quote in the name is data, not syntax: it matches only itself.
        assert db_manager.run(Q.select(users).columns("id").where(C("name") == hostile_name)) == [
            {"id": new_id}
        ]
        assert db_manager.run(Q.select(users).columns("id").where(C("name") == "' OR 1=1")) == []

    def test_hostile_identifiers_are_rejected_before_the_database(self, db_manager, tables):
        users, orders = tables
        seed(db_manager, users, orders)
        for bad in [f"{users}; DROP TABLE {orders}", f"{users} --", f'{users}"', "a b"]:
            with pytest.raises(QueryError, match="invalid"):
                Q.select(bad)
            with pytest.raises(QueryError, match="invalid"):
                Q.select(users).columns(bad)
            with pytest.raises(QueryError, match="invalid"):
                Q.select(users).order_by(bad)
        assert db_manager.table_exists(users) and db_manager.table_exists(orders)
        assert db_manager.run_one(Q.select(orders).columns(Q.raw("COUNT(*)").as_("n")))["n"] == 3


def test_postgres_returning(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect()
    users, orders = table_names()
    try:
        create_tables(db.execute, users, orders)
        rows = db.run(Q.insert(users).values(email="r@example.com").returning("id", "email"))
        assert rows == [{"id": 1, "email": "r@example.com"}]
        # return_id on a statement that already has RETURNING yields its first column
        value = Q.insert(users).values(email="s@example.com").returning("email").execute(
            db, return_id=True
        )
        assert value == "s@example.com"
    finally:
        db.execute(f"DROP TABLE IF EXISTS {orders}")
        db.execute(f"DROP TABLE IF EXISTS {users}")
        db.disconnect()


# ---------------------------------------------------------------------------
# Async PostgreSQL backend
# ---------------------------------------------------------------------------

ASYNC_URL = os.getenv(
    "TEST_POSTGRESQL_URL", "postgresql://postgres:mwtest@127.0.0.1:15432/mwtest"
)


@pytest.fixture
async def async_backend():
    from nexusql import PSYCOPG_AVAILABLE
    if not PSYCOPG_AVAILABLE:
        pytest.skip("psycopg not installed")
    from nexusql import AsyncPostgresBackend

    backend = AsyncPostgresBackend(ASYNC_URL, max_pool_size=4)
    try:
        if not await backend.connect():
            pytest.skip("PostgreSQL not reachable for async tests")
    except Exception as exc:
        pytest.skip(f"PostgreSQL not reachable for async tests: {exc}")
    yield backend
    await backend.disconnect()


async def test_async_backend_run_and_run_one(async_backend):
    users, orders = table_names()
    try:
        await async_backend.execute(
            f"CREATE TABLE {users} (id SERIAL PRIMARY KEY, email VARCHAR(100) NOT NULL, age INTEGER)"
        )
        first = await async_backend.run(
            Q.insert(users).values(email="a@example.com", age=30), return_id=True
        )
        second = await async_backend.run(
            Q.insert(users).values(email="b@example.com", age=20).returning("id")
        )
        assert first == 1 and second == [{"id": 2}]

        q = Q.select(users).columns("id", "email").where(C("age") >= 25).order_by("-age")
        assert await async_backend.run(q) == [{"id": 1, "email": "a@example.com"}]
        assert await async_backend.run_one(q) == {"id": 1, "email": "a@example.com"}
        assert await async_backend.run_one(q.where(C("id") == -1)) is None

        async with async_backend.transaction():
            await async_backend.run(Q.update(users).set(age=C("age") + 1).where(C("id") == 1))
            await async_backend.run(Q.delete(users).where(C("id") == 2))
        rows = await async_backend.run(Q.select(users).columns("id", "age").order_by("id"))
        assert rows == [{"id": 1, "age": 31}]
    finally:
        await async_backend.execute(f"DROP TABLE IF EXISTS {users}")
