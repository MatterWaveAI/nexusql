"""
Integration tests for MSSQL database backend

These tests require a running Microsoft SQL Server instance.
Run with: pytest -m mssql

Note: Requires pyodbc and ODBC Driver for SQL Server to be installed.
"""

import os
import pytest
from datetime import datetime
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


@pytest.fixture
def mssql_config():
    """Get MSSQL connection configuration from environment"""
    url = os.environ.get(
        "TEST_MSSQL_URL",
        "mssql://sa:TestPass123!@localhost:11433/ia_modules_test"
    )
    return ConnectionConfig(
        database_type=DatabaseType.MSSQL,
        database_url=url
    )


@pytest.fixture
def mssql_db(mssql_config):
    """Create an MSSQL database manager for testing"""
    from tests.conftest import ensure_mssql_database_exists

    # Ensure test database exists first
    ensure_mssql_database_exists(mssql_config.database_url)

    db = DatabaseManager(mssql_config)
    if not db.connect():
        pytest.skip("MSSQL not available")

    # Clean up test tables
    test_tables = [
        'test_users', 'test_products', 'test_data', 'test_table',
        'test_items', 'test_json', 'test_timestamps', 'test_transactions',
        'test_unicode', 'test_binary'
    ]
    for table in test_tables:
        try:
            db.execute(f'DROP TABLE IF EXISTS {table}')
        except:
            pass

    yield db

    # Cleanup after tests
    for table in test_tables:
        try:
            db.execute(f'DROP TABLE IF EXISTS {table}')
        except:
            pass

    db.disconnect()


@pytest.mark.mssql
@pytest.mark.integration
class TestMSSQLBasicOperations:
    """Test basic CRUD operations with MSSQL"""

    def test_create_table(self, mssql_db):
        """Test table creation"""
        mssql_db.execute('''
            CREATE TABLE test_users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(100) NOT NULL,
                email NVARCHAR(255) NOT NULL,
                created_at DATETIME2 DEFAULT GETDATE()
            )
        ''')

        # Verify table exists
        result = mssql_db.execute(
            "SELECT COUNT(*) as count FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_NAME = 'test_users'"
        )
        assert result[0]['count'] == 1

    def test_insert_and_select(self, mssql_db):
        """Test inserting and selecting data"""
        mssql_db.execute('''
            CREATE TABLE test_users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(100) NOT NULL,
                email NVARCHAR(255) NOT NULL
            )
        ''')

        # Insert data
        mssql_db.execute(
            "INSERT INTO test_users (username, email) VALUES (:username, :email)",
            {"username": "john_doe", "email": "john@example.com"}
        )

        # Select data
        result = mssql_db.execute("SELECT * FROM test_users WHERE username = :username", {"username": "john_doe"})
        assert len(result) == 1
        assert result[0]['username'] == "john_doe"
        assert result[0]['email'] == "john@example.com"

    def test_update(self, mssql_db):
        """Test updating data"""
        mssql_db.execute('''
            CREATE TABLE test_users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(100) NOT NULL,
                email NVARCHAR(255) NOT NULL
            )
        ''')

        mssql_db.execute(
            "INSERT INTO test_users (username, email) VALUES (:username, :email)",
            {"username": "john_doe", "email": "john@example.com"}
        )

        # Update data
        mssql_db.execute(
            "UPDATE test_users SET email = :email WHERE username = :username",
            {"email": "newemail@example.com", "username": "john_doe"}
        )

        # Verify update
        result = mssql_db.execute("SELECT email FROM test_users WHERE username = :username", {"username": "john_doe"})
        assert result[0]['email'] == "newemail@example.com"

    def test_delete(self, mssql_db):
        """Test deleting data"""
        mssql_db.execute('''
            CREATE TABLE test_users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(100) NOT NULL
            )
        ''')

        mssql_db.execute("INSERT INTO test_users (username) VALUES (:username)", {"username": "john_doe"})
        mssql_db.execute("DELETE FROM test_users WHERE username = :username", {"username": "john_doe"})

        result = mssql_db.execute("SELECT * FROM test_users WHERE username = :username", {"username": "john_doe"})
        assert len(result) == 0


@pytest.mark.mssql
@pytest.mark.integration
class TestMSSQLDataTypes:
    """Test MSSQL-specific data types"""

    def test_nvarchar_type(self, mssql_db):
        """Test NVARCHAR (Unicode) data type"""
        mssql_db.execute('''
            CREATE TABLE test_unicode (
                id INT IDENTITY(1,1) PRIMARY KEY,
                text NVARCHAR(MAX)
            )
        ''')

        # Test with Unicode characters
        unicode_text = "Hello 世界 مرحبا עולם"
        mssql_db.execute("INSERT INTO test_unicode (text) VALUES (:text)", {"text": unicode_text})

        result = mssql_db.execute("SELECT text FROM test_unicode")
        assert result[0]['text'] == unicode_text

    def test_datetime2_type(self, mssql_db):
        """Test DATETIME2 data type with high precision"""
        mssql_db.execute('''
            CREATE TABLE test_timestamps (
                id INT IDENTITY(1,1) PRIMARY KEY,
                created_at DATETIME2(7) DEFAULT SYSDATETIME(),
                event_time DATETIME2(7)
            )
        ''')

        test_time = datetime.now()
        mssql_db.execute("INSERT INTO test_timestamps (event_time) VALUES (:event_time)", {"event_time": test_time})

        result = mssql_db.execute("SELECT created_at, event_time FROM test_timestamps")
        assert result[0]['created_at'] is not None
        assert result[0]['event_time'] is not None

    def test_json_support(self, mssql_db):
        """Test JSON support using NVARCHAR(MAX) with JSON functions"""
        mssql_db.execute('''
            CREATE TABLE test_json (
                id INT IDENTITY(1,1) PRIMARY KEY,
                data NVARCHAR(MAX)
            )
        ''')

        import json
        test_data = {"name": "John", "age": 30, "tags": ["python", "mssql"]}

        mssql_db.execute(
            "INSERT INTO test_json (data) VALUES (:data)",
            {"data": json.dumps(test_data)}
        )

        # Verify JSON is valid
        result = mssql_db.execute(
            "SELECT data, ISJSON(data) as is_valid FROM test_json"
        )
        assert result[0]['is_valid'] == 1
        retrieved_data = json.loads(result[0]['data'])
        assert retrieved_data == test_data

    def test_binary_type(self, mssql_db):
        """Test VARBINARY data type"""
        mssql_db.execute('''
            CREATE TABLE test_binary (
                id INT IDENTITY(1,1) PRIMARY KEY,
                data VARBINARY(MAX)
            )
        ''')

        binary_data = b"Binary test data \x00\x01\x02\xFF"
        mssql_db.execute("INSERT INTO test_binary (data) VALUES (:data)", {"data": binary_data})

        result = mssql_db.execute("SELECT data FROM test_binary")
        assert result[0]['data'] == binary_data


@pytest.mark.mssql
@pytest.mark.integration
class TestMSSQLTransactions:
    """Test transaction support in MSSQL"""

    def test_commit_transaction(self, mssql_db):
        """Test committing a transaction"""
        mssql_db.execute('''
            CREATE TABLE test_transactions (
                id INT IDENTITY(1,1) PRIMARY KEY,
                value NVARCHAR(100)
            )
        ''')

        with mssql_db.transaction():
            mssql_db.execute("INSERT INTO test_transactions (value) VALUES (:value)", {"value": "test1"})
            mssql_db.execute("INSERT INTO test_transactions (value) VALUES (:value)", {"value": "test2"})

        result = mssql_db.execute("SELECT COUNT(*) as count FROM test_transactions")
        assert result[0]['count'] == 2

    def test_rollback_transaction(self, mssql_db):
        """Test rolling back a transaction"""
        mssql_db.execute('''
            CREATE TABLE test_transactions (
                id INT IDENTITY(1,1) PRIMARY KEY,
                value NVARCHAR(100)
            )
        ''')

        class _Abort(Exception):
            pass

        with pytest.raises(_Abort):
            with mssql_db.transaction():
                mssql_db.execute("INSERT INTO test_transactions (value) VALUES (:value)", {"value": "test1"})
                raise _Abort()

        result = mssql_db.execute("SELECT COUNT(*) as count FROM test_transactions")
        assert result[0]['count'] == 0

    def test_savepoint(self, mssql_db):
        """Test transaction savepoints"""
        mssql_db.execute('''
            CREATE TABLE test_transactions (
                id INT IDENTITY(1,1) PRIMARY KEY,
                value NVARCHAR(100)
            )
        ''')

        with mssql_db.transaction():
            mssql_db.execute("INSERT INTO test_transactions (value) VALUES (:value)", {"value": "test1"})
            # Savepoints are passed through to the server as ordinary SQL.
            mssql_db.execute("SAVE TRANSACTION savepoint1")
            mssql_db.execute("INSERT INTO test_transactions (value) VALUES (:value)", {"value": "test2"})
            mssql_db.execute("ROLLBACK TRANSACTION savepoint1")

        result = mssql_db.execute("SELECT COUNT(*) as count FROM test_transactions")
        assert result[0]['count'] == 1  # Only test1 should be committed


@pytest.mark.mssql
@pytest.mark.integration
class TestMSSQLPerformance:
    """Test MSSQL performance characteristics"""

    def test_bulk_insert(self, mssql_db):
        """Test bulk insert performance"""
        mssql_db.execute('''
            CREATE TABLE test_items (
                id INT IDENTITY(1,1) PRIMARY KEY,
                name NVARCHAR(100),
                value INT
            )
        ''')

        # Insert 1000 records
        for i in range(1000):
            mssql_db.execute(
                "INSERT INTO test_items (name, value) VALUES (:name, :value)",
                {"name": f"item_{i}", "value": i}
            )

        result = mssql_db.execute("SELECT COUNT(*) as count FROM test_items")
        assert result[0]['count'] == 1000

    def test_indexed_query(self, mssql_db):
        """Test query with index"""
        mssql_db.execute('''
            CREATE TABLE test_items (
                id INT IDENTITY(1,1) PRIMARY KEY,
                name NVARCHAR(100),
                value INT
            )
        ''')

        # Create index
        mssql_db.execute("CREATE INDEX idx_name ON test_items(name)")

        # Insert test data
        for i in range(100):
            mssql_db.execute(
                "INSERT INTO test_items (name, value) VALUES (:name, :value)",
                {"name": f"item_{i}", "value": i}
            )

        # Query using index
        result = mssql_db.execute(
            "SELECT * FROM test_items WHERE name = :name",
            {"name": "item_50"}
        )

        assert len(result) == 1
        assert result[0]['value'] == 50

    def test_stored_procedure(self, mssql_db):
        """Test creating and calling a stored procedure"""
        # Drop procedure if it exists
        try:
            mssql_db.execute("DROP PROCEDURE IF EXISTS GetItemByName")
        except:
            pass

        mssql_db.execute('''
            CREATE TABLE test_items (
                id INT IDENTITY(1,1) PRIMARY KEY,
                name NVARCHAR(100),
                value INT
            )
        ''')

        # Create stored procedure
        mssql_db.execute('''
            CREATE PROCEDURE GetItemByName
                @name NVARCHAR(100)
            AS
            BEGIN
                SELECT * FROM test_items WHERE name = @name
            END
        ''')

        # Insert test data
        mssql_db.execute(
            "INSERT INTO test_items (name, value) VALUES (:name, :value)",
            {"name": "test_item", "value": 42}
        )

        # Call stored procedure
        result = mssql_db.execute("EXEC GetItemByName :name", {"name": "test_item"})
        assert len(result) == 1
        assert result[0]['value'] == 42

        # Clean up
        mssql_db.execute("DROP PROCEDURE GetItemByName")


@pytest.mark.mssql
@pytest.mark.integration
class TestMSSQLAdvancedFeatures:
    """Test MSSQL advanced features"""

    def test_computed_column(self, mssql_db):
        """Test computed columns"""
        mssql_db.execute('''
            CREATE TABLE test_data (
                id INT IDENTITY(1,1) PRIMARY KEY,
                first_name NVARCHAR(50),
                last_name NVARCHAR(50),
                full_name AS (first_name + ' ' + last_name)
            )
        ''')

        mssql_db.execute(
            "INSERT INTO test_data (first_name, last_name) VALUES (:first, :last)",
            {"first": "John", "last": "Doe"}
        )

        result = mssql_db.execute("SELECT full_name FROM test_data")
        assert result[0]['full_name'] == "John Doe"

    def test_output_clause(self, mssql_db):
        """Test OUTPUT clause for getting inserted IDs"""
        mssql_db.execute('''
            CREATE TABLE test_data (
                id INT IDENTITY(1,1) PRIMARY KEY,
                name NVARCHAR(100)
            )
        ''')

        result = mssql_db.execute(
            "INSERT INTO test_data (name) OUTPUT INSERTED.id VALUES (:name)",
            {"name": "test_name"}
        )

        assert len(result) == 1
        assert result[0]['id'] > 0

    def test_case_sensitivity(self, mssql_db):
        """Test case-sensitive and case-insensitive collations"""
        # Default collation is case-insensitive
        mssql_db.execute('''
            CREATE TABLE test_data (
                id INT IDENTITY(1,1) PRIMARY KEY,
                name NVARCHAR(100)
            )
        ''')

        mssql_db.execute("INSERT INTO test_data (name) VALUES (:name)", {"name": "TestName"})

        # Case-insensitive search should find the record
        result = mssql_db.execute("SELECT * FROM test_data WHERE name = :name", {"name": "testname"})
        assert len(result) == 1

    def test_window_functions(self, mssql_db):
        """Test window functions (ROW_NUMBER, RANK, etc.)"""
        mssql_db.execute('''
            CREATE TABLE test_items (
                id INT IDENTITY(1,1) PRIMARY KEY,
                category NVARCHAR(50),
                value INT
            )
        ''')

        # Insert test data
        items = [
            ("A", 100),
            ("A", 200),
            ("B", 150),
            ("B", 250),
        ]
        for category, value in items:
            mssql_db.execute(
                "INSERT INTO test_items (category, value) VALUES (:category, :value)",
                {"category": category, "value": value}
            )

        # Use window function
        result = mssql_db.execute('''
            SELECT
                category,
                value,
                ROW_NUMBER() OVER (PARTITION BY category ORDER BY value) as row_num
            FROM test_items
            ORDER BY category, value
        ''')

        assert len(result) == 4
        assert result[0]['row_num'] == 1  # First A
        assert result[1]['row_num'] == 2  # Second A
        assert result[2]['row_num'] == 1  # First B
        assert result[3]['row_num'] == 2  # Second B
