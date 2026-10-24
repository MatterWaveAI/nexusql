# NexusQL Database Reference Guide

Complete reference documentation for NexusQL multi-database abstraction layer.

## Table of Contents

- [Quick Start](#quick-start)
- [Database Compatibility](#database-compatibility)
- [Query Patterns](#query-patterns)
- [Testing Guide](#testing-guide)
- [SQL Dialect Differences](#sql-dialect-differences)
- [Migration System](#migration-system)

---

## Quick Start

### Installation

```bash
pip install nexusql

# With optional database drivers
pip install nexusql[postgresql]  # PostgreSQL support
pip install nexusql[mysql]       # MySQL support
pip install nexusql[mssql]       # MSSQL support
pip install nexusql[all]         # All drivers
```

### Basic Usage

```python
from nexusql import DatabaseManager

# Connect to any database with the same API
db = DatabaseManager("postgresql://localhost/mydb")
# db = DatabaseManager("mysql://localhost/mydb")
# db = DatabaseManager("sqlite:///mydb.db")
# db = DatabaseManager("mssql://localhost/mydb")

db.connect()

# Write queries once, run anywhere
users = db.execute(
    "SELECT * FROM users WHERE age > :min_age",
    {"min_age": 18}
)

db.disconnect()
```

---

## Database Compatibility

### Supported Databases

| Database | Status | Driver | Notes |
|----------|--------|--------|-------|
| **SQLite** | ✅ Full | Built-in | Perfect for dev/testing |
| **PostgreSQL** | ✅ Full | psycopg 3 | Recommended for production; sync and async |
| **MySQL** | ✅ Full | pymysql | Full compatibility |
| **MSSQL** | ✅ Full | pyodbc | Requires ODBC driver |

### Query Compatibility Matrix

| Feature | SQLite | PostgreSQL | MySQL | MSSQL | Notes |
|---------|--------|------------|-------|-------|-------|
| Named params `:name` | ✅ | ✅ | ✅ | ✅ | Auto-converted |
| SELECT queries | ✅ | ✅ | ✅ | ✅ | 100% compatible |
| INSERT queries | ✅ | ✅ | ✅ | ✅ | 100% compatible |
| UPDATE queries | ✅ | ✅ | ✅ | ✅ | 100% compatible |
| DELETE queries | ✅ | ✅ | ✅ | ✅ | 100% compatible |
| JOINs | ✅ | ✅ | ✅ | ✅ | Standard SQL |
| Transactions | ✅ | ✅ | ✅ | ✅ | Auto-commit by default |
| Subqueries | ✅ | ✅ | ✅ | ✅ | Standard SQL |
| Window functions | ✅ | ✅ | ✅ | ✅ | SQLite 3.25+ |

---

## Query Patterns

### Basic CRUD Operations

```python
from nexusql import DatabaseManager

db = DatabaseManager("postgresql://localhost/mydb")
db.connect()

# CREATE
db.execute("""
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        age INTEGER
    )
""")

# INSERT with named parameters
db.execute(
    "INSERT INTO users (name, email, age) VALUES (:name, :email, :age)",
    {"name": "Alice", "email": "alice@example.com", "age": 30}
)

# SELECT one row
user = db.fetch_one(
    "SELECT * FROM users WHERE email = :email",
    {"email": "alice@example.com"}
)
print(user)  # {'id': 1, 'name': 'Alice', 'email': 'alice@example.com', 'age': 30}

# SELECT all rows
users = db.execute(
    "SELECT * FROM users WHERE age >= :min_age",
    {"min_age": 25}
)

# UPDATE
db.execute(
    "UPDATE users SET age = :age WHERE email = :email",
    {"age": 31, "email": "alice@example.com"}
)

# DELETE
db.execute(
    "DELETE FROM users WHERE id = :id",
    {"id": 1}
)

db.disconnect()
```

### Complex Queries

```python
# JOINs
results = db.execute("""
    SELECT u.name, o.total, o.created_at
    FROM users u
    JOIN orders o ON u.id = o.user_id
    WHERE o.total > :min_total
    ORDER BY o.created_at DESC
""", {"min_total": 100})

# Aggregations
stats = db.fetch_one("""
    SELECT
        COUNT(*) as total_users,
        AVG(age) as avg_age,
        MAX(age) as max_age
    FROM users
    WHERE active = :active
""", {"active": 1})

# Subqueries
power_users = db.execute("""
    SELECT u.*
    FROM users u
    WHERE u.id IN (
        SELECT user_id
        FROM orders
        GROUP BY user_id
        HAVING SUM(total) > :threshold
    )
""", {"threshold": 1000})
```

### Parameterized Queries

Always use named parameters (`:param_name`) for safety and compatibility:

```python
# ✅ GOOD - Safe from SQL injection
db.execute(
    "SELECT * FROM users WHERE name = :name AND age > :age",
    {"name": user_input, "age": 18}
)

# ❌ BAD - SQL injection risk
db.execute(f"SELECT * FROM users WHERE name = '{user_input}'")

# ✅ GOOD - Multiple parameters in any order
db.execute(
    "UPDATE users SET name = :name, email = :email WHERE id = :id",
    {"id": 1, "name": "Bob", "email": "bob@example.com"}
)
```

---

## Testing Guide

### Docker Test Environment

NexusQL provides a Docker Compose setup for testing against real databases.

#### Setup

```bash
# 1. Start test databases
docker-compose -f tests/docker-compose.test.yml up -d

# 2. Set environment variables (one-liner)
export TEST_POSTGRESQL_URL="postgresql://testuser:testpass@localhost:15432/nexusql_test" TEST_MYSQL_URL="mysql://testuser:testpass@localhost:13306/nexusql_test" TEST_MSSQL_URL="mssql://sa:TestPass123!@localhost:11433/master?TrustServerCertificate=yes"

# Or set them separately:
export TEST_POSTGRESQL_URL="postgresql://testuser:testpass@localhost:15432/nexusql_test"
export TEST_MYSQL_URL="mysql://testuser:testpass@localhost:13306/nexusql_test"
export TEST_MSSQL_URL="mssql://sa:TestPass123!@localhost:11433/master?TrustServerCertificate=yes"

# 3. Run tests
pytest tests/integration/ -v
```

#### Multi-Database Testing

```python
import pytest
from nexusql import DatabaseManager

# Test against all databases
@pytest.fixture(params=[
    "sqlite:///:memory:",
    "postgresql://localhost/test",
    "mysql://localhost/test",
    "mssql://localhost/test"
])
def db(request):
    """Runs test on all database types"""
    database = DatabaseManager(request.param)
    if database.connect():
        yield database
        database.disconnect()
    else:
        pytest.skip(f"Database not available: {request.param}")

def test_crud_operations(db):
    """This test runs 4 times - once per database"""
    # Create table
    db.execute("""
        CREATE TABLE test_table (
            id INTEGER PRIMARY KEY,
            value TEXT
        )
    """)

    # Insert
    db.execute(
        "INSERT INTO test_table (id, value) VALUES (:id, :val)",
        {"id": 1, "val": "test"}
    )

    # Select
    result = db.fetch_one("SELECT * FROM test_table WHERE id = :id", {"id": 1})
    assert result["value"] == "test"

    # Update
    db.execute(
        "UPDATE test_table SET value = :val WHERE id = :id",
        {"val": "updated", "id": 1}
    )

    # Delete
    db.execute("DELETE FROM test_table WHERE id = :id", {"id": 1})

    # Verify
    result = db.fetch_one("SELECT * FROM test_table WHERE id = :id", {"id": 1})
    assert result is None
```

---

## SQL Dialect Differences

### Data Types

| Feature | PostgreSQL | SQLite | MySQL | MSSQL |
|---------|-----------|--------|--------|-------|
| **Auto-increment** | `SERIAL` | `INTEGER AUTOINCREMENT` | `AUTO_INCREMENT` | `IDENTITY` |
| **Boolean** | `BOOLEAN` | `INTEGER` | `TINYINT(1)` | `BIT` |
| **Text** | `TEXT`, `VARCHAR` | `TEXT` | `TEXT`, `VARCHAR` | `VARCHAR`, `NVARCHAR` |
| **JSON** | `JSONB`, `JSON` | `TEXT` | `JSON` | `NVARCHAR(MAX)` |
| **UUID** | `UUID` | `TEXT` | `CHAR(36)` | `UNIQUEIDENTIFIER` |
| **Timestamp** | `TIMESTAMP` | `TEXT` | `DATETIME` | `DATETIME2` |

### Query Differences

| Operation | PostgreSQL | SQLite | MySQL | MSSQL |
|-----------|-----------|--------|--------|-------|
| **String concat** | `\|\|` or `CONCAT()` | `\|\|` | `CONCAT()` | `+` or `CONCAT()` |
| **Case insensitive** | `ILIKE` | `LIKE` | `LIKE` | `LIKE` (depends on collation) |
| **Limit** | `LIMIT n` | `LIMIT n` | `LIMIT n` | `TOP n` |
| **Date/Time now** | `NOW()` | `CURRENT_TIMESTAMP` | `NOW()` | `GETDATE()` |
| **Random** | `RANDOM()` | `RANDOM()` | `RAND()` | `NEWID()` |

### Parameter Placeholders

NexusQL handles this automatically, but here's what happens behind the scenes:

| Database | Native Placeholder | NexusQL Input | Example |
|----------|-------------------|---------------|---------|
| **PostgreSQL** | `$1, $2, $3` | `:name, :age` | Converted automatically |
| **SQLite** | `?, ?, ?` | `:name, :age` | Converted automatically |
| **MySQL** | `%s, %s, %s` | `:name, :age` | Converted automatically |
| **MSSQL** | `?, ?, ?` | `:name, :age` | Converted automatically |

---

## Migration System

### File-Based Migrations

NexusQL uses versioned SQL migration files:

```
migrations/
  ├── V001__create_users.sql
  ├── V002__add_email_index.sql
  └── V003__create_orders.sql
```

### Running Migrations

```python
from nexusql import DatabaseManager

db = DatabaseManager("postgresql://localhost/mydb")

# Run migrations on startup
await db.initialize(
    apply_schema=True,
    app_migration_paths=["path/to/migrations"]
)
```

### Migration File Format

```sql
-- migrations/V001__create_users.sql

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE,
    age INTEGER,
    active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_email ON users(email);
```

### Migration Tracking

NexusQL automatically tracks applied migrations in a `nexusql_migrations` table:

```sql
CREATE TABLE nexusql_migrations (
    version VARCHAR(50) PRIMARY KEY,
    description VARCHAR(255),
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Best Practices

1. **Use sequential version numbers**: `V001`, `V002`, `V003`, etc.
2. **Descriptive names**: `V001__create_users.sql` not `V001.sql`
3. **One change per migration**: Don't bundle unrelated changes
4. **Test migrations**: Run on test database first
5. **No rollbacks in production**: Only forward migrations
6. **Keep migrations immutable**: Never edit applied migrations

---

## API Reference

### DatabaseManager

```python
class DatabaseManager:
    def __init__(self, database_url: str)
    def connect() -> bool
    def disconnect() -> None
    async def close() -> None
    def transaction()                      # context manager: with db.transaction(): ...
    def execute(query: str, params: Dict = None) -> List[Dict]
    async def execute_async(query: str, params: Dict = None) -> List[Dict]
    def fetch_one(query: str, params: Dict = None) -> Optional[Dict]
    async def fetch_one_async(query: str, params: Dict = None) -> Optional[Dict]
    def create_table(table_name: str, schema: str) -> None
    def table_exists(table_name: str) -> bool
    async def initialize(apply_schema: bool = True, app_migration_paths: List[str] = None) -> bool
    async def execute_script(script: str) -> QueryResult
```

### Connection URLs

```python
# SQLite
db = DatabaseManager("sqlite:///path/to/database.db")
db = DatabaseManager("sqlite:///:memory:")  # In-memory

# PostgreSQL
db = DatabaseManager("postgresql://user:password@host:port/database")
db = DatabaseManager("postgresql://localhost/mydb")

# MySQL
db = DatabaseManager("mysql://user:password@host:port/database")
db = DatabaseManager("mysql://localhost/mydb")

# MSSQL
db = DatabaseManager("mssql://user:password@host:port/database")
db = DatabaseManager("mssql://localhost/mydb")
```

---

## Examples

### Multi-Tenant Application

```python
class TenantDatabase:
    def __init__(self, tenant_id: str):
        # Each tenant can use different database
        configs = {
            "tenant_a": "postgresql://server1/tenant_a",
            "tenant_b": "mysql://server2/tenant_b",
            "tenant_c": "mssql://server3/tenant_c",
        }

        self.db = DatabaseManager(configs[tenant_id])
        self.db.connect()

    def get_users(self, min_age: int):
        # Same query works for all tenants
        return self.db.execute(
            "SELECT * FROM users WHERE age >= :min_age",
            {"min_age": min_age}
        )
```

### Development to Production

```python
import os

# Development: SQLite
if os.getenv("ENV") == "development":
    db = DatabaseManager("sqlite:///dev.db")

# Staging: PostgreSQL
elif os.getenv("ENV") == "staging":
    db = DatabaseManager(os.getenv("DATABASE_URL"))

# Production: PostgreSQL with replica
else:
    db = DatabaseManager(os.getenv("DATABASE_URL"))

db.connect()

# Same queries everywhere!
users = db.execute("SELECT * FROM users WHERE active = :active", {"active": 1})
```

---

## Troubleshooting

### Common Issues

**Connection fails:**
```python
# Check connection
if not db.connect():
    print("Failed to connect")
    # Check: database URL format, credentials, database exists, network
```

**Import error:**
```python
# Install required driver
pip install "psycopg[binary,pool]"  # PostgreSQL
pip install pymysql          # MySQL
pip install pyodbc           # MSSQL
```

**SQL injection warning:**
```python
# ❌ Never use string formatting
query = f"SELECT * FROM users WHERE name = '{user_input}'"

# ✅ Always use parameters
query = "SELECT * FROM users WHERE name = :name"
db.execute(query, {"name": user_input})
```

**Query works on SQLite but not PostgreSQL:**
- Check data types (INTEGER vs SERIAL)
- Check string concat (`||` vs `CONCAT()`)
- Check limit syntax (`LIMIT` vs `TOP`)

---

## Performance Tips

1. **Use indexes**: Add indexes on frequently queried columns
2. **Parameterize queries**: Reuse prepared statements
3. **Batch operations**: Use transactions for multiple inserts
4. **Limit result sets**: Use `LIMIT` for large tables
5. **Profile slow queries**: Use `EXPLAIN` to analyze query plans

---

## Additional Resources

- **GitHub**: https://github.com/matterwaveai/nexusql
- **Issues**: https://github.com/matterwaveai/nexusql/issues
- **Documentation**: https://github.com/matterwaveai/nexusql#readme
- **Roadmap**: [POTENTIAL_DATABASE_ISSUES.md](POTENTIAL_DATABASE_ISSUES.md)
