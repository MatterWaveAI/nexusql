#!/usr/bin/env python
"""Print pytest deselection flags for database backends this runner cannot reach.

The test job declares `services:` for postgres, mysql and mssql. A Gitea runner in
**hostexecutor** mode ignores them silently -- it runs directly on the host, so whatever
is already listening on 5432/3306 answers instead, with different credentials. That
produced 29 failures reading `password authentication failed for user "postgres"`, which
is a runner-topology problem and not a library defect.

So probe each backend and deselect the ones that are not actually ours, rather than
failing a build over a database that was never started.

On a GitHub-hosted runner the services are real, and a backend that cannot be reached
means the job is broken, not the topology. Set NEXUSQL_CI_REQUIRE_BACKENDS=1 there and
the script exits non-zero instead of quietly deselecting, so MySQL and SQL Server cannot
drop out of CI unnoticed.

Usage:
    DESELECT=$(python scripts/ci_available_backends.py)
    eval pytest tests/integration/ $DESELECT

Prints nothing when every backend is reachable.
"""
from __future__ import annotations

import os
import re
import sys
from urllib.parse import parse_qs, urlparse


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


def mssql_ok() -> bool:
    """Connect through the ODBC driver the manager would pick, to master.

    The test database itself is created by the suite on first use, so the
    probe only needs the server to answer.
    """
    url = os.environ.get("TEST_MSSQL_URL")
    if not url:
        return False
    if os.environ.get("MSSQL_DRIVER") == "absent":
        _warn("mssql", RuntimeError("ODBC driver could not be installed on this runner"))
        return False
    try:
        import pyodbc
    except ImportError as exc:
        _warn("mssql", exc)
        return False
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    preferred = [d for d in drivers if "ODBC Driver" in d] or drivers
    if not preferred:
        _warn("mssql", RuntimeError("no SQL Server ODBC driver installed"))
        return False
    parsed = urlparse(url)
    params = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items()}
    trust = params.get("trustservercertificate", "yes")
    conn_str = (
        f"DRIVER={{{preferred[0]}}};SERVER={parsed.hostname},{parsed.port or 1433};"
        f"DATABASE=master;UID={parsed.username};PWD={parsed.password};"
        f"TrustServerCertificate={trust}"
    )
    try:
        pyodbc.connect(conn_str, timeout=5).close()
        return True
    except Exception as exc:
        _warn("mssql", exc)
        return False


def main() -> int:
    skip = []
    if not postgres_ok():
        skip.append("postgresql")
    if not mysql_ok():
        skip.append("mysql")
    if not mssql_ok():
        skip.append("mssql")

    if skip and os.environ.get("NEXUSQL_CI_REQUIRE_BACKENDS"):
        print(f"::error::backends unreachable on a runner that declares them as "
              f"services: {', '.join(skip)}", file=sys.stderr)
        return 1
    if skip:
        expr = " and ".join(f"not {name}" for name in skip)
        print(f'-m "{expr}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
