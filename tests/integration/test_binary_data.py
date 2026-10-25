"""
Integration tests for binary/BLOB data handling
Tests binary parameter support across PostgreSQL, MySQL, MSSQL, and SQLite
"""

import os
import pytest
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
        return ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url="sqlite://:memory:"
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
def db_manager(db_config):
    """Create and connect database manager"""
    # Probed once per session and cached -- an absent backend costs 15s per attempt
    # otherwise, and every parametrised test pays it before skipping anyway.
    from tests.conftest import skip_unless_reachable
    skip_unless_reachable(db_config.database_url, db_config.database_type.value)

    db = DatabaseManager(db_config)
    if not db.connect():
        pytest.skip(f"{db_config.database_type.value} not reachable")

    # Clean up any existing test tables
    try:
        db.execute("DROP TABLE IF EXISTS test_binary_files")
    except:
        pass

    # Create table with appropriate binary column type for each database
    if db_config.database_type == DatabaseType.SQLITE:
        db.execute("""
            CREATE TABLE test_binary_files (
                id INTEGER PRIMARY KEY,
                filename TEXT,
                data BLOB,
                size INTEGER
            )
        """)
    elif db_config.database_type == DatabaseType.POSTGRESQL:
        db.execute("""
            CREATE TABLE test_binary_files (
                id SERIAL PRIMARY KEY,
                filename VARCHAR(255),
                data BYTEA,
                size INTEGER
            )
        """)
    elif db_config.database_type == DatabaseType.MYSQL:
        db.execute("""
            CREATE TABLE test_binary_files (
                id INT AUTO_INCREMENT PRIMARY KEY,
                filename VARCHAR(255),
                data LONGBLOB,
                size INT
            )
        """)
    elif db_config.database_type == DatabaseType.MSSQL:
        db.execute("""
            CREATE TABLE test_binary_files (
                id INT IDENTITY(1,1) PRIMARY KEY,
                filename NVARCHAR(255),
                data VARBINARY(MAX),
                size INT
            )
        """)

    yield db

    # Clean up after tests
    try:
        db.execute("DROP TABLE IF EXISTS test_binary_files")
    except:
        pass

    db.disconnect()


class TestBinaryBasics:
    """Basic binary data insertion and retrieval tests"""

    def test_small_binary_data(self, db_manager):
        """Test storing and retrieving small binary data"""
        test_data = b'\x00\x01\x02\x03\xff\xfe\xfd'

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'test.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'test.bin'})
        assert result is not None
        assert result['filename'] == 'test.bin'
        assert result['size'] == len(test_data)

        # Compare binary data
        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    def test_empty_binary_data(self, db_manager):
        """Test storing and retrieving empty binary data"""
        test_data = b''

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'empty.bin', 'data': test_data, 'size': 0}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'empty.bin'})
        assert result is not None

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    def test_null_binary_data(self, db_manager):
        """Test storing and retrieving NULL binary data"""
        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'null.bin', 'data': None, 'size': 0}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'null.bin'})
        assert result is not None
        assert result['data'] is None


class TestBinarySpecialSequences:
    """Test binary data with special byte sequences"""

    def test_all_byte_values(self, db_manager):
        """Test that all possible byte values (0-255) can be stored"""
        test_data = bytes(range(256))

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'all_bytes.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'all_bytes.bin'})

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    def test_png_header(self, db_manager):
        """Test PNG file header (common binary signature)"""
        png_header = b'\x89PNG\r\n\x1a\n'

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'image.png', 'data': png_header, 'size': len(png_header)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'image.png'})

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == png_header

    def test_null_bytes_in_data(self, db_manager):
        """Test binary data containing null bytes"""
        test_data = b'Hello\x00World\x00Test'

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'nulls.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'nulls.bin'})

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    def test_high_entropy_data(self, db_manager):
        """Test random-looking data (simulating encrypted/compressed content)"""
        # Simulate high-entropy data
        import hashlib
        test_data = hashlib.sha256(b'test_data').digest() * 4  # 128 bytes of hash

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'encrypted.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'encrypted.bin'})

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data


class TestBinarySizes:
    """Test binary data of various sizes"""

    def test_1kb_data(self, db_manager):
        """Test 1KB of binary data"""
        test_data = bytes(range(256)) * 4  # 1KB

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': '1kb.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': '1kb.bin'})
        assert result['size'] == 1024

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    def test_64kb_data(self, db_manager):
        """Test 64KB of binary data"""
        test_data = bytes(range(256)) * 256  # 64KB

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': '64kb.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': '64kb.bin'})
        assert result['size'] == 65536

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    @pytest.mark.slow
    def test_1mb_data(self, db_manager):
        """Test 1MB of binary data"""
        from tests.conftest import skip_if_legacy_mssql_driver
        skip_if_legacy_mssql_driver(db_manager, "cannot write large VARBINARY(MAX) values")
        test_data = bytes(range(256)) * 4096  # 1MB

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': '1mb.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': '1mb.bin'})
        assert result['size'] == 1048576

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data


class TestBinaryMultipleRows:
    """Test multiple binary rows and queries"""

    def test_multiple_binary_inserts(self, db_manager):
        """Test inserting multiple binary records"""
        files = [
            ('file1.bin', b'\x01\x02\x03'),
            ('file2.bin', b'\x04\x05\x06'),
            ('file3.bin', b'\x07\x08\x09'),
        ]

        for filename, data in files:
            db_manager.execute(
                "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
                {'name': filename, 'data': data, 'size': len(data)}
            )

        results = db_manager.execute("SELECT * FROM test_binary_files ORDER BY filename")
        assert len(results) == 3

        for i, result in enumerate(results):
            expected_filename, expected_data = files[i]
            assert result['filename'] == expected_filename

            retrieved_data = result['data']
            if isinstance(retrieved_data, memoryview):
                retrieved_data = bytes(retrieved_data)
            assert retrieved_data == expected_data

    def test_query_by_size(self, db_manager):
        """Test querying binary data by size"""
        test_files = [
            ('small.bin', b'\x01\x02'),           # 2 bytes
            ('medium.bin', b'\x01' * 100),         # 100 bytes
            ('large.bin', b'\x01' * 1000),         # 1000 bytes
        ]

        for filename, data in test_files:
            db_manager.execute(
                "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
                {'name': filename, 'data': data, 'size': len(data)}
            )

        # Query files larger than 50 bytes
        results = db_manager.execute(
            "SELECT * FROM test_binary_files WHERE size > :min_size ORDER BY size",
            {'min_size': 50}
        )

        assert len(results) == 2
        assert results[0]['filename'] == 'medium.bin'
        assert results[1]['filename'] == 'large.bin'


class TestBinaryEdgeCases:
    """Edge case tests for binary data"""

    def test_binary_with_sql_special_chars(self, db_manager):
        """Test binary data that looks like SQL when decoded"""
        # Binary that contains SQL-like patterns
        test_data = b"'; DROP TABLE students; --"

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'sql_injection.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'sql_injection.bin'})

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data

    def test_binary_update(self, db_manager):
        """Test updating binary data"""
        original_data = b'\x01\x02\x03'
        new_data = b'\x04\x05\x06\x07'

        # Insert original
        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'update_test.bin', 'data': original_data, 'size': len(original_data)}
        )

        # Update with new data
        db_manager.execute(
            "UPDATE test_binary_files SET data = :data, size = :size WHERE filename = :name",
            {'name': 'update_test.bin', 'data': new_data, 'size': len(new_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'update_test.bin'})
        assert result['size'] == len(new_data)

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == new_data

    def test_repeated_byte_patterns(self, db_manager):
        """Test data with repeated patterns"""
        # Pattern that might trigger compression or optimization
        test_data = b'\xFF' * 1000  # 1000 bytes of 0xFF

        db_manager.execute(
            "INSERT INTO test_binary_files (filename, data, size) VALUES (:name, :data, :size)",
            {'name': 'repeated.bin', 'data': test_data, 'size': len(test_data)}
        )

        result = db_manager.fetch_one("SELECT * FROM test_binary_files WHERE filename = :name", {'name': 'repeated.bin'})

        retrieved_data = result['data']
        if isinstance(retrieved_data, memoryview):
            retrieved_data = bytes(retrieved_data)
        assert retrieved_data == test_data
        assert len(retrieved_data) == 1000
