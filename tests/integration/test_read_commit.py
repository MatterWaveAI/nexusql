"""
Live check that reads hand their connection back idle on PostgreSQL.

psycopg_pool logs "rolling back returned connection" at WARNING through the
``psycopg.pool`` logger whenever a connection comes back in-transaction.
A plain SELECT used to trigger that on every call.
"""

import logging

import pytest

from nexusql import DatabaseManager

psycopg = pytest.importorskip("psycopg")
from psycopg.pq import TransactionStatus  # noqa: E402


@pytest.fixture
def pg(postgresql_url, skip_if_no_postgresql):
    db = DatabaseManager(postgresql_url, pool_min=1, pool_max=1)
    assert db.connect()
    yield db
    db.disconnect()


def _status(db) -> TransactionStatus:
    with db._borrow() as conn:
        return conn.info.transaction_status


def test_select_through_execute_leaves_connection_idle(pg, caplog):
    with caplog.at_level(logging.WARNING, logger="psycopg.pool"):
        assert pg.execute("SELECT 1 AS one") == [{"one": 1}]
        assert pg.fetch_one("SELECT 2 AS two") == {"two": 2}
        assert pg.execute("SELECT 1 AS one WHERE false") == []
    assert _status(pg) == TransactionStatus.IDLE
    assert not [r for r in caplog.records if "rolling back" in r.getMessage()]


def test_reads_inside_transaction_stay_in_transaction_until_commit(pg):
    with pg.transaction():
        pg.execute("SELECT 1 AS one")
        assert _status(pg) == TransactionStatus.INTRANS
    assert _status(pg) == TransactionStatus.IDLE
