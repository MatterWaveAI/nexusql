"""
Integration tests for concurrent database access
Tests that multiple DatabaseManager instances can safely work in parallel
"""

import os
import pytest
import threading
import concurrent.futures
import time
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType

# Import shared MSSQL helper from conftest
from ..conftest import ensure_mssql_database_exists


@pytest.fixture(params=[
    pytest.param('sqlite', marks=pytest.mark.sqlite),
    pytest.param('postgresql', marks=pytest.mark.postgresql),
    pytest.param('mysql', marks=pytest.mark.mysql),
    pytest.param('mssql', marks=pytest.mark.mssql),
])
def db_config(request):
    """Provide database configurations for all supported databases"""
    db_type = request.param

    if db_type == 'sqlite':
        # Use a file-based SQLite for concurrency tests (memory mode isn't truly concurrent)
        import tempfile
        temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        temp_db.close()
        return ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url=f"sqlite:///{temp_db.name}"
        )

    env_vars = {
        'postgresql': ('TEST_POSTGRESQL_URL', DatabaseType.POSTGRESQL),
        'mysql': ('TEST_MYSQL_URL', DatabaseType.MYSQL),
        'mssql': ('TEST_MSSQL_URL', DatabaseType.MSSQL),
    }

    env_var, db_type_enum = env_vars[db_type]
    db_url = os.getenv(env_var)

    if not db_url:
        pytest.skip(f"{env_var} not set")

    # For MSSQL, ensure test database exists first
    if db_type == 'mssql':
        ensure_mssql_database_exists(db_url)

    return ConnectionConfig(
        database_type=db_type_enum,
        database_url=db_url
    )


@pytest.fixture
def setup_test_table(db_config):
    """Set up test table before tests"""
    db = DatabaseManager(db_config)
    db.connect()

    # Clean up any existing test table
    try:
        db.execute("DROP TABLE IF EXISTS test_concurrent_workers")
    except:
        pass

    # Create test table
    if db_config.database_type == DatabaseType.SQLITE:
        db.execute("""
            CREATE TABLE test_concurrent_workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                iteration INTEGER NOT NULL,
                value TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    elif db_config.database_type == DatabaseType.POSTGRESQL:
        db.execute("""
            CREATE TABLE test_concurrent_workers (
                id SERIAL PRIMARY KEY,
                worker_id INTEGER NOT NULL,
                iteration INTEGER NOT NULL,
                value TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    elif db_config.database_type == DatabaseType.MYSQL:
        db.execute("""
            CREATE TABLE test_concurrent_workers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                worker_id INT NOT NULL,
                iteration INT NOT NULL,
                value TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    elif db_config.database_type == DatabaseType.MSSQL:
        db.execute("""
            CREATE TABLE test_concurrent_workers (
                id INT IDENTITY(1,1) PRIMARY KEY,
                worker_id INT NOT NULL,
                iteration INT NOT NULL,
                value NVARCHAR(255),
                timestamp DATETIME2 DEFAULT GETDATE()
            )
        """)

    db.disconnect()

    yield db_config

    # Clean up after tests
    db = DatabaseManager(db_config)
    db.connect()
    try:
        db.execute("DROP TABLE IF EXISTS test_concurrent_workers")
    except:
        pass
    db.disconnect()


class TestConcurrentReads:
    """Test concurrent read operations"""

    def test_multiple_readers_same_data(self, setup_test_table):
        """Test multiple threads reading the same data simultaneously"""
        db_config = setup_test_table

        # Insert test data
        db = DatabaseManager(db_config)
        db.connect()
        for i in range(10):
            db.execute(
                "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                {'w': 0, 'i': i, 'v': f'test_{i}'}
            )
        db.disconnect()

        def reader_worker(worker_id):
            """Each worker reads all data"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                results = db.execute("SELECT * FROM test_concurrent_workers ORDER BY iteration")
                return len(results)
            finally:
                db.disconnect()

        # Run 5 concurrent readers
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(reader_worker, i) for i in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All readers should see all 10 rows
        assert all(count == 10 for count in results)

    def test_concurrent_fetch_one_queries(self, setup_test_table):
        """Test multiple threads using fetch_one simultaneously"""
        db_config = setup_test_table

        # Insert test data
        db = DatabaseManager(db_config)
        db.connect()
        for i in range(20):
            db.execute(
                "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                {'w': i, 'i': 0, 'v': f'worker_{i}'}
            )
        db.disconnect()

        def fetch_specific_row(worker_id):
            """Each worker fetches its own row"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                result = db.fetch_one(
                    "SELECT * FROM test_concurrent_workers WHERE worker_id = :w",
                    {'w': worker_id}
                )
                return result['worker_id'] if result else None
            finally:
                db.disconnect()

        # Run 20 concurrent fetch_one queries
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_specific_row, i) for i in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Each worker should have found its row
        assert sorted(results) == list(range(20))


class TestConcurrentWrites:
    """Test concurrent write operations"""

    def test_multiple_writers_separate_rows(self, setup_test_table):
        """Test multiple threads inserting different data"""
        db_config = setup_test_table

        def writer_worker(worker_id, iterations=10):
            """Each worker inserts multiple rows"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                for i in range(iterations):
                    db.execute(
                        "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                        {'w': worker_id, 'i': i, 'v': f'worker_{worker_id}_iter_{i}'}
                    )
                return worker_id
            finally:
                db.disconnect()

        # Run 5 workers, each inserting 10 rows
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(writer_worker, i, 10) for i in range(5)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify all 50 rows were inserted
        db = DatabaseManager(db_config)
        db.connect()
        count_result = db.fetch_one("SELECT COUNT(*) as cnt FROM test_concurrent_workers")
        db.disconnect()

        assert count_result['cnt'] == 50

    def test_concurrent_updates(self, setup_test_table):
        """Test multiple threads updating different rows"""
        db_config = setup_test_table

        # Insert initial data
        db = DatabaseManager(db_config)
        db.connect()
        for i in range(10):
            db.execute(
                "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                {'w': i, 'i': 0, 'v': 'original'}
            )
        db.disconnect()

        def update_worker(worker_id):
            """Each worker updates its own row"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                db.execute(
                    "UPDATE test_concurrent_workers SET value = :v WHERE worker_id = :w",
                    {'v': f'updated_{worker_id}', 'w': worker_id}
                )
                return worker_id
            finally:
                db.disconnect()

        # Run 10 concurrent updates
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(update_worker, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify all updates succeeded
        db = DatabaseManager(db_config)
        db.connect()
        rows = db.execute("SELECT * FROM test_concurrent_workers ORDER BY worker_id")
        db.disconnect()

        assert len(rows) == 10
        for i, row in enumerate(rows):
            assert row['value'] == f'updated_{i}'


class TestConcurrentMixed:
    """Test mixed read/write operations"""

    def test_readers_and_writers_mixed(self, setup_test_table):
        """Test concurrent readers and writers"""
        db_config = setup_test_table

        # Insert initial data
        db = DatabaseManager(db_config)
        db.connect()
        for i in range(10):
            db.execute(
                "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                {'w': 0, 'i': i, 'v': 'initial'}
            )
        db.disconnect()

        results = {'reads': [], 'writes': []}

        def reader_worker(worker_id):
            """Read data periodically"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                count_result = db.fetch_one("SELECT COUNT(*) as cnt FROM test_concurrent_workers")
                return ('read', count_result['cnt'])
            finally:
                db.disconnect()

        def writer_worker(worker_id):
            """Insert new data"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                db.execute(
                    "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                    {'w': worker_id, 'i': 0, 'v': f'writer_{worker_id}'}
                )
                return ('write', worker_id)
            finally:
                db.disconnect()

        # Run mix of readers and writers
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            # 5 readers
            for i in range(5):
                futures.append(executor.submit(reader_worker, i))
            # 5 writers
            for i in range(5):
                futures.append(executor.submit(writer_worker, i + 100))

            for future in concurrent.futures.as_completed(futures):
                op_type, value = future.result()
                results[f'{op_type}s'].append(value)

        # Verify operations completed
        assert len(results['reads']) == 5
        assert len(results['writes']) == 5

        # Verify final count is correct
        db = DatabaseManager(db_config)
        db.connect()
        final_count = db.fetch_one("SELECT COUNT(*) as cnt FROM test_concurrent_workers")
        db.disconnect()

        assert final_count['cnt'] == 15  # 10 initial + 5 written


class TestConcurrentStress:
    """Stress tests for concurrent operations"""

    def test_high_concurrency_inserts(self, setup_test_table):
        """Test many concurrent inserts"""
        db_config = setup_test_table

        def insert_worker(worker_id):
            """Insert a single row"""
            db = DatabaseManager(db_config)
            db.connect()
            try:
                db.execute(
                    "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                    {'w': worker_id, 'i': 0, 'v': f'worker_{worker_id}'}
                )
                return worker_id
            finally:
                db.disconnect()

        # Run 50 concurrent workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(insert_worker, i) for i in range(50)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify all inserts succeeded
        assert len(results) == 50

        db = DatabaseManager(db_config)
        db.connect()
        count_result = db.fetch_one("SELECT COUNT(*) as cnt FROM test_concurrent_workers")
        db.disconnect()

        assert count_result['cnt'] == 50

    def test_rapid_connect_disconnect(self, setup_test_table):
        """Test rapid connection creation and destruction"""
        db_config = setup_test_table

        def rapid_ops(worker_id):
            """Connect, query, disconnect rapidly"""
            for i in range(5):
                db = DatabaseManager(db_config)
                db.connect()
                try:
                    db.execute(
                        "INSERT INTO test_concurrent_workers (worker_id, iteration, value) VALUES (:w, :i, :v)",
                        {'w': worker_id, 'i': i, 'v': f'w{worker_id}_i{i}'}
                    )
                finally:
                    db.disconnect()
            return worker_id

        # Run 10 workers doing rapid connect/disconnect
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(rapid_ops, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # Verify all operations succeeded
        assert len(results) == 10

        db = DatabaseManager(db_config)
        db.connect()
        count_result = db.fetch_one("SELECT COUNT(*) as cnt FROM test_concurrent_workers")
        db.disconnect()

        # 10 workers * 5 iterations = 50 rows
        assert count_result['cnt'] == 50
