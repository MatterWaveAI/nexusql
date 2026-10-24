"""
Regression tests for B-001: Postgres parameter prefix corruption.

Previously, the PostgreSQL branch of _convert_params used a naive string
replace (str.replace(f":{key}", f"%({key})s")). When one parameter name was a
prefix of another (:org vs :org_id, :id vs :id_org), the shorter name rewrote
the inside of the longer one, producing %(org)s_id instead of %(org_id)s.

The fix replaces only complete :name tokens with a word-boundary regex and a
negative lookbehind so PostgreSQL :: casts are never treated as parameters.

These tests exercise _convert_params directly, so they run without a database
server. An end-to-end Postgres test lives in
tests/integration/test_postgres_param_prefix.py.
"""

import pytest
from nexusql import DatabaseManager


@pytest.fixture
def postgres_manager():
    """DatabaseManager configured for PostgreSQL (no connection needed)."""
    return DatabaseManager("postgresql://testuser:testpass@localhost:15432/nexusql_test")


def test_insert_org_and_org_id(postgres_manager):
    """T-001 pattern: :org_id and :org in the same query, dict ordered org first."""
    query = "INSERT INTO conversations (org_id, owner) VALUES (:org_id, :org)"
    params = {"org": "acme", "org_id": 42}
    converted_query, converted_params = postgres_manager._convert_params(query, params)
    assert converted_query == (
        "INSERT INTO conversations (org_id, owner) VALUES (%(org_id)s, %(org)s)"
    )
    assert converted_params == {"org": "acme", "org_id": 42}


def test_where_id_and_id_org_reversed_dict_order(postgres_manager):
    """Result must not depend on dict insertion order (:id vs :id_org)."""
    query = "SELECT * FROM t WHERE id = :id AND org_id = :id_org"
    params = {"id_org": "x", "id": 1}
    converted_query, _ = postgres_manager._convert_params(query, params)
    assert converted_query == "SELECT * FROM t WHERE id = %(id)s AND org_id = %(id_org)s"


def test_id_and_id_extra_pair(postgres_manager):
    """The pair called out in the bug report: {'id', 'id_extra'}."""
    query = "SELECT * FROM t WHERE id = :id OR id_extra = :id_extra"
    params = {"id": 1, "id_extra": "extra"}
    converted_query, converted_params = postgres_manager._convert_params(query, params)
    assert "%(id)s" in converted_query
    assert "%(id_extra)s" in converted_query
    assert "%(id)s_extra" not in converted_query
    assert converted_params == {"id": 1, "id_extra": "extra"}


def test_parent_and_conversation_pairs(postgres_manager):
    """All three T-001 prefix pairs in one query."""
    query = (
        "UPDATE messages SET parent = :parent, parent_id = :parent_id, "
        "conversation = :conversation, conversation_id = :conversation_id "
        "WHERE id = :id AND id_org = :id_org"
    )
    params = {
        "id": 1,
        "id_org": "o",
        "parent": "p",
        "parent_id": 7,
        "conversation": "c",
        "conversation_id": 9,
    }
    converted_query, _ = postgres_manager._convert_params(query, params)
    assert converted_query == (
        "UPDATE messages SET parent = %(parent)s, parent_id = %(parent_id)s, "
        "conversation = %(conversation)s, conversation_id = %(conversation_id)s "
        "WHERE id = %(id)s AND id_org = %(id_org)s"
    )


def test_postgres_cast_not_treated_as_param(postgres_manager):
    """::type casts must survive conversion (negative lookbehind on ':')."""
    query = "SELECT config::jsonb FROM t WHERE id = :id"
    converted_query, _ = postgres_manager._convert_params(query, {"id": 1})
    assert converted_query == "SELECT config::jsonb FROM t WHERE id = %(id)s"


def test_postgres_cast_with_matching_param_name(postgres_manager):
    """A param literally named like a cast target must not corrupt the cast."""
    query = "SELECT '{}'::text AS v, :text AS p"
    converted_query, _ = postgres_manager._convert_params(query, {"text": "hello"})
    assert converted_query == "SELECT '{}'::text AS v, %(text)s AS p"
