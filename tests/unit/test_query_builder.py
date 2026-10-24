"""
Query builder: SQL/parameter snapshots and the SQLite end-to-end path.

The snapshots pin the canonical SQL for every clause, since that text is
what the manager translates per dialect. Live backends are covered by
tests/integration/test_query_builder.py.
"""

import sqlite3

import pytest

from nexusql import DatabaseManager, Q, QueryError
from nexusql.query import Col, Expr, Param, Raw, Select

C = Q.col


def where(expr: Expr):
    """The WHERE text and params of ``SELECT * FROM t WHERE <expr>``."""
    sql, params = Q.select("t").where(expr).build()
    prefix = "SELECT * FROM t WHERE "
    assert sql.startswith(prefix)
    return sql[len(prefix):], params


# ---------------------------------------------------------------------------
# SELECT clauses
# ---------------------------------------------------------------------------

class TestSelect:
    def test_star_by_default(self):
        assert Q.select("users").build() == ("SELECT * FROM users", {})

    def test_full_chain(self):
        q = (
            Q.select("users").columns("id", "email")
            .where(C("age") >= Q.p("min_age", 18))
            .order_by("-created_at").limit(10).offset(20)
        )
        assert q.build() == (
            "SELECT id, email FROM users WHERE age >= :min_age "
            "ORDER BY created_at DESC LIMIT 10 OFFSET 20",
            {"min_age": 18},
        )

    def test_distinct(self):
        assert Q.select("users").columns("country").distinct().build() == (
            "SELECT DISTINCT country FROM users", {}
        )

    def test_joins(self):
        q = (
            Q.select("users", alias="u").columns("u.id", "o.amount")
            .join("orders", on=C("o.user_id") == C("u.id"), kind="left", alias="o")
            .join("plans", on=C("plans.id") == C("u.plan_id"))
            .join("regions", kind="cross")
        )
        assert q.build() == (
            "SELECT u.id, o.amount FROM users AS u "
            "LEFT JOIN orders AS o ON o.user_id = u.id "
            "INNER JOIN plans ON plans.id = u.plan_id "
            "CROSS JOIN regions",
            {},
        )

    def test_join_on_with_parameter(self):
        q = Q.select("a").join(
            "b", on=(C("b.a_id") == C("a.id")) & (C("b.kind") == "x"), kind="inner"
        )
        assert q.build() == (
            "SELECT * FROM a INNER JOIN b ON b.a_id = a.id AND b.kind = :p1", {"p1": "x"}
        )

    def test_group_by_having(self):
        q = (
            Q.select("orders").columns("user_id", Q.raw("COUNT(*)").as_("n"))
            .group_by("user_id").having(Q.raw("COUNT(*)") > 2)
            .having(Q.raw("SUM(amount)") >= Q.p("min_total", 100))
        )
        assert q.build() == (
            "SELECT user_id, COUNT(*) AS n FROM orders GROUP BY user_id "
            "HAVING COUNT(*) > :p1 AND SUM(amount) >= :min_total",
            {"p1": 2, "min_total": 100},
        )

    def test_order_by_variants(self):
        q = Q.select("t").order_by("-a", "+b", "c", C("d").desc(), C("e").asc(), Q.raw("LOWER(f)"))
        assert q.build()[0] == "SELECT * FROM t ORDER BY a DESC, b, c, d DESC, e, LOWER(f)"

    def test_star_variants(self):
        assert Q.select("t").columns("*", "u.*").build()[0] == "SELECT *, u.* FROM t"

    def test_where_calls_are_anded(self):
        q = Q.select("t").where((C("a") == 1) | (C("b") == 2)).where(C("c") == 3)
        assert q.build() == (
            "SELECT * FROM t WHERE (a = :p1 OR b = :p2) AND c = :p3", {"p1": 1, "p2": 2, "p3": 3}
        )

    def test_quoted_identifiers(self):
        q = Q.select(C("Users", quote=True)).columns(C("public.Email", quote=True))
        assert q.build()[0] == 'SELECT "public"."Email" FROM "Users"'

    def test_limit_and_offset_are_inlined_integers(self):
        # Digits rather than parameters: the SQL Server translation rewrites
        # LIMIT/OFFSET by matching literal numbers.
        sql, params = Q.select("t").order_by("id").limit(5).offset(0).build()
        assert sql.endswith("ORDER BY id LIMIT 5 OFFSET 0")
        assert params == {}


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

class TestExpressions:
    @pytest.mark.parametrize(
        "expr, sql, params",
        [
            (C("a") == 1, "a = :p1", {"p1": 1}),
            (C("a") != 1, "a <> :p1", {"p1": 1}),
            (C("a") < 1, "a < :p1", {"p1": 1}),
            (C("a") <= 1, "a <= :p1", {"p1": 1}),
            (C("a") > 1, "a > :p1", {"p1": 1}),
            (C("a") >= 1, "a >= :p1", {"p1": 1}),
            (C("a").like("x%"), "a LIKE :p1", {"p1": "x%"}),
            (C("a").not_like("x%"), "a NOT LIKE :p1", {"p1": "x%"}),
            (C("a").ilike("x%"), "LOWER(a) LIKE LOWER(:p1)", {"p1": "x%"}),
            (C("a").in_([1, 2, 3]), "a IN (:p1, :p2, :p3)", {"p1": 1, "p2": 2, "p3": 3}),
            (C("a").not_in(["x"]), "a NOT IN (:p1)", {"p1": "x"}),
            (C("a").in_([]), "1 = 0", {}),
            (C("a").not_in([]), "1 = 1", {}),
            (C("a").is_null(), "a IS NULL", {}),
            (C("a").is_not_null(), "a IS NOT NULL", {}),
            (C("a") == None, "a IS NULL", {}),  # noqa: E711
            (C("a") != None, "a IS NOT NULL", {}),  # noqa: E711
            (C("a").between(1, 5), "a BETWEEN :p1 AND :p2", {"p1": 1, "p2": 5}),
            (C("a") == C("b.c"), "a = b.c", {}),
            (C("a") == True, "a = :p1", {"p1": True}),  # noqa: E712
            (C("a") + 1 > 2, "(a + :p1) > :p2", {"p1": 1, "p2": 2}),
            (C("a") - C("b"), "a - b", {}),
            (C("a") * 2, "a * :p1", {"p1": 2}),
            (C("a") / 2, "a / :p1", {"p1": 2}),
        ],
    )
    def test_operators(self, expr, sql, params):
        assert where(expr) == (sql, params)

    def test_boolean_combinators_and_precedence(self):
        expr = ((C("a") == 1) & (C("b") == 2)) | ~C("c").is_null()
        assert where(expr) == ("(a = :p1 AND b = :p2) OR NOT (c IS NULL)", {"p1": 1, "p2": 2})

        expr = (C("a") == 1) & ((C("b") == 2) | (C("c") == 3)) & (C("d") == 4)
        assert where(expr) == (
            "a = :p1 AND (b = :p2 OR c = :p3) AND d = :p4",
            {"p1": 1, "p2": 2, "p3": 3, "p4": 4},
        )

    def test_not_of_compound(self):
        assert where(~((C("a") == 1) | (C("b") == 2))) == (
            "NOT (a = :p1 OR b = :p2)", {"p1": 1, "p2": 2}
        )

    def test_explicit_and_auto_parameters_mix(self):
        expr = (C("a") == Q.p("p1", "explicit")) & (C("b") == "auto") & (C("c") == Q.p("p2", 3))
        # The auto-named value skips p1 and p2, which Q.p() already took.
        assert where(expr) == (
            "a = :p1 AND b = :p3 AND c = :p2", {"p1": "explicit", "p3": "auto", "p2": 3}
        )

    def test_same_explicit_parameter_twice_is_fine(self):
        expr = (C("a") == Q.p("v", 1)) | (C("b") == Q.p("v", 1))
        assert where(expr) == ("a = :v OR b = :v", {"v": 1})

    def test_conflicting_explicit_parameter_values_raise(self):
        expr = (C("a") == Q.p("v", 1)) | (C("b") == Q.p("v", 2))
        with pytest.raises(QueryError, match="two different values"):
            Q.select("t").where(expr).build()

    def test_raw_with_parameters(self):
        expr = Q.raw("created_at > NOW() - :days * INTERVAL '1 day'", days=7)
        assert where(expr) == ("created_at > NOW() - :days * INTERVAL '1 day'", {"days": 7})

    def test_raw_is_comparable(self):
        assert where(Q.raw("LENGTH(name)") > 3) == ("LENGTH(name) > :p1", {"p1": 3})

    def test_alias(self):
        sql, _ = Q.select("t").columns(C("a").as_("b"), Q.raw("MAX(x)").as_("mx")).build()
        assert sql == "SELECT a AS b, MAX(x) AS mx FROM t"

    def test_expressions_are_not_booleans(self):
        with pytest.raises(TypeError, match="cannot be used as booleans"):
            bool(C("a") == 1)
        with pytest.raises(TypeError):
            # ``and`` would silently drop the left operand.
            (C("a") == 1) and (C("b") == 2)

    def test_values_are_parameters_never_text(self):
        hostile = "1; DROP TABLE users; --"
        sql, params = where(C("id") == hostile)
        assert sql == "id = :p1"
        assert params == {"p1": hostile}
        assert "DROP" not in sql


# ---------------------------------------------------------------------------
# INSERT / UPDATE / DELETE
# ---------------------------------------------------------------------------

class TestWrites:
    def test_insert(self):
        assert Q.insert("users").values(email="a@b.c", name="A").build() == (
            "INSERT INTO users (email, name) VALUES (:p1, :p2)", {"p1": "a@b.c", "p2": "A"}
        )

    def test_insert_mapping_and_overrides(self):
        q = Q.insert("t").values({"a": 1, "b": 2}).values(b=3, c=Q.raw("NOW()"))
        assert q.build() == (
            "INSERT INTO t (a, b, c) VALUES (:p1, :p2, NOW())", {"p1": 1, "p2": 3}
        )

    def test_insert_returning(self):
        q = Q.insert("users").values(email="x").returning("id", "created_at")
        assert q.build()[0] == "INSERT INTO users (email) VALUES (:p1) RETURNING id, created_at"

    def test_update(self):
        q = Q.update("users").set(name="B", views=C("views") + 1).where(C("id") == 7)
        assert q.build() == (
            "UPDATE users SET name = :p1, views = views + :p2 WHERE id = :p3",
            {"p1": "B", "p2": 1, "p3": 7},
        )

    def test_update_all_rows_is_explicit(self):
        assert Q.update("t").set(flag=0).all_rows().build() == ("UPDATE t SET flag = :p1", {"p1": 0})

    def test_delete(self):
        assert Q.delete("users").where(C("id").in_([1, 2])).build() == (
            "DELETE FROM users WHERE id IN (:p1, :p2)", {"p1": 1, "p2": 2}
        )

    def test_delete_all_rows_is_explicit(self):
        assert Q.delete("t").all_rows().build() == ("DELETE FROM t", {})

    def test_str_is_the_sql(self):
        assert str(Q.select("t").where(C("a") == 1)) == "SELECT * FROM t WHERE a = :p1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

BAD_IDENTIFIERS = [
    "users; DROP TABLE x",
    "na me",
    'a"b',
    "a'b",
    "a.b.c",
    "1abc",
    "",
    "a-b",
    "users--",
    "a.",
    ".a",
    "col)",
]


class TestValidation:
    @pytest.mark.parametrize("name", BAD_IDENTIFIERS)
    def test_bad_identifiers_raise_everywhere(self, name):
        with pytest.raises(QueryError, match="invalid"):
            Q.col(name)
        with pytest.raises(QueryError, match="invalid"):
            Q.select(name)
        with pytest.raises(QueryError, match="invalid"):
            Q.select("t").columns(name)
        with pytest.raises(QueryError, match="invalid"):
            Q.select("t").order_by(name)
        with pytest.raises(QueryError, match="invalid"):
            Q.insert("t").values({name: 1})
        with pytest.raises(QueryError, match="invalid"):
            Q.update("t").set({name: 1})
        with pytest.raises(QueryError, match="invalid"):
            Q.select("t").join(name, on=C("a") == C("b"))

    def test_non_string_identifier(self):
        with pytest.raises(QueryError, match="must be a string"):
            Q.col(5)  # type: ignore[arg-type]

    def test_alias_must_be_simple(self):
        with pytest.raises(QueryError, match="alias"):
            Q.select("t", alias="a.b")
        with pytest.raises(QueryError, match="alias"):
            C("a").as_("b c")
        with pytest.raises(QueryError, match="alias"):
            Q.select("t").join("u", on=C("a") == C("b"), alias="x;")

    def test_parameter_names_are_validated(self):
        with pytest.raises(QueryError, match="parameter name"):
            Q.p("bad name", 1)
        with pytest.raises(QueryError, match="parameter name"):
            Q.raw("x", **{"a-b": 1})

    @pytest.mark.parametrize("value", [-1, 1.5, "10", True, None])
    def test_limit_offset_must_be_non_negative_ints(self, value):
        with pytest.raises(QueryError, match="non-negative int"):
            Q.select("t").limit(value)
        with pytest.raises(QueryError, match="non-negative int"):
            Q.select("t").offset(value)

    def test_join_validation(self):
        with pytest.raises(QueryError, match="unknown join kind"):
            Q.select("t").join("u", on=C("a") == C("b"), kind="sideways")
        with pytest.raises(QueryError, match="needs an ON"):
            Q.select("t").join("u")
        with pytest.raises(QueryError, match="cross join takes no ON"):
            Q.select("t").join("u", on=C("a") == C("b"), kind="cross")
        with pytest.raises(QueryError, match="expected an expression"):
            Q.select("t").join("u", on="a = b")

    def test_where_requires_expression(self):
        with pytest.raises(QueryError, match="expected an expression"):
            Q.select("t").where("id = 1")
        with pytest.raises(QueryError, match="expected an expression"):
            (C("a") == 1) & "b = 2"

    def test_write_guards(self):
        with pytest.raises(QueryError, match="UPDATE without WHERE"):
            Q.update("t").set(a=1).build()
        with pytest.raises(QueryError, match="DELETE without WHERE"):
            Q.delete("t").build()
        with pytest.raises(QueryError, match="needs at least one column"):
            Q.insert("t").build()
        with pytest.raises(QueryError, match="needs at least one column"):
            Q.update("t").where(C("a") == 1).build()

    def test_raw_requires_text(self):
        with pytest.raises(QueryError, match="SQL string"):
            Q.raw(5)  # type: ignore[arg-type]

    def test_query_error_is_a_value_error(self):
        assert issubclass(QueryError, ValueError)


class TestImmutability:
    def test_chaining_never_mutates(self):
        base = Q.select("users").columns("id")
        filtered = base.where(C("active") == True)  # noqa: E712
        paged = filtered.limit(5)
        assert base.build() == ("SELECT id FROM users", {})
        assert filtered.build() == ("SELECT id FROM users WHERE active = :p1", {"p1": True})
        assert paged.build()[0].endswith("LIMIT 5")
        assert isinstance(paged, Select)

    def test_build_is_repeatable(self):
        q = Q.select("t").where((C("a") == 1) & (C("b") == Q.p("x", 2)))
        assert q.build() == q.build()


# ---------------------------------------------------------------------------
# SQLite end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def db(temp_db_path):
    manager = DatabaseManager(f"sqlite:///{temp_db_path}")
    assert manager.connect()
    manager.execute(
        "CREATE TABLE users (id SERIAL PRIMARY KEY, email TEXT NOT NULL, "
        "name TEXT, age INTEGER)"
    )
    yield manager
    manager.disconnect()


class TestSqlite:
    def seed(self, db):
        ids = []
        for email, name, age in [("a@x.io", "Ann", 30), ("b@x.io", "Bob", 20), ("c@x.io", None, 40)]:
            ids.append(
                Q.insert("users").values(email=email, name=name, age=age)
                .execute(db, return_id=True)
            )
        return ids

    def test_insert_select_update_delete(self, db):
        ids = self.seed(db)
        assert ids == [1, 2, 3]

        q = (
            Q.select("users").columns("id", "email")
            .where(C("age") >= Q.p("min_age", 25)).order_by("-age").limit(1).offset(1)
        )
        assert db.run(q) == [{"id": 1, "email": "a@x.io"}]
        assert q.execute(db) == db.run(q)
        assert db.run_one(q) == {"id": 1, "email": "a@x.io"}
        assert q.fetch_one(db) == db.run_one(q)

        db.run(Q.update("users").set(name="Cy", age=C("age") + 1).where(C("name").is_null()))
        assert db.run_one(Q.select("users").columns("name", "age").where(C("id") == 3)) == {
            "name": "Cy", "age": 41
        }

        db.run(Q.delete("users").where(C("email").like("b%")))
        remaining = db.run(Q.select("users").columns(Q.raw("COUNT(*)").as_("n")))
        assert remaining == [{"n": 2}]

    def test_predicates_run(self, db):
        self.seed(db)
        q = Q.select("users").columns("id").order_by("id")
        assert db.run(q.where(C("id").in_([1, 3]))) == [{"id": 1}, {"id": 3}]
        assert db.run(q.where(C("id").not_in([1, 3]))) == [{"id": 2}]
        assert db.run(q.where(C("id").in_([]))) == []
        assert db.run(q.where(C("age").between(25, 35))) == [{"id": 1}]
        assert db.run(q.where(C("name").ilike("%ANN%"))) == [{"id": 1}]
        assert db.run(q.where(~(C("name") == "Ann") & C("name").is_not_null())) == [{"id": 2}]
        assert db.run(q.where(C("age") == None)) == []  # noqa: E711

    def test_join_group_having(self, db):
        self.seed(db)
        db.execute("CREATE TABLE orders (id SERIAL PRIMARY KEY, user_id INTEGER, amount INTEGER)")
        for user_id, amount in [(1, 10), (1, 20), (2, 5)]:
            db.run(Q.insert("orders").values(user_id=user_id, amount=amount))
        q = (
            Q.select("users", alias="u").columns("u.id", Q.raw("COUNT(o.id)").as_("n"))
            .join("orders", on=C("o.user_id") == C("u.id"), kind="left", alias="o")
            .group_by("u.id").having(Q.raw("COUNT(o.id)") > 1).order_by("u.id")
        )
        assert db.run(q) == [{"id": 1, "n": 2}]

    def test_returning_on_sqlite(self, db):
        if sqlite3.sqlite_version_info < (3, 35):
            pytest.skip("SQLite without RETURNING")
        rows = db.run(Q.insert("users").values(email="r@x.io").returning("id", "email"))
        assert rows == [{"id": 1, "email": "r@x.io"}]
        # return_id on a statement that already has RETURNING yields its first column
        assert Q.insert("users").values(email="s@x.io").returning("email").execute(
            db, return_id=True
        ) == "s@x.io"

    def test_hostile_values_are_stored_verbatim(self, db):
        hostile = "1; DROP TABLE users; --"
        new_id = Q.insert("users").values(email=hostile, name="' OR 1=1 --").execute(
            db, return_id=True
        )
        assert db.table_exists("users")
        row = db.run_one(Q.select("users").columns("email", "name").where(C("email") == hostile))
        assert row == {"email": hostile, "name": "' OR 1=1 --"}
        assert db.run(Q.select("users").columns("id").where(C("name") == "' OR 1=1 --")) == [
            {"id": new_id}
        ]

    def test_hostile_identifiers_never_reach_the_database(self, db):
        with pytest.raises(QueryError):
            Q.select("users; DROP TABLE users")
        with pytest.raises(QueryError):
            Q.select("users").columns('email" FROM users --')
        assert db.table_exists("users")
