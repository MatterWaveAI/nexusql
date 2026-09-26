"""
Comprehensive database backend tests with query repository

Tests database translation layer across PostgreSQL, MySQL, and MSSQL with:
1. Standard query repository that works across all databases
2. Data verification using raw SQL queries
3. Complete CRUD operations testing
4. Transaction handling
5. Data type compatibility
6. Performance characteristics
"""

import os
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Any
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


# ============================================================================
# Test Query Repository - Translated queries that work across all databases
# ============================================================================

class QueryRepository:
    """Repository of test queries with database-agnostic placeholders"""

    # Table creation queries
    CREATE_USERS_TABLE = """
        CREATE TABLE test_users (
            id {autoincrement_pk},
            username VARCHAR(100) NOT NULL,
            email VARCHAR(255) NOT NULL,
            age INTEGER,
            balance DECIMAL(10, 2),
            is_active {boolean},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata {json_type}
        )
    """

    CREATE_PRODUCTS_TABLE = """
        CREATE TABLE test_products (
            id {autoincrement_pk},
            name VARCHAR(200) NOT NULL,
            description TEXT,
            price DECIMAL(10, 2) NOT NULL,
            quantity INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """

    CREATE_ORDERS_TABLE = """
        CREATE TABLE test_orders (
            id {autoincrement_pk},
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            total_price DECIMAL(10, 2) NOT NULL,
            status VARCHAR(50) DEFAULT 'pending',
            ordered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """

    # CRUD queries using named placeholders (: prefix)
    INSERT_USER = "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)"
    INSERT_PRODUCT = "INSERT INTO test_products (name, description, price, quantity) VALUES (:name, :description, :price, :quantity)"
    INSERT_ORDER = "INSERT INTO test_orders (user_id, product_id, quantity, total_price, status) VALUES (:user_id, :product_id, :quantity, :total_price, :status)"

    SELECT_USER_BY_ID = "SELECT * FROM test_users WHERE id = :id"
    SELECT_USER_BY_USERNAME = "SELECT * FROM test_users WHERE username = :username"
    SELECT_ACTIVE_USERS = "SELECT * FROM test_users WHERE is_active = :is_active"
    SELECT_USERS_BY_AGE_RANGE = "SELECT * FROM test_users WHERE age >= :min_age AND age <= :max_age"

    UPDATE_USER_EMAIL = "UPDATE test_users SET email = :email WHERE id = :id"
    UPDATE_USER_BALANCE = "UPDATE test_users SET balance = balance + :amount WHERE id = :id"
    UPDATE_PRODUCT_QUANTITY = "UPDATE test_products SET quantity = quantity - :quantity WHERE id = :id"

    DELETE_USER_BY_ID = "DELETE FROM test_users WHERE id = :id"
    DELETE_INACTIVE_USERS = "DELETE FROM test_users WHERE is_active = :is_active"

    # Complex queries
    SELECT_USERS_WITH_HIGH_BALANCE = "SELECT * FROM test_users WHERE balance > :min_balance ORDER BY balance DESC"
    SELECT_PRODUCTS_LOW_STOCK = "SELECT * FROM test_products WHERE quantity < :max_quantity ORDER BY quantity ASC"

    # Join queries
    SELECT_ORDERS_WITH_DETAILS = """
        SELECT o.id, o.quantity, o.total_price, o.status,
               u.username, u.email,
               p.name as product_name, p.price
        FROM test_orders o
        JOIN test_users u ON o.user_id = u.id
        JOIN test_products p ON o.product_id = p.id
        WHERE o.status = :status
    """

    # Aggregation queries
    COUNT_USERS = "SELECT COUNT(*) as count FROM test_users"
    COUNT_ACTIVE_USERS = "SELECT COUNT(*) as count FROM test_users WHERE is_active = :is_active"
    SUM_USER_BALANCES = "SELECT SUM(balance) as total FROM test_users WHERE is_active = :is_active"
    AVG_PRODUCT_PRICE = "SELECT AVG(price) as avg_price FROM test_products"

    # Group by queries
    COUNT_USERS_BY_AGE = "SELECT age, COUNT(*) as count FROM test_users GROUP BY age ORDER BY age"
    SUM_ORDERS_BY_USER = "SELECT user_id, SUM(total_price) as total FROM test_orders GROUP BY user_id"


# ============================================================================
# Raw SQL Query Repository - For data verification (database-specific)
# ============================================================================

class RawQueryRepository:
    """Raw SQL queries for data verification - database specific"""

    POSTGRES = {
        'count_users': "SELECT COUNT(*) FROM test_users",
        'get_user_by_id': "SELECT * FROM test_users WHERE id = :id",
        'verify_boolean': "SELECT is_active FROM test_users WHERE id = :id",
        'verify_json': "SELECT metadata FROM test_users WHERE id = :id",
        'table_exists': "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = :table_name)",
    }

    MYSQL = {
        'count_users': "SELECT COUNT(*) FROM test_users",
        'get_user_by_id': "SELECT * FROM test_users WHERE id = :id",
        'verify_boolean': "SELECT is_active FROM test_users WHERE id = :id",
        'verify_json': "SELECT metadata FROM test_users WHERE id = :id",
        'table_exists': "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :table_name",
    }

    MSSQL = {
        'count_users': "SELECT COUNT(*) FROM test_users",
        'get_user_by_id': "SELECT * FROM test_users WHERE id = :id",
        'verify_boolean': "SELECT is_active FROM test_users WHERE id = :id",
        'verify_json': "SELECT metadata FROM test_users WHERE id = :id",
        'table_exists': "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :table_name",
    }


# ============================================================================
# Database Configuration Fixtures
# ============================================================================

@pytest.fixture
def postgres_config():
    """PostgreSQL configuration"""
    url = os.environ.get(
        "TEST_POSTGRESQL_URL",
        "postgresql://testuser:testpass@localhost:15432/ia_modules_test"
    )
    return ConnectionConfig(
        database_type=DatabaseType.POSTGRESQL,
        database_url=url
    )


@pytest.fixture
def mysql_config():
    """MySQL configuration"""
    url = os.environ.get(
        "TEST_MYSQL_URL",
        "mysql://testuser:testpass@localhost:13306/ia_modules_test"
    )
    return ConnectionConfig(
        database_type=DatabaseType.MYSQL,
        database_url=url
    )


@pytest.fixture
def mssql_config():
    """MSSQL configuration"""
    url = os.environ.get(
        "TEST_MSSQL_URL",
        "mssql://sa:TestPass123!@localhost:11433/ia_modules_test"
    )
    return ConnectionConfig(
        database_type=DatabaseType.MSSQL,
        database_url=url
    )


@pytest.fixture(params=['postgres', 'mysql', 'mssql'])
def db_config(request, postgres_config, mysql_config, mssql_config):
    """Parametrized fixture for all database types"""
    configs = {
        'postgres': postgres_config,
        'mysql': mysql_config,
        'mssql': mssql_config
    }
    return configs[request.param]


@pytest.fixture
def db_manager(db_config):
    """Create database manager for testing"""
    from tests.conftest import ensure_mssql_database_exists

    # For MSSQL, ensure the test database exists first. This runs before connect(),
    # so it must not block when the server is absent -- it has no timeout of its own
    # and hung the whole suite when a services: block was ignored by the runner.
    if db_config.database_type == DatabaseType.MSSQL:
        try:
            ensure_mssql_database_exists(db_config.database_url)
        except Exception as exc:
            pytest.skip(f"mssql not reachable: {exc}")

    from tests.conftest import skip_unless_reachable
    skip_unless_reachable(db_config.database_url, db_config.database_type.value)

    db = DatabaseManager(db_config)
    if not db.connect():
        pytest.skip(f"{db_config.database_type.value} not available")

    # Clean up test tables
    test_tables = ['test_orders', 'test_products', 'test_users']
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


# ============================================================================
# Test Classes
# ============================================================================

@pytest.mark.integration
class TestDatabaseTranslations:
    """Test that translated queries work across all database backends"""

    def test_create_table_with_types(self, db_manager):
        """Test table creation with various data types"""
        # Get the appropriate type mappings for this database
        type_map = self._get_type_mappings(db_manager.config.database_type)

        create_sql = QueryRepository.CREATE_USERS_TABLE.format(**type_map)
        db_manager.execute(create_sql)

        # Verify table was created using raw SQL
        result = self._verify_table_exists(db_manager, 'test_users')
        assert result, "Table test_users should exist"

    def test_insert_translated_query(self, db_manager):
        """Test INSERT with parameter translation"""
        self._create_users_table(db_manager)

        # Use translated query with ? placeholders
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "john_doe", "email": "john@example.com", "age": 30, "balance": Decimal("100.50"), "is_active": True}
        )

        # Verify data using raw SQL
        count = self._get_user_count(db_manager)
        assert count == 1

    def test_select_translated_query(self, db_manager):
        """Test SELECT with parameter translation"""
        self._create_users_table(db_manager)

        # Insert test data
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "alice", "email": "alice@example.com", "age": 25, "balance": Decimal("200.00"), "is_active": True}
        )

        # Query using translated placeholders
        result = db_manager.execute(
            QueryRepository.SELECT_USER_BY_USERNAME,
            {"username": "alice"}
        )

        assert len(result) == 1
        assert result[0]['username'] == "alice"
        assert result[0]['email'] == "alice@example.com"

    def test_update_translated_query(self, db_manager):
        """Test UPDATE with parameter translation"""
        self._create_users_table(db_manager)

        # Insert and get ID
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "bob", "email": "bob@example.com", "age": 35, "balance": Decimal("150.00"), "is_active": True}
        )

        user = db_manager.execute(QueryRepository.SELECT_USER_BY_USERNAME, {"username": "bob"})[0]
        user_id = user['id']

        # Update email
        db_manager.execute(
            QueryRepository.UPDATE_USER_EMAIL,
            {"email": "bob_new@example.com", "id": user_id}
        )

        # Verify update
        updated = db_manager.execute(QueryRepository.SELECT_USER_BY_ID, {"id": user_id})[0]
        assert updated['email'] == "bob_new@example.com"

    def test_delete_translated_query(self, db_manager):
        """Test DELETE with parameter translation"""
        self._create_users_table(db_manager)

        # Insert test data
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "charlie", "email": "charlie@example.com", "age": 40, "balance": Decimal("50.00"), "is_active": False}
        )

        user = db_manager.execute(QueryRepository.SELECT_USER_BY_USERNAME, {"username": "charlie"})[0]
        user_id = user['id']

        # Delete user
        db_manager.execute(QueryRepository.DELETE_USER_BY_ID, {"id": user_id})

        # Verify deletion
        count = self._get_user_count(db_manager)
        assert count == 0

    def test_complex_select_with_conditions(self, db_manager):
        """Test complex SELECT with multiple conditions"""
        self._create_users_table(db_manager)

        # Insert multiple users
        users = [
            {"username": "user1", "email": "user1@example.com", "age": 20, "balance": Decimal("100.00"), "is_active": True},
            {"username": "user2", "email": "user2@example.com", "age": 25, "balance": Decimal("200.00"), "is_active": True},
            {"username": "user3", "email": "user3@example.com", "age": 30, "balance": Decimal("300.00"), "is_active": False},
            {"username": "user4", "email": "user4@example.com", "age": 35, "balance": Decimal("400.00"), "is_active": True},
        ]

        for user_data in users:
            db_manager.execute(QueryRepository.INSERT_USER, user_data)

        # Query age range
        result = db_manager.execute(
            QueryRepository.SELECT_USERS_BY_AGE_RANGE,
            {"min_age": 22, "max_age": 32}
        )

        assert len(result) == 2
        ages = [r['age'] for r in result]
        assert 25 in ages and 30 in ages

    def test_aggregation_queries(self, db_manager):
        """Test COUNT, SUM, AVG aggregations"""
        self._create_users_table(db_manager)

        # Insert test data
        users = [
            {"username": "user1", "email": "user1@example.com", "age": 20, "balance": Decimal("100.00"), "is_active": True},
            {"username": "user2", "email": "user2@example.com", "age": 25, "balance": Decimal("200.00"), "is_active": True},
            {"username": "user3", "email": "user3@example.com", "age": 30, "balance": Decimal("300.00"), "is_active": False},
        ]

        for user_data in users:
            db_manager.execute(QueryRepository.INSERT_USER, user_data)

        # Count all users
        result = db_manager.execute(QueryRepository.COUNT_USERS)
        assert result[0]['count'] == 3

        # Count active users
        result = db_manager.execute(QueryRepository.COUNT_ACTIVE_USERS, {"is_active": True})
        assert result[0]['count'] == 2

        # Sum active user balances
        result = db_manager.execute(QueryRepository.SUM_USER_BALANCES, {"is_active": True})
        assert result[0]['total'] == Decimal("300.00")

    def test_join_queries(self, db_manager):
        """Test JOIN queries across tables"""
        # Create all tables
        self._create_users_table(db_manager)
        self._create_products_table(db_manager)
        self._create_orders_table(db_manager)

        # Insert test data
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "buyer1", "email": "buyer1@example.com", "age": 30, "balance": Decimal("500.00"), "is_active": True}
        )
        db_manager.execute(
            QueryRepository.INSERT_PRODUCT,
            {"name": "Product A", "description": "Description A", "price": Decimal("50.00"), "quantity": 100}
        )

        # Get IDs
        user = db_manager.execute(QueryRepository.SELECT_USER_BY_USERNAME, {"username": "buyer1"})[0]
        product = db_manager.execute("SELECT * FROM test_products WHERE name = :name", {"name": "Product A"})[0]

        # Create order
        db_manager.execute(
            QueryRepository.INSERT_ORDER,
            {"user_id": user['id'], "product_id": product['id'], "quantity": 2, "total_price": Decimal("100.00"), "status": "pending"}
        )

        # Query with JOIN
        result = db_manager.execute(
            QueryRepository.SELECT_ORDERS_WITH_DETAILS,
            {"status": "pending"}
        )

        assert len(result) == 1
        assert result[0]['username'] == "buyer1"
        assert result[0]['product_name'] == "Product A"
        assert result[0]['quantity'] == 2


@pytest.mark.integration
class TestDataVerification:
    """Test data integrity using raw SQL queries"""

    def test_boolean_type_storage(self, db_manager):
        """Verify boolean values are stored correctly"""
        self._create_users_table(db_manager)

        # Insert user with is_active=True
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "active_user", "email": "active@example.com", "age": 25, "balance": Decimal("100.00"), "is_active": True}
        )

        # Verify using raw SQL
        user = db_manager.execute(QueryRepository.SELECT_USER_BY_USERNAME, {"username": "active_user"})[0]
        user_id = user['id']

        result = self._execute_raw_query(
            db_manager,
            'verify_boolean',
            {"id": user_id}
        )

        # PostgreSQL returns bool, MySQL returns int (0/1), MSSQL returns bit
        is_active = result[0]['is_active']
        assert is_active in [True, 1, b'\x01'], f"Expected boolean true, got {is_active}"

    def test_decimal_type_precision(self, db_manager):
        """Verify decimal precision is maintained"""
        self._create_users_table(db_manager)

        precise_amount = Decimal("123.45")
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "precise_user", "email": "precise@example.com", "age": 30, "balance": precise_amount, "is_active": True}
        )

        result = db_manager.execute(
            QueryRepository.SELECT_USER_BY_USERNAME,
            {"username": "precise_user"}
        )

        assert result[0]['balance'] == precise_amount

    def test_timestamp_storage(self, db_manager):
        """Verify timestamp handling"""
        self._create_users_table(db_manager)

        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "timestamp_user", "email": "ts@example.com", "age": 25, "balance": Decimal("100.00"), "is_active": True}
        )

        result = db_manager.execute(
            QueryRepository.SELECT_USER_BY_USERNAME,
            {"username": "timestamp_user"}
        )

        assert 'created_at' in result[0]
        assert result[0]['created_at'] is not None
        assert isinstance(result[0]['created_at'], datetime)

    def test_null_value_handling(self, db_manager):
        """Verify NULL values are handled correctly"""
        self._create_users_table(db_manager)

        # Insert with NULL age and balance
        db_manager.execute(
            "INSERT INTO test_users (username, email, is_active) VALUES (:username, :email, :is_active)",
            {"username": "null_user", "email": "null@example.com", "is_active": True}
        )

        result = db_manager.execute(
            QueryRepository.SELECT_USER_BY_USERNAME,
            {"username": "null_user"}
        )

        assert result[0]['age'] is None
        assert result[0]['balance'] is None

    def test_text_field_storage(self, db_manager):
        """Verify TEXT fields store large content"""
        self._create_products_table(db_manager)

        long_description = "A" * 5000  # 5000 character description

        db_manager.execute(
            QueryRepository.INSERT_PRODUCT,
            {"name": "Long Product", "description": long_description, "price": Decimal("99.99"), "quantity": 50}
        )

        result = db_manager.execute(
            "SELECT * FROM test_products WHERE name = :name",
            {"name": "Long Product"}
        )

        assert result[0]['description'] == long_description
        assert len(result[0]['description']) == 5000


@pytest.mark.integration
class TestTransactionConsistency:
    """Test transaction handling across databases"""

    def test_commit_transaction(self, db_manager):
        """Test transaction commit"""
        self._create_users_table(db_manager)

        with db_manager.transaction():
            db_manager.execute(
                QueryRepository.INSERT_USER,
                {"username": "tx_user1", "email": "tx1@example.com", "age": 25, "balance": Decimal("100.00"), "is_active": True}
            )
            db_manager.execute(
                QueryRepository.INSERT_USER,
                {"username": "tx_user2", "email": "tx2@example.com", "age": 30, "balance": Decimal("200.00"), "is_active": True}
            )

        # Verify both users exist
        count = self._get_user_count(db_manager)
        assert count == 2

    def test_rollback_transaction(self, db_manager):
        """Test transaction rollback"""
        self._create_users_table(db_manager)

        # Insert one user outside transaction
        db_manager.execute(
            QueryRepository.INSERT_USER,
            {"username": "existing_user", "email": "existing@example.com", "age": 25, "balance": Decimal("100.00"), "is_active": True}
        )

        # Roll back by raising out of the transaction block.
        class _Abort(Exception):
            pass

        with pytest.raises(_Abort):
            with db_manager.transaction():
                db_manager.execute(
                    QueryRepository.INSERT_USER,
                    {"username": "tx_user", "email": "tx@example.com", "age": 30, "balance": Decimal("200.00"), "is_active": True}
                )
                raise _Abort()

        # Verify only the first user exists
        count = self._get_user_count(db_manager)
        assert count == 1

        result = db_manager.execute(QueryRepository.SELECT_USER_BY_USERNAME, {"username": "existing_user"})
        assert len(result) == 1


@pytest.mark.integration
class TestConcurrentOperations:
    """Test concurrent database operations"""

    def test_multiple_inserts(self, db_manager):
        """Test multiple inserts in sequence"""
        self._create_users_table(db_manager)

        for i in range(10):
            db_manager.execute(
                QueryRepository.INSERT_USER,
                {"username": f"user{i}", "email": f"user{i}@example.com", "age": 20 + i, "balance": Decimal(f"{100 + i * 10}.00"), "is_active": True}
            )

        count = self._get_user_count(db_manager)
        assert count == 10

    def test_bulk_operations(self, db_manager):
        """Test bulk insert and update operations"""
        self._create_products_table(db_manager)

        # Bulk insert
        for i in range(50):
            db_manager.execute(
                QueryRepository.INSERT_PRODUCT,
                {"name": f"Product {i}", "description": f"Description {i}", "price": Decimal(f"{10 + i}.99"), "quantity": i * 10}
            )

        # Verify count
        result = db_manager.execute("SELECT COUNT(*) as count FROM test_products")
        assert result[0]['count'] == 50

        # Bulk update
        db_manager.execute(
            "UPDATE test_products SET price = price * :multiplier WHERE quantity < :max_quantity",
            {"multiplier": Decimal("1.1"), "max_quantity": 100}
        )

        # Verify updates
        result = db_manager.execute(
            "SELECT * FROM test_products WHERE quantity < :max_quantity AND price > :min_price",
            {"max_quantity": 100, "min_price": Decimal("10.00")}
        )
        assert len(result) > 0


# ============================================================================
# Helper Methods
# ============================================================================

def _get_type_mappings(db_type: DatabaseType) -> Dict[str, str]:
    """Get database-specific type mappings"""
    mappings = {
        DatabaseType.POSTGRESQL: {
            'autoincrement_pk': 'SERIAL PRIMARY KEY',
            'boolean': 'BOOLEAN DEFAULT FALSE',
            'json_type': 'JSONB'
        },
        DatabaseType.MYSQL: {
            'autoincrement_pk': 'INT AUTO_INCREMENT PRIMARY KEY',
            'boolean': 'BOOLEAN DEFAULT FALSE',
            'json_type': 'JSON'
        },
        DatabaseType.MSSQL: {
            'autoincrement_pk': 'INT IDENTITY(1,1) PRIMARY KEY',
            'boolean': 'BIT DEFAULT 0',
            'json_type': 'NVARCHAR(MAX)'
        }
    }
    return mappings.get(db_type, mappings[DatabaseType.POSTGRESQL])


def _create_users_table(db_manager):
    """Helper to create users table"""
    type_map = _get_type_mappings(db_manager.config.database_type)
    create_sql = QueryRepository.CREATE_USERS_TABLE.format(**type_map)
    db_manager.execute(create_sql)


def _create_products_table(db_manager):
    """Helper to create products table"""
    type_map = _get_type_mappings(db_manager.config.database_type)
    create_sql = QueryRepository.CREATE_PRODUCTS_TABLE.format(**type_map)
    db_manager.execute(create_sql)


def _create_orders_table(db_manager):
    """Helper to create orders table"""
    type_map = _get_type_mappings(db_manager.config.database_type)
    create_sql = QueryRepository.CREATE_ORDERS_TABLE.format(**type_map)
    db_manager.execute(create_sql)


def _get_user_count(db_manager) -> int:
    """Get count of users using raw SQL"""
    result = db_manager.execute("SELECT COUNT(*) as count FROM test_users")
    return result[0]['count']


def _verify_table_exists(db_manager, table_name: str) -> bool:
    """Verify table exists using database-specific query"""
    db_type = db_manager.config.database_type

    if db_type == DatabaseType.POSTGRESQL:
        result = db_manager.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = :table_name)",
            {"table_name": table_name}
        )
        return result[0]['exists']
    elif db_type == DatabaseType.MYSQL:
        result = db_manager.execute(
            "SELECT COUNT(*) as count FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = :table_name",
            {"table_name": table_name}
        )
        return result[0]['count'] > 0
    elif db_type == DatabaseType.MSSQL:
        result = db_manager.execute(
            "SELECT COUNT(*) as count FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :table_name",
            {"table_name": table_name}
        )
        return result[0]['count'] > 0

    return False


def _execute_raw_query(db_manager, query_name: str, params: dict = None):
    """Execute raw SQL query for data verification"""
    db_type = db_manager.config.database_type

    queries = {
        DatabaseType.POSTGRESQL: RawQueryRepository.POSTGRES,
        DatabaseType.MYSQL: RawQueryRepository.MYSQL,
        DatabaseType.MSSQL: RawQueryRepository.MSSQL
    }

    query = queries[db_type].get(query_name)
    if not query:
        raise ValueError(f"Query '{query_name}' not found for {db_type}")

    return db_manager.execute(query, params)


# Add helper methods to test classes
TestDatabaseTranslations._get_type_mappings = staticmethod(_get_type_mappings)
TestDatabaseTranslations._create_users_table = staticmethod(_create_users_table)
TestDatabaseTranslations._create_products_table = staticmethod(_create_products_table)
TestDatabaseTranslations._create_orders_table = staticmethod(_create_orders_table)
TestDatabaseTranslations._get_user_count = staticmethod(_get_user_count)
TestDatabaseTranslations._verify_table_exists = staticmethod(_verify_table_exists)

TestDataVerification._create_users_table = staticmethod(_create_users_table)
TestDataVerification._create_products_table = staticmethod(_create_products_table)
TestDataVerification._execute_raw_query = staticmethod(_execute_raw_query)

TestTransactionConsistency._create_users_table = staticmethod(_create_users_table)
TestTransactionConsistency._get_user_count = staticmethod(_get_user_count)

TestConcurrentOperations._create_users_table = staticmethod(_create_users_table)
TestConcurrentOperations._create_products_table = staticmethod(_create_products_table)
TestConcurrentOperations._get_user_count = staticmethod(_get_user_count)
