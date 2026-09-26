"""
Tests for DatabaseManager across multiple database backends.

These tests use parameterized fixtures to run against all configured databases:
- SQLite (always available, in-memory)
- PostgreSQL (if TEST_POSTGRESQL_URL env var is set)
- MySQL (if TEST_MYSQL_URL env var is set)
- MSSQL (if TEST_MSSQL_URL env var is set)

To run with PostgreSQL:
    export TEST_POSTGRESQL_URL="postgresql://user:pass@localhost:5432/testdb"
    pytest tests/unit/test_database_multi_backend.py

To run with all databases:
    export TEST_POSTGRESQL_URL="postgresql://user:pass@localhost:5432/testdb"
    export TEST_MYSQL_URL="mysql://user:pass@localhost:3306/testdb"
    export TEST_MSSQL_URL="mssql://user:pass@localhost:1433/testdb"
    pytest tests/unit/test_database_multi_backend.py
"""

import pytest
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


class TestDatabaseBasicOperations:
    """Test basic database operations across all backends"""

    def test_connection(self, db_manager):
        """Test database connection works"""
        assert db_manager._connection is not None

    def test_execute_with_named_params(self, db_manager):
        """Test execute with named parameters"""
        # Create table
        db_manager.execute("""
            CREATE TABLE test_users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                age INTEGER
            )
        """)

        # Insert with named parameters
        result = db_manager.execute("""
            INSERT INTO test_users (name, age) VALUES (:name, :age)
        """, {"name": "Alice", "age": 30})

        assert isinstance(result, list)  # execute() returns list

    def test_fetch_one_with_named_params(self, db_manager):
        """Test fetch_one with named parameters"""
        # Create and populate table
        db_manager.execute("""
            CREATE TABLE test_products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL
            )
        """)

        db_manager.execute("""
            INSERT INTO test_products (name, price) VALUES (:name, :price)
        """, {"name": "Widget", "price": 19.99})

        # Fetch with named parameters
        row = db_manager.fetch_one("""
            SELECT * FROM test_products WHERE name = :name
        """, {"name": "Widget"})

        assert row is not None
        assert row["name"] == "Widget"
        assert abs(row["price"] - 19.99) < 0.01

    def test_fetch_all_with_named_params(self, db_manager):
        """Test fetch_all with named parameters"""
        # Create and populate table
        db_manager.execute("""
            CREATE TABLE test_items (
                id INTEGER PRIMARY KEY,
                category TEXT,
                quantity INTEGER
            )
        """)

        db_manager.execute("""
            INSERT INTO test_items (category, quantity) VALUES (:cat, :qty)
        """, {"cat": "A", "qty": 10})

        db_manager.execute("""
            INSERT INTO test_items (category, quantity) VALUES (:cat, :qty)
        """, {"cat": "B", "qty": 20})

        db_manager.execute("""
            INSERT INTO test_items (category, quantity) VALUES (:cat, :qty)
        """, {"cat": "A", "qty": 30})

        # Fetch all with filter
        rows = db_manager.execute("""
            SELECT * FROM test_items WHERE category = :category ORDER BY quantity
        """, {"category": "A"})

        assert len(rows) == 2
        assert rows[0]["quantity"] == 10
        assert rows[1]["quantity"] == 30

    @pytest.mark.asyncio
    async def test_table_exists(self, db_manager):
        """Test table_exists method"""
        # Check non-existent table
        exists = db_manager.table_exists("nonexistent_table")
        assert exists is False

        # Create table
        db_manager.execute("""
            CREATE TABLE test_existence (id INTEGER PRIMARY KEY)
        """)

        # Check existing table
        exists = db_manager.table_exists("test_existence")
        assert exists is True


class TestDatabaseMigrations:
    """Test database migrations across all backends"""

    @pytest.mark.asyncio
    async def test_migrations_table_creation(self, db_config):
        """Test that migrations tracking table is created"""
        db = DatabaseManager(db_config)

        # Initialize should create ia_migrations table
        await db.initialize(apply_schema=True)

        # Check table exists
        exists = db.table_exists("ia_migrations")
        assert exists is True

        db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_tracking(self, db_config):
        """Test that migrations are tracked properly"""
        db = DatabaseManager(db_config)
        await db.initialize(apply_schema=True)

        # Check applied migrations
        rows = db.execute("SELECT * FROM ia_migrations ORDER BY version")

        # Should have migrations from system migrations directory
        assert len(rows) > 0

        # Check that migration records have required fields
        first_migration = rows[0]
        assert "version" in first_migration
        assert "filename" in first_migration
        assert "applied_at" in first_migration
        assert "migration_type" in first_migration

        db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_idempotency(self, db_config):
        """Test that running migrations twice doesn't duplicate"""
        db = DatabaseManager(db_config)

        # Run migrations first time
        await db.initialize(apply_schema=True)
        first_count = len(db.execute("SELECT * FROM ia_migrations"))

        # Run migrations second time (should skip already applied)
        await db.initialize(apply_schema=True)
        second_count = len(db.execute("SELECT * FROM ia_migrations"))

        assert first_count == second_count

        db.disconnect()


class TestSQLTranslation:
    """Test SQL translation from PostgreSQL syntax to target database"""

    @pytest.mark.asyncio
    async def test_boolean_translation(self, db_config):
        """Test BOOLEAN type translation"""
        db = DatabaseManager(db_config)
        db.connect()

        # Clean up if table exists
        try:
            db.execute("DROP TABLE IF EXISTS test_booleans")
        except:
            pass

        # PostgreSQL syntax with BOOLEAN
        psql_syntax = """
            CREATE TABLE test_booleans (
                id INTEGER PRIMARY KEY,
                is_active BOOLEAN DEFAULT TRUE,
                is_deleted BOOLEAN DEFAULT FALSE
            )
        """

        # Should work on all databases (translated for SQLite)
        result = db.execute(psql_syntax)
        assert isinstance(result, list)  # execute() returns list

        # Cleanup
        db.execute("DROP TABLE IF EXISTS test_booleans")
        db.disconnect()

    @pytest.mark.asyncio
    async def test_jsonb_translation(self, db_config):
        """Test JSONB type translation"""
        db = DatabaseManager(db_config)
        db.connect()

        # Clean up if table exists
        try:
            db.execute("DROP TABLE IF EXISTS test_json")
        except:
            pass

        # PostgreSQL syntax with JSONB
        psql_syntax = """
            CREATE TABLE test_json (
                id INTEGER PRIMARY KEY,
                metadata JSONB
            )
        """

        # Should work on all databases (translated for SQLite)
        result = db.execute(psql_syntax)
        assert isinstance(result, list)  # execute() returns list

        # Cleanup
        db.execute("DROP TABLE IF EXISTS test_json")
        db.disconnect()

    @pytest.mark.asyncio
    async def test_varchar_translation(self, db_config):
        """Test VARCHAR type translation"""
        db = DatabaseManager(db_config)
        db.connect()

        # Clean up if table exists
        try:
            db.execute("DROP TABLE IF EXISTS test_varchar")
        except:
            pass

        # PostgreSQL syntax with VARCHAR
        psql_syntax = """
            CREATE TABLE test_varchar (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                code VARCHAR(50)
            )
        """

        # Should work on all databases (translated for SQLite)
        result = db.execute(psql_syntax)
        assert isinstance(result, list)  # execute() returns list

        # Cleanup
        db.execute("DROP TABLE IF EXISTS test_varchar")
        db.disconnect()

    @pytest.mark.asyncio
    async def test_uuid_translation(self, db_config):
        """Test UUID type translation"""
        db = DatabaseManager(db_config)
        db.connect()

        # Clean up if table exists
        try:
            db.execute("DROP TABLE IF EXISTS test_uuid")
        except:
            pass

        # PostgreSQL syntax with UUID
        psql_syntax = """
            CREATE TABLE test_uuid (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL
            )
        """

        # Should work on all databases (translated for SQLite)
        result = db.execute(psql_syntax)
        assert isinstance(result, list)  # execute() returns list

        # Cleanup
        db.execute("DROP TABLE IF EXISTS test_uuid")
        db.disconnect()

    @pytest.mark.asyncio
    async def test_timestamp_functions(self, db_config):
        """Test timestamp function translation (NOW() -> CURRENT_TIMESTAMP)"""
        db = DatabaseManager(db_config)
        db.connect()

        # Clean up if table exists
        try:
            db.execute("DROP TABLE IF EXISTS test_timestamps")
        except:
            pass

        # PostgreSQL syntax with NOW()
        psql_syntax = """
            CREATE TABLE test_timestamps (
                id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """

        # Should work on all databases (translated for SQLite)
        result = db.execute(psql_syntax)
        assert isinstance(result, list)  # execute() returns list

        # Cleanup
        db.execute("DROP TABLE IF EXISTS test_timestamps")
        db.disconnect()


class TestAsyncOperations:
    """Test async database operations across all backends"""

    @pytest.mark.asyncio
    async def test_execute_async(self, db_config):
        """Test async execute method"""
        db = DatabaseManager(db_config)
        db.connect()

        # Create table
        await db.execute_async("""
            CREATE TABLE test_async (
                id INTEGER PRIMARY KEY,
                value TEXT
            )
        """)

        # Insert data
        result = await db.execute_async("""
            INSERT INTO test_async (value) VALUES (:value)
        """, {"value": "test"})

        assert isinstance(result, list)  # execute() returns list

        db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_script(self, db_config):
        """Test execute_script with multiple statements"""
        db = DatabaseManager(db_config)
        db.connect()

        # Multiple statements in one script
        script = """
            CREATE TABLE test_script1 (id INTEGER PRIMARY KEY);
            CREATE TABLE test_script2 (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO test_script2 (name) VALUES ('test');
        """

        result = await db.execute_script(script)
        # execute_script returns QueryResult, not list
        assert result.success

        # Verify tables were created
        exists1 = db.table_exists("test_script1")
        exists2 = db.table_exists("test_script2")
        assert exists1 is True
        assert exists2 is True

        db.disconnect()
