"""
Complete database backend tests with consistent named parameters

Tests the database translation layer by:
1. Using named parameters (:param_name) consistently
2. DatabaseManager translates to database-specific format
3. Verifying data integrity across PostgreSQL, MySQL, and MSSQL
"""

import os
import pytest
from datetime import datetime
from decimal import Decimal
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def postgres_config():
    """PostgreSQL configuration"""
    url = os.environ.get(
        "TEST_POSTGRESQL_URL",
        "postgresql://testuser:testpass@localhost:15432/ia_modules_test"
    )
    return ConnectionConfig(database_type=DatabaseType.POSTGRESQL, database_url=url)


@pytest.fixture
def mysql_config():
    """MySQL configuration"""
    url = os.environ.get(
        "TEST_MYSQL_URL",
        "mysql://testuser:testpass@localhost:13306/ia_modules_test"
    )
    return ConnectionConfig(database_type=DatabaseType.MYSQL, database_url=url)


@pytest.fixture
def mssql_config():
    """MSSQL configuration"""
    url = os.environ.get(
        "TEST_MSSQL_URL",
        "mssql://sa:TestPass123!@localhost:11433/ia_modules_test"
    )
    return ConnectionConfig(database_type=DatabaseType.MSSQL, database_url=url)


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
def db(db_config):
    """Create database manager for testing"""
    from tests.conftest import ensure_mssql_database_exists

    # For MSSQL, ensure test database exists first
    if db_config.database_type == DatabaseType.MSSQL:
        ensure_mssql_database_exists(db_config.database_url)

    manager = DatabaseManager(db_config)
    if not manager.connect():
        pytest.skip(f"{db_config.database_type.value} not available")

    # Clean up test tables
    for table in ['test_orders', 'test_products', 'test_users']:
        try:
            manager.execute(f'DROP TABLE IF EXISTS {table}')
        except:
            pass

    yield manager

    # Cleanup
    for table in ['test_orders', 'test_products', 'test_users']:
        try:
            manager.execute(f'DROP TABLE IF EXISTS {table}')
        except:
            pass

    manager.disconnect()


# ============================================================================
# Helper Functions
# ============================================================================

def get_type_sql(db_type: DatabaseType, type_name: str) -> str:
    """Get database-specific SQL type"""
    types = {
        DatabaseType.POSTGRESQL: {
            'autoincrement': 'SERIAL PRIMARY KEY',
            'boolean': 'BOOLEAN DEFAULT FALSE',
            'json': 'JSONB'
        },
        DatabaseType.MYSQL: {
            'autoincrement': 'INT AUTO_INCREMENT PRIMARY KEY',
            'boolean': 'BOOLEAN DEFAULT FALSE',
            'json': 'JSON'
        },
        DatabaseType.MSSQL: {
            'autoincrement': 'INT IDENTITY(1,1) PRIMARY KEY',
            'boolean': 'BIT DEFAULT 0',
            'json': 'NVARCHAR(MAX)'
        }
    }
    return types[db_type][type_name]


def create_users_table(db):
    """Create test_users table"""
    autoincrement = get_type_sql(db.config.database_type, 'autoincrement')
    boolean = get_type_sql(db.config.database_type, 'boolean')

    db.execute(f'''
        CREATE TABLE test_users (
            id {autoincrement},
            username VARCHAR(100) NOT NULL,
            email VARCHAR(255) NOT NULL,
            age INTEGER,
            balance DECIMAL(10, 2),
            is_active {boolean},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def create_products_table(db):
    """Create test_products table"""
    autoincrement = get_type_sql(db.config.database_type, 'autoincrement')

    db.execute(f'''
        CREATE TABLE test_products (
            id {autoincrement},
            name VARCHAR(200) NOT NULL,
            description TEXT,
            price DECIMAL(10, 2) NOT NULL,
            quantity INTEGER DEFAULT 0
        )
    ''')


# ============================================================================
# Test Classes
# ============================================================================

@pytest.mark.integration
class TestBasicCRUD:
    """Test basic CRUD operations with named parameters"""

    def test_insert_with_named_params(self, db):
        """Test INSERT using named parameters"""
        create_users_table(db)

        # Insert using named parameters - DatabaseManager will translate
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {
                "username": "john_doe",
                "email": "john@example.com",
                "age": 30,
                "balance": Decimal("100.50"),
                "is_active": True
            }
        )

        # Verify
        result = db.execute("SELECT COUNT(*) as count FROM test_users")
        assert result[0]['count'] == 1

    def test_select_with_named_params(self, db):
        """Test SELECT using named parameters"""
        create_users_table(db)

        # Insert test data
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {
                "username": "alice",
                "email": "alice@example.com",
                "age": 25,
                "balance": Decimal("200.00"),
                "is_active": True
            }
        )

        # Query using named parameter
        result = db.execute(
            "SELECT * FROM test_users WHERE username = :username",
            {"username": "alice"}
        )

        assert len(result) == 1
        assert result[0]['username'] == "alice"
        assert result[0]['email'] == "alice@example.com"
        assert result[0]['age'] == 25

    def test_update_with_named_params(self, db):
        """Test UPDATE using named parameters"""
        create_users_table(db)

        # Insert
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {
                "username": "bob",
                "email": "bob@example.com",
                "age": 35,
                "balance": Decimal("150.00"),
                "is_active": True
            }
        )

        # Get ID
        user = db.execute("SELECT * FROM test_users WHERE username = :username", {"username": "bob"})[0]
        user_id = user['id']

        # Update
        db.execute(
            "UPDATE test_users SET email = :email WHERE id = :id",
            {"email": "bob_new@example.com", "id": user_id}
        )

        # Verify
        updated = db.execute("SELECT * FROM test_users WHERE id = :id", {"id": user_id})[0]
        assert updated['email'] == "bob_new@example.com"

    def test_delete_with_named_params(self, db):
        """Test DELETE using named parameters"""
        create_users_table(db)

        # Insert
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {
                "username": "charlie",
                "email": "charlie@example.com",
                "age": 40,
                "balance": Decimal("50.00"),
                "is_active": False
            }
        )

        # Get ID and delete
        user = db.execute("SELECT * FROM test_users WHERE username = :username", {"username": "charlie"})[0]
        db.execute("DELETE FROM test_users WHERE id = :id", {"id": user['id']})

        # Verify
        count = db.execute("SELECT COUNT(*) as count FROM test_users")[0]['count']
        assert count == 0


@pytest.mark.integration
class TestComplexQueries:
    """Test complex queries with multiple parameters"""

    def test_multiple_conditions(self, db):
        """Test query with multiple WHERE conditions"""
        create_users_table(db)

        # Insert test data
        users = [
            {"username": "user1", "email": "user1@example.com", "age": 20, "balance": Decimal("100.00"), "is_active": True},
            {"username": "user2", "email": "user2@example.com", "age": 25, "balance": Decimal("200.00"), "is_active": True},
            {"username": "user3", "email": "user3@example.com", "age": 30, "balance": Decimal("300.00"), "is_active": False},
            {"username": "user4", "email": "user4@example.com", "age": 35, "balance": Decimal("400.00"), "is_active": True},
        ]

        for user_data in users:
            db.execute(
                "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
                user_data
            )

        # Query with age range
        result = db.execute(
            "SELECT * FROM test_users WHERE age >= :min_age AND age <= :max_age",
            {"min_age": 22, "max_age": 32}
        )

        assert len(result) == 2
        ages = sorted([r['age'] for r in result])
        assert ages == [25, 30]

    def test_aggregation_with_params(self, db):
        """Test aggregation queries with parameters"""
        create_users_table(db)

        # Insert test data
        users = [
            {"username": "active1", "email": "a1@example.com", "age": 20, "balance": Decimal("100.00"), "is_active": True},
            {"username": "active2", "email": "a2@example.com", "age": 25, "balance": Decimal("200.00"), "is_active": True},
            {"username": "inactive1", "email": "i1@example.com", "age": 30, "balance": Decimal("300.00"), "is_active": False},
        ]

        for user_data in users:
            db.execute(
                "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
                user_data
            )

        # Count active users
        result = db.execute(
            "SELECT COUNT(*) as count FROM test_users WHERE is_active = :is_active",
            {"is_active": True}
        )
        assert result[0]['count'] == 2

        # Sum active balances
        result = db.execute(
            "SELECT SUM(balance) as total FROM test_users WHERE is_active = :is_active",
            {"is_active": True}
        )
        assert result[0]['total'] == Decimal("300.00")

    def test_join_with_params(self, db):
        """Test JOIN queries with named parameters"""
        create_users_table(db)
        create_products_table(db)

        # Insert user
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {"username": "buyer", "email": "buyer@example.com", "age": 30, "balance": Decimal("500.00"), "is_active": True}
        )

        # Insert product
        db.execute(
            "INSERT INTO test_products (name, description, price, quantity) VALUES (:name, :description, :price, :quantity)",
            {"name": "Product A", "description": "Description A", "price": Decimal("50.00"), "quantity": 100}
        )

        # Join query
        result = db.execute("""
            SELECT u.username, p.name as product_name, p.price
            FROM test_users u
            CROSS JOIN test_products p
            WHERE u.username = :username AND p.price < :max_price
        """, {"username": "buyer", "max_price": Decimal("100.00")})

        assert len(result) == 1
        assert result[0]['username'] == "buyer"
        assert result[0]['product_name'] == "Product A"


@pytest.mark.integration
class TestDataTypes:
    """Test data type handling across databases"""

    def test_boolean_storage(self, db):
        """Test boolean values are stored correctly"""
        create_users_table(db)

        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {"username": "active", "email": "active@example.com", "age": 25, "balance": Decimal("100.00"), "is_active": True}
        )

        result = db.execute("SELECT is_active FROM test_users WHERE username = :username", {"username": "active"})
        is_active = result[0]['is_active']

        # Different databases return different types for boolean
        # PostgreSQL: bool, MySQL: int (0/1), MSSQL: bytes
        assert is_active in [True, 1, b'\x01']

    def test_decimal_precision(self, db):
        """Test decimal precision is maintained"""
        create_users_table(db)

        precise_amount = Decimal("123.45")
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {"username": "precise", "email": "precise@example.com", "age": 30, "balance": precise_amount, "is_active": True}
        )

        result = db.execute("SELECT balance FROM test_users WHERE username = :username", {"username": "precise"})
        assert result[0]['balance'] == precise_amount

    def test_null_handling(self, db):
        """Test NULL values"""
        create_users_table(db)

        db.execute(
            "INSERT INTO test_users (username, email, is_active) VALUES (:username, :email, :is_active)",
            {"username": "null_user", "email": "null@example.com", "is_active": True}
        )

        result = db.execute("SELECT * FROM test_users WHERE username = :username", {"username": "null_user"})
        assert result[0]['age'] is None
        assert result[0]['balance'] is None

    def test_text_field(self, db):
        """Test large TEXT fields"""
        create_products_table(db)

        long_desc = "A" * 5000
        db.execute(
            "INSERT INTO test_products (name, description, price, quantity) VALUES (:name, :description, :price, :quantity)",
            {"name": "Long Product", "description": long_desc, "price": Decimal("99.99"), "quantity": 50}
        )

        result = db.execute("SELECT description FROM test_products WHERE name = :name", {"name": "Long Product"})
        assert len(result[0]['description']) == 5000


@pytest.mark.integration
class TestAutoCommit:
    """Test auto-commit behavior (DatabaseManager auto-commits by default)"""

    def test_multiple_inserts_committed(self, db):
        """Test that multiple inserts are auto-committed"""
        create_users_table(db)

        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {"username": "user1", "email": "user1@example.com", "age": 25, "balance": Decimal("100.00"), "is_active": True}
        )
        db.execute(
            "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
            {"username": "user2", "email": "user2@example.com", "age": 30, "balance": Decimal("200.00"), "is_active": True}
        )

        # Both should be committed
        count = db.execute("SELECT COUNT(*) as count FROM test_users")[0]['count']
        assert count == 2


@pytest.mark.integration
class TestBulkOperations:
    """Test bulk insert and update operations"""

    def test_bulk_insert(self, db):
        """Test multiple inserts"""
        create_users_table(db)

        for i in range(20):
            db.execute(
                "INSERT INTO test_users (username, email, age, balance, is_active) VALUES (:username, :email, :age, :balance, :is_active)",
                {
                    "username": f"user{i}",
                    "email": f"user{i}@example.com",
                    "age": 20 + i,
                    "balance": Decimal(f"{100 + i * 10}.00"),
                    "is_active": True
                }
            )

        count = db.execute("SELECT COUNT(*) as count FROM test_users")[0]['count']
        assert count == 20

    def test_bulk_update(self, db):
        """Test bulk updates with parameters"""
        create_products_table(db)

        # Insert products
        for i in range(10):
            db.execute(
                "INSERT INTO test_products (name, description, price, quantity) VALUES (:name, :description, :price, :quantity)",
                {
                    "name": f"Product {i}",
                    "description": f"Description {i}",
                    "price": Decimal(f"{10 + i}.99"),
                    "quantity": i * 5
                }
            )

        # Bulk update
        db.execute(
            "UPDATE test_products SET price = price * :multiplier WHERE quantity < :max_qty",
            {"multiplier": Decimal("1.1"), "max_qty": 20}
        )

        # Verify
        result = db.execute(
            "SELECT * FROM test_products WHERE quantity < :max_qty AND price > :min_price",
            {"max_qty": 20, "min_price": Decimal("11.00")}
        )
        assert len(result) > 0
