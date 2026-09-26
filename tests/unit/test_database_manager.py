"""Tests for DatabaseManager with named parameters"""

import pytest
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


class TestDatabaseManager:
    """Test DatabaseManager with SQLite (in-memory)"""

    @pytest.fixture
    def db_manager(self):
        """Create in-memory SQLite database"""
        config = ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url="sqlite://:memory:"
        )
        db = DatabaseManager(config)
        db.connect()
        yield db
        db.disconnect()

    def test_connection(self, db_manager):
        """Test database connection"""
        assert db_manager._connection is not None

    def test_execute_async(self, db_manager):
        """Test execute query with named parameters"""
        # Create table
        db_manager.execute("""
            CREATE TABLE test_users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                age INTEGER
            )
        """)

        # Insert with named parameters - returns empty list on success
        result = db_manager.execute("""
            INSERT INTO test_users (name, age) VALUES (:name, :age)
        """, {"name": "Alice", "age": 30})

        # For non-SELECT queries, execute returns an empty list on success
        assert isinstance(result, list)

    def test_fetch_one(self, db_manager):
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
        assert row["price"] == 19.99

    def test_fetch_all(self, db_manager):
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
            CREATE TABLE test_table (id INTEGER PRIMARY KEY)
        """)

        # Check existing table
        exists = db_manager.table_exists("test_table")
        assert exists is True

    def test_parameter_conversion(self, db_manager):
        """Test that named parameters are converted correctly"""
        db_manager.execute("""
            CREATE TABLE test_params (
                name TEXT,
                value1 INTEGER,
                value2 TEXT,
                value3 REAL
            )
        """)

        # Insert with multiple named parameters
        result = db_manager.execute("""
            INSERT INTO test_params (name, value1, value2, value3)
            VALUES (:name, :v1, :v2, :v3)
        """, {
            "name": "test",
            "v1": 42,
            "v2": "hello",
            "v3": 3.14
        })

        # For non-SELECT queries, execute returns an empty list on success
        assert isinstance(result, list)

        # Fetch back
        row = db_manager.fetch_one("""
            SELECT * FROM test_params WHERE name = :name
        """, {"name": "test"})

        assert row["value1"] == 42
        assert row["value2"] == "hello"
        assert abs(row["value3"] - 3.14) < 0.01


class TestDatabaseMigrations:
    """Test database migrations"""

    @pytest.mark.asyncio
    async def test_migrations_table_creation(self):
        """Test that migrations tracking table is created"""
        config = ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url="sqlite://:memory:"
        )
        db = DatabaseManager(config)

        # Initialize should create ia_migrations table
        await db.initialize(apply_schema=True)

        # Check table exists
        exists = db.table_exists("ia_migrations")
        assert exists is True

        db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_tracking(self, tmp_path):
        """An applied migration is recorded in ia_migrations with its metadata.

        The migration comes from a directory this test owns. It used to rely on
        ``nexusql/migrations/``, which shipped an application schema inside a
        generic library; c569e17 deleted it and the test kept asserting rows
        appeared, so it failed on an empty table. A library that ships no
        migrations of its own is the intended state -- what matters is that a
        migration handed to it is applied once and recorded.
        """
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "V001__create_widget.sql").write_text(
            "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT);"
        )

        config = ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url="sqlite://:memory:"
        )
        db = DatabaseManager(config)
        await db.initialize(apply_schema=True, app_migration_paths=[str(migrations)])

        rows = db.execute("SELECT * FROM ia_migrations ORDER BY version")
        assert len(rows) == 1

        applied = rows[0]
        assert "version" in applied
        assert "filename" in applied
        assert "applied_at" in applied
        assert applied["filename"] == "V001__create_widget.sql"

        # The migration actually ran, not just got recorded.
        assert db.table_exists("widget") is True

        db.disconnect()
