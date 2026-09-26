"""Pytest configuration for NexusQL tests"""
import pytest
import os
from pathlib import Path


@pytest.fixture
def temp_db_path(tmp_path):
    """Provide a temporary database path"""
    return str(tmp_path / "test.db")


@pytest.fixture
def sqlite_url(temp_db_path):
    """Provide a SQLite database URL"""
    return f"sqlite:///{temp_db_path}"


# PostgreSQL test configuration
@pytest.fixture
def postgresql_url():
    """Get PostgreSQL test URL from environment"""
    return os.getenv("TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:15432/ia_modules_test")


# MySQL test configuration
@pytest.fixture
def mysql_url():
    """Get MySQL test URL from environment"""
    return os.getenv("TEST_MYSQL_URL", "mysql://testuser:testpass@localhost:13306/ia_modules_test")


# MSSQL test configuration
@pytest.fixture
def mssql_url():
    """Get MSSQL test URL from environment"""
    return os.getenv("TEST_MSSQL_URL", "mssql://sa:TestPass123!@localhost:11433/master?TrustServerCertificate=yes")


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

    master_db = DatabaseManager(master_url)
    if not master_db.connect():
        pytest.skip("MSSQL master database not available")

    try:
        master_db._connection.autocommit = True
        master_db.execute(f"IF NOT EXISTS (SELECT * FROM sys.databases WHERE name = '{db_name}') CREATE DATABASE {db_name}")
        master_db._connection.autocommit = False
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
    try:
        db = DatabaseManager(url)
        ok = bool(db.connect())
        if ok:
            db.disconnect()
    except Exception:
        ok = False
    _REACHABLE[url] = ok
    return ok


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
