"""Pytest configuration for NexusQL tests"""
import asyncio
import sys

import pytest
import os
from pathlib import Path

# psycopg's async connection needs a selector-based loop; on Windows the
# default is the proactor loop, under which every async PostgreSQL test
# fails to connect and skips as "unreachable". Pick the selector loop here
# so the async tests run on a Windows dev box the same as they do in CI.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture
def temp_db_path(tmp_path):
    """Provide a temporary database path"""
    return str(tmp_path / "test.db")


@pytest.fixture
def sqlite_url(temp_db_path):
    """Provide a SQLite database URL"""
    return f"sqlite:///{temp_db_path}"


# PostgreSQL test configuration
# The defaults below use 127.0.0.1 rather than localhost on purpose: on
# Windows, localhost resolves to ::1 first, and a container published on
# 127.0.0.1 leaves every connection waiting about 20 seconds for the IPv6
# attempt to time out before the IPv4 one succeeds.
@pytest.fixture
def postgresql_url():
    """Get PostgreSQL test URL from environment"""
    return os.getenv("TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@127.0.0.1:15432/nexusql_test")


# MySQL test configuration
@pytest.fixture
def mysql_url():
    """Get MySQL test URL from environment"""
    return os.getenv("TEST_MYSQL_URL", "mysql://testuser:testpass@127.0.0.1:13306/nexusql_test")


# MSSQL test configuration
@pytest.fixture
def mssql_url():
    """Get MSSQL test URL from environment"""
    return os.getenv("TEST_MSSQL_URL", "mssql://sa:TestPass123!@127.0.0.1:11433/nexusql_test?TrustServerCertificate=yes")


@pytest.fixture
def skip_if_no_postgresql(postgresql_url):
    """Skip test if PostgreSQL is not available"""
    try:
        from nexusql import DatabaseManager
        db = DatabaseManager(postgresql_url)
        if not db.connect():
            pytest.skip("PostgreSQL not available")
        db.disconnect()
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")


@pytest.fixture
def skip_if_no_mysql(mysql_url):
    """Skip test if MySQL is not available"""
    try:
        from nexusql import DatabaseManager
        db = DatabaseManager(mysql_url)
        if not db.connect():
            pytest.skip("MySQL not available")
        db.disconnect()
    except Exception as e:
        pytest.skip(f"MySQL not available: {e}")


@pytest.fixture
def skip_if_no_mssql(mssql_url):
    """Skip test if MSSQL is not available"""
    try:
        from nexusql import DatabaseManager
        db = DatabaseManager(mssql_url)
        if not db.connect():
            pytest.skip("MSSQL not available")
        db.disconnect()
    except Exception as e:
        pytest.skip(f"MSSQL not available: {e}")


@pytest.fixture
def db_config(request, sqlite_url, postgresql_url, mysql_url, mssql_url):
    """Provide a ConnectionConfig for the requested database type"""
    from nexusql import ConnectionConfig

    db_type = request.param if hasattr(request, 'param') else 'sqlite'

    url_map = {
        'sqlite': sqlite_url,
        'postgresql': postgresql_url,
        'mysql': mysql_url,
        'mssql': mssql_url
    }

    db_url = url_map.get(db_type, sqlite_url)
    return ConnectionConfig.from_url(db_url)


def ensure_mssql_database_exists(database_url: str) -> None:
    """
    Ensure MSSQL test database exists by connecting to master and creating it.

    MSSQL cannot auto-create databases like PostgreSQL/MySQL, so we must:
    1. Connect to the master database
    2. Execute CREATE DATABASE IF NOT EXISTS
    3. Disconnect from master

    Args:
        database_url: Full MSSQL connection URL including database name and query params
    """
    from urllib.parse import urlparse
    from nexusql import DatabaseManager

    parsed = urlparse(database_url)
    db_name = parsed.path.lstrip('/')

    # Build master URL preserving all query parameters
    master_url = f"{parsed.scheme}://{parsed.netloc}/master"
    if parsed.query:
        master_url += f"?{parsed.query}"

    if not create_mssql_database(database_url):
        pytest.skip("MSSQL master database not available")


def create_mssql_database(database_url: str) -> bool:
    """
    Create the database named in an MSSQL URL if it is missing.

    Returns False when the server's master database cannot be reached.
    Shared by the fixture above and the collection-time reachability probe:
    the probe connects to the test database itself, which does not exist on
    a fresh server, so without this every MSSQL test skipped as unreachable.
    """
    from urllib.parse import urlparse
    from nexusql import DatabaseManager

    parsed = urlparse(database_url)
    db_name = parsed.path.lstrip('/')
    if not db_name or db_name.lower() == 'master':
        return True

    master_url = f"{parsed.scheme}://{parsed.netloc}/master"
    if parsed.query:
        master_url += f"?{parsed.query}"

    master_db = DatabaseManager(master_url)
    try:
        if not master_db.connect():
            return False
    except Exception:
        return False
    try:
        # CREATE DATABASE refuses to run inside a transaction, so switch the
        # borrowed connection to autocommit for the one statement.
        with master_db._borrow() as conn:
            conn.autocommit = True
            try:
                conn.cursor().execute(
                    f"IF NOT EXISTS (SELECT * FROM sys.databases WHERE name = '{db_name}') "
                    f"CREATE DATABASE {db_name}")
            finally:
                conn.autocommit = False
        return True
    finally:
        master_db.disconnect()


@pytest.fixture
def db_manager(request, sqlite_url, postgresql_url, mysql_url, mssql_url):
    """Provide a DatabaseManager for the requested database type"""
    from nexusql import DatabaseManager

    db_type = request.param if hasattr(request, 'param') else 'sqlite'

    url_map = {
        'sqlite': sqlite_url,
        'postgresql': postgresql_url,
        'mysql': mysql_url,
        'mssql': mssql_url
    }

    db_url = url_map.get(db_type, sqlite_url)

    # For MSSQL, ensure test database exists first
    if db_type == 'mssql':
        ensure_mssql_database_exists(mssql_url)

    db = DatabaseManager(db_url)

    # Try to connect, skip if not available
    try:
        if not db.connect():
            pytest.skip(f"{db_type} not available")
    except Exception as e:
        pytest.skip(f"{db_type} not available: {e}")

    yield db

    # Cleanup
    try:
        db.disconnect()
    except:
        pass

# --- backend reachability, probed once per session -------------------------------
#
# A declared-but-absent backend costs 15 seconds per attempt: the pool retries for 10
# and then gives up. Multiplied across every parametrised test that is about to skip
# anyway, that turned a CI run into minutes of waiting. This probes each backend once
# and caches the answer.
#
# It exists because a Gitea runner in hostexecutor mode silently ignores `services:`,
# so TEST_*_URL points at whatever is already on that port -- usually with different
# credentials.

_REACHABLE: dict = {}


def backend_reachable(url: str) -> bool:
    """True if this URL accepts a connection. Probed once per session, then cached."""
    if url in _REACHABLE:
        return _REACHABLE[url]
    from nexusql import DatabaseManager
    if url.startswith("mssql") and not create_mssql_database(url):
        _REACHABLE[url] = False
        return False
    try:
        db = DatabaseManager(url)
        ok = bool(db.connect())
        if ok:
            db.disconnect()
    except Exception:
        ok = False
    _REACHABLE[url] = ok
    return ok


def mssql_driver_is_legacy() -> bool:
    """
    True when the only SQL Server ODBC driver is the one Windows ships.

    That driver predates DATETIME2 and VARBINARY(MAX) streaming: it returns
    timestamps as text and cannot write large binary values. The tests that
    exercise exactly those behaviours skip on it rather than report a
    library failure; "ODBC Driver 18 for SQL Server" runs them.
    """
    try:
        import pyodbc
    except ImportError:
        return False
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    return bool(drivers) and not any("ODBC Driver" in d for d in drivers)


def skip_if_legacy_mssql_driver(db, reason: str) -> None:
    """Skip a test running on MSSQL through the legacy driver."""
    from nexusql import DatabaseType
    if db.config.database_type == DatabaseType.MSSQL and mssql_driver_is_legacy():
        pytest.skip(f"legacy 'SQL Server' ODBC driver: {reason}")


def skip_unless_reachable(url: str, name: str) -> None:
    """Skip the test unless `url` is a database we can actually reach."""
    if not backend_reachable(url):
        pytest.skip(f"{name} not reachable at {url.split('@')[-1]}")

def pytest_collection_modifyitems(config, items):
    """Skip every test parametrised on a backend we cannot reach.

    Applied at collection, before any fixture runs, so each backend is probed once
    for the whole session rather than once per test. Individual fixtures still guard
    themselves -- this only stops the suite paying 15 seconds per parametrisation to
    rediscover the same absence.

    A parametrisation counts as a backend when its id contains the name, which is how
    every file in tests/integration/ names them.
    """
    import os

    urls = {
        "postgres": os.environ.get("TEST_POSTGRESQL_URL"),
        "postgresql": os.environ.get("TEST_POSTGRESQL_URL"),
        "mysql": os.environ.get("TEST_MYSQL_URL"),
        "mssql": os.environ.get("TEST_MSSQL_URL"),
    }

    for item in items:
        name = item.name.lower()
        for backend, url in urls.items():
            if f"[{backend}" not in name and f"-{backend}]" not in name:
                continue
            if not url:
                item.add_marker(pytest.mark.skip(reason=f"{backend}: TEST_*_URL not set"))
            elif not backend_reachable(url):
                item.add_marker(pytest.mark.skip(reason=f"{backend} not reachable"))
            break
