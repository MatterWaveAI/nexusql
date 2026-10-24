# NexusQL Quick Start

## Installation

```bash
pip install -e .
```

## 30-Second Example

```python
from nexusql import DatabaseManager

# Connect to SQLite (no setup required)
db = DatabaseManager("sqlite:///myapp.db")
db.connect()

# Create a table (works on all databases!)
db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL UNIQUE,
        name TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )
""")

# Insert data with named parameters (SQL injection safe!)
db.execute(
    "INSERT INTO users (email, name) VALUES (:email, :name)",
    {"email": "alice@example.com", "name": "Alice"}
)

# Query data
users = db.execute(
    "SELECT * FROM users WHERE email = :email",
    {"email": "alice@example.com"}
)
print(users)  # [{'id': 1, 'email': 'alice@example.com', ...}]

# Fetch single row
user = db.fetch_one(
    "SELECT * FROM users WHERE id = :id",
    {"id": 1}
)
print(user)  # {'id': 1, 'email': 'alice@example.com', ...}

db.disconnect()
```

## Works Identically on All Databases

```python
# SQLite (file-based, no server)
db = DatabaseManager("sqlite:///myapp.db")

# PostgreSQL
db = DatabaseManager("postgresql://user:pass@localhost:5432/mydb")

# MySQL
db = DatabaseManager("mysql://user:pass@localhost:3306/mydb")

# MSSQL
db = DatabaseManager("mssql://user:pass@localhost:1433/mydb")

# Use the exact same code for all!
db.connect()
db.execute("SELECT * FROM users WHERE id = :id", {"id": 1})
db.disconnect()
```

## Key Features

### 1. Named Parameters (SQL Injection Protection)

```python
# ✅ Safe - use named parameters
db.execute(
    "SELECT * FROM users WHERE email = :email",
    {"email": user_input}
)

# ❌ Unsafe - never do this!
db.execute(f"SELECT * FROM users WHERE email = '{user_input}'")
```

### 2. Automatic SQL Translation

Write once in PostgreSQL syntax - works everywhere:

```python
# This works on SQLite, MySQL, and MSSQL too!
db.execute("""
    CREATE TABLE products (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255),
        active BOOLEAN DEFAULT TRUE,
        price NUMERIC(10,2),
        metadata JSONB,
        created_at TIMESTAMP DEFAULT NOW()
    )
""")

# NexusQL automatically translates:
# - SERIAL → INT AUTO_INCREMENT (MySQL), INT IDENTITY (MSSQL), INTEGER AUTOINCREMENT (SQLite)
# - BOOLEAN → TINYINT(1) (MySQL), BIT (MSSQL), INTEGER (SQLite)
# - JSONB → JSON (MySQL), NVARCHAR(MAX) (MSSQL), TEXT (SQLite)
# - NOW() → GETDATE() (MSSQL), CURRENT_TIMESTAMP (SQLite)
```

### 3. Migration System

```python
# Run migrations on startup
await db.initialize(
    apply_schema=True,
    app_migration_paths=["path/to/migrations"]
)

# Migrations are tracked and only run once
# Files: V001__initial.sql, V002__add_users.sql, etc.
```

### 4. Context Manager Support

```python
# Auto-connect and disconnect
with DatabaseManager("sqlite:///myapp.db") as db:
    users = db.execute("SELECT * FROM users")
    # auto-disconnect on exit
```

### 5. Async Support

```python
# Async/await compatible
await db.initialize(apply_schema=True)
result = await db.execute_async("SELECT * FROM users")
await db.close()
```

## Common Patterns

### Check if Table Exists

```python
if db.table_exists("users"):
    print("Users table exists")
else:
    db.execute("CREATE TABLE users (...)")
```

### Insert and Get ID

```python
# return_id=True hands back the key the database generated for the new row.
# One call on every backend: SQLite lastrowid, PostgreSQL RETURNING,
# MySQL LAST_INSERT_ID(), MSSQL SCOPE_IDENTITY() are handled for you.
user_id = db.execute(
    "INSERT INTO users (email, name) VALUES (:email, :name)",
    {"email": "bob@example.com", "name": "Bob"},
    return_id=True,
)

# Returns None when nothing was inserted (e.g. INSERT .. ON CONFLICT DO NOTHING).
# If you write your own RETURNING clause, its first column is returned instead.
```

### Batch Inserts

```python
users = [
    {"email": "user1@example.com", "name": "User 1"},
    {"email": "user2@example.com", "name": "User 2"},
    {"email": "user3@example.com", "name": "User 3"},
]

for user in users:
    db.execute(
        "INSERT INTO users (email, name) VALUES (:email, :name)",
        user
    )
```

### Execute vs Fetch One

```python
# All rows: execute returns the result set as a list of dicts
all_users = db.execute("SELECT * FROM users")
# Returns: [{'id': 1, ...}, {'id': 2, ...}]

# Fetch single row
one_user = db.fetch_one("SELECT * FROM users WHERE id = :id", {"id": 1})
# Returns: {'id': 1, 'email': '...', ...}

# Fetch one returns None if not found
missing = db.fetch_one("SELECT * FROM users WHERE id = :id", {"id": 999})
# Returns: None
```

### Execute Script (Multiple Statements)

```python
script = """
CREATE TABLE categories (id SERIAL PRIMARY KEY, name TEXT);
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id)
);
INSERT INTO categories (name) VALUES ('Electronics');
"""

result = await db.execute_script(script)
if result.success:
    print("Script executed successfully")
```

## Database-Specific Notes

### SQLite
- No server required
- Great for development, testing, and small applications
- File-based: `sqlite:///path/to/file.db`
- In-memory: `sqlite:///:memory:`

### PostgreSQL
- Install driver: `pip install nexusql[postgresql]` (psycopg 3, sync and async)
- Connection string: `postgresql://user:pass@host:port/dbname`
- Best for production, supports advanced features

### MySQL
- Install driver: `pip install pymysql`
- Connection string: `mysql://user:pass@host:port/dbname`
- Popular choice, wide hosting support

### MSSQL
- Install driver: `pip install pyodbc`
- Connection string: `mssql://user:pass@host:port/dbname`
- Windows-friendly, good for enterprise

## Next Steps

- Read [README.md](README.md) for comprehensive documentation
- Check [tests/](tests/) for more examples
- See [INSTALLATION.md](INSTALLATION.md) for advanced setup

## Need Help?

```python
# Print version
import nexusql
print(nexusql.__version__)

# Check what's available
from nexusql import *
print(__all__)
# ['DatabaseManager', 'ConnectionConfig', 'DatabaseType', 'QueryResult', ...]
```

## License

MIT - Use freely in your projects!
