#!/usr/bin/env python
"""Print pytest deselection flags for database backends this runner cannot reach.

The test job declares `services:` for postgres, mysql and mssql. A Gitea runner in
**hostexecutor** mode ignores them silently -- it runs directly on the host, so whatever
is already listening on 5432/3306 answers instead, with different credentials. That
produced 29 failures reading `password authentication failed for user "postgres"`, which
is a runner-topology problem and not a library defect.

So probe each backend and deselect the ones that are not actually ours, rather than
failing a build over a database that was never started.

Usage:
    DESELECT=$(python scripts/ci_available_backends.py)
    eval pytest tests/integration/ $DESELECT

Prints nothing when every backend is reachable.
"""
from __future__ import annotations

import os
import re
import sys


def _warn(backend: str, exc: Exception) -> None:
    first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    print(f"::warning::{backend} not usable on this runner, deselecting: {first}",
          file=sys.stderr)


def postgres_ok() -> bool:
    url = os.environ.get("TEST_POSTGRESQL_URL")
    if not url:
        return False
    try:
        import psycopg
    except ImportError as exc:
        _warn("postgresql", exc)
        return False
    try:
        psycopg.connect(url, connect_timeout=5).close()
        return True
    except Exception as exc:
        _warn("postgresql", exc)
        return False


def mysql_ok() -> bool:
    url = os.environ.get("TEST_MYSQL_URL")
    if not url:
        return False
    try:
        import pymysql
    except ImportError as exc:
        _warn("mysql", exc)
        return False
    m = re.match(r"mysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", url)
    if not m:
        _warn("mysql", ValueError(f"cannot parse TEST_MYSQL_URL: {url}"))
        return False
    user, password, host, port, database = m.groups()
    try:
        pymysql.connect(user=user, password=password, host=host,
                        port=int(port), database=database,
                        connect_timeout=5).close()
        return True
    except Exception as exc:
        _warn("mysql", exc)
        return False


def main() -> int:
    skip = []
    if not postgres_ok():
        skip.append("postgresql")
    if not mysql_ok():
        skip.append("mysql")
    # The ODBC install step sets this when it could not install the driver.
    if os.environ.get("MSSQL_DRIVER") == "absent":
        skip.append("mssql")

    if skip:
        expr = " and ".join(f"not {name}" for name in skip)
        print(f'-m "{expr}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
