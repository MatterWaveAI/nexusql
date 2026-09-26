"""
PostgreSQL is the canonical source dialect, so a query written against it must
mean the same thing on every backend.

These tests cover the two ways that used to break: ``::`` casts being read as
``:parameters``, and one parameter name matching the prefix of another.
"""

import re
import tempfile

import pytest

from nexusql import DatabaseManager, ConnectionConfig, DatabaseType
from nexusql.manager import PARAM_TOKEN

ALL_TYPES = [
    DatabaseType.SQLITE,
    DatabaseType.POSTGRESQL,
    DatabaseType.MYSQL,
    DatabaseType.MSSQL,
]


def manager_for(db_type):
    return DatabaseManager(
        ConnectionConfig(database_type=db_type, database_url="scheme://host/db")
    )


class TestTokenPattern:
    @pytest.mark.parametrize("query,expected", [
        ("SELECT :id", ["id"]),
        ("SELECT :id, :id_org", ["id", "id_org"]),
        ("SELECT a::text WHERE id = :id", ["id"]),
        ("SELECT a::int, b::bigint WHERE id = :id", ["id"]),
        ("SELECT a::numeric, b::date, c::boolean, :id", ["id"]),
        ("SELECT '{}'::jsonb, :id", ["id"]),
        ("SELECT :a, :b, :c", ["a", "b", "c"]),
        ("SELECT * FROM t", []),
    ])
    def test_finds_only_real_parameters(self, query, expected):
        assert re.findall(PARAM_TOKEN, query) == expected


class TestCastsAreNotParameters:
    """A ::cast must never be mistaken for a :parameter on any backend."""

    @pytest.mark.parametrize("db_type", ALL_TYPES)
    @pytest.mark.parametrize("cast", [
        "::int", "::bigint", "::text", "::varchar", "::jsonb", "::json",
        "::uuid", "::numeric", "::date", "::timestamp", "::boolean",
    ])
    def test_cast_survives_or_is_stripped_but_never_bound(self, db_type, cast):
        db = manager_for(db_type)
        query = f"SELECT val{cast} AS v FROM t WHERE id = :id"
        translated = db._translate_sql(query)
        converted, params = db._convert_params(translated, {"id": 1})

        # The one real parameter was bound, and nothing else was.
        if db_type == DatabaseType.POSTGRESQL:
            assert converted.count("%(id)s") == 1
            assert "%(" not in converted.replace("%(id)s", "")
        else:
            assert len(params) == 1
            assert params[0] == 1

    @pytest.mark.parametrize("db_type", ALL_TYPES)
    def test_non_postgres_backends_strip_casts(self, db_type):
        db = manager_for(db_type)
        translated = db._translate_sql("SELECT a::int FROM t")
        if db_type == DatabaseType.POSTGRESQL:
            assert "::int" in translated
        else:
            # Other dialects have no ::cast syntax, so it must be removed
            # rather than passed through to the driver.
            assert "::" not in translated


class TestPrefixNamesAreDistinct:
    """:id must not match the start of :id_org."""

    @pytest.mark.parametrize("db_type", ALL_TYPES)
    def test_prefix_parameter_not_corrupted(self, db_type):
        db = manager_for(db_type)
        query = "SELECT * FROM t WHERE a = :id AND b = :id_org"
        converted, params = db._convert_params(query, {"id": 1, "id_org": 2})

        if db_type == DatabaseType.POSTGRESQL:
            assert "%(id)s" in converted
            assert "%(id_org)s" in converted
        else:
            # Positional: values ordered by appearance in the query.
            assert list(params) == [1, 2]


class TestEndToEndOnSqlite:
    """The bug as a caller would hit it."""

    @pytest.fixture
    def db(self):
        d = tempfile.mkdtemp()
        config = ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url=f"sqlite:///{d}/parity.db",
        )
        manager = DatabaseManager(config)
        manager.connect()
        manager.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n TEXT)")
        manager.execute("INSERT INTO t (n) VALUES ('x')")
        yield manager
        manager.disconnect()

    def test_cast_and_parameter_together(self, db):
        rows = db.execute(
            "SELECT id::int AS v FROM t WHERE id = :id", {"id": 1}
        )
        assert rows == [{"v": 1}]

    def test_prefix_names_together(self, db):
        rows = db.execute(
            "SELECT :id AS a, :id_org AS b", {"id": 1, "id_org": 2}
        )
        assert rows == [{"a": 1, "b": 2}]

    def test_jsonb_cast_still_works(self, db):
        rows = db.execute("SELECT '{}'::jsonb AS j WHERE 1 = :one", {"one": 1})
        assert rows == [{"j": "{}"}]
