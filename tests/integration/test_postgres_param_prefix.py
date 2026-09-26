"""
Regression test for B-001 end to end on PostgreSQL: parameters whose names are
prefixes of each other (:org / :org_id) must bind to the right columns.

Skipped when no PostgreSQL server is available; the pure conversion logic is
covered (without a server) by tests/unit/test_param_prefix_postgres.py.
"""

import pytest
from nexusql import DatabaseManager


def test_postgres_prefix_params_end_to_end(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url)
    assert db.connect(), "Postgres connect failed"
    try:
        db.execute("DROP TABLE IF EXISTS b001_prefix")
        db.execute(
            """
            CREATE TABLE b001_prefix (
                id INTEGER PRIMARY KEY,
                org_id INTEGER,
                org TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO b001_prefix (org_id, org) VALUES (:org_id, :org)",
            {"org": "acme", "org_id": 42},
        )
        row = db.fetch_one(
            "SELECT org_id, org FROM b001_prefix WHERE org_id = :org_id AND org = :org",
            {"org": "acme", "org_id": 42},
        )
        assert row == {"org_id": 42, "org": "acme"}
    finally:
        db.disconnect()
