"""
Regression tests for database edge cases and parameter handling

Tests for issues not covered by existing test suites:
- NULL parameter handling
- Datetime parameter handling
- Empty string vs NULL
- Boolean parameters
- Unicode and special characters
- fetch_one() and execute() with various data types
"""

import pytest
from datetime import datetime, timezone
from nexusql import DatabaseManager


@pytest.fixture(params=["sqlite", "postgresql", "mysql", "mssql"], ids=lambda x: x)
def db(request):
    """Provide a database manager for each database type"""
    from nexusql import DatabaseManager
    import os

    url_map = {
        'sqlite': f"sqlite:///:memory:",
        'postgresql': os.getenv("TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:15432/ia_modules_test"),
        'mysql': os.getenv("TEST_MYSQL_URL", "mysql://testuser:testpass@localhost:13306/ia_modules_test"),
        'mssql': os.getenv("TEST_MSSQL_URL", "mssql://sa:TestPass123!@localhost:11433/master?TrustServerCertificate=yes")
    }

    db_type = request.param
    db_url = url_map[db_type]
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


def test_null_parameter_handling(db):
    """Test that NULL parameters work correctly across all databases"""
    # Drop and create table
    try:
        db.execute("DROP TABLE IF EXISTS test_nulls")
    except:
        pass

    db.execute("""
        CREATE TABLE test_nulls (
            id INTEGER PRIMARY KEY,
            nullable_col TEXT,
            not_null_col TEXT
        )
    """)

    # Insert with NULL parameter
    db.execute(
        "INSERT INTO test_nulls (id, nullable_col, not_null_col) VALUES (:id, :null_val, :val)",
        {'id': 1, 'null_val': None, 'val': 'not null'}
    )

    # Verify NULL was inserted
    result = db.fetch_one("SELECT * FROM test_nulls WHERE id = :id", {'id': 1})
    assert result is not None
    assert result['nullable_col'] is None
    assert result['not_null_col'] == 'not null'


def test_datetime_parameter_handling(db):
    """Test datetime parameters work across all databases"""
    try:
        db.execute("DROP TABLE IF EXISTS test_dates")
    except:
        pass

    db.execute("""
        CREATE TABLE test_dates (
            id INTEGER PRIMARY KEY,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
    """)

    # Insert with datetime objects
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO test_dates (id, created_at, updated_at) VALUES (:id, :created, :updated)",
        {'id': 1, 'created': now, 'updated': now}
    )

    # Verify dates were inserted
    result = db.fetch_one("SELECT * FROM test_dates WHERE id = :id", {'id': 1})
    assert result is not None
    assert result['created_at'] is not None
    assert result['updated_at'] is not None


def test_empty_string_vs_null(db):
    """Test empty string is distinct from NULL"""
    try:
        db.execute("DROP TABLE IF EXISTS test_empty_strings")
    except:
        pass

    db.execute("""
        CREATE TABLE test_empty_strings (
            id INTEGER PRIMARY KEY,
            empty_str TEXT,
            null_val TEXT
        )
    """)

    # Insert empty string and NULL
    db.execute(
        "INSERT INTO test_empty_strings (id, empty_str, null_val) VALUES (:id, :empty, :null)",
        {'id': 1, 'empty': '', 'null': None}
    )

    # Verify they're different
    result = db.fetch_one("SELECT * FROM test_empty_strings WHERE id = :id", {'id': 1})
    assert result is not None
    assert result['empty_str'] == ''
    assert result['null_val'] is None


def test_boolean_parameter_handling(db):
    """Test boolean parameters work correctly"""
    try:
        db.execute("DROP TABLE IF EXISTS test_booleans")
    except:
        pass

    # Create table with appropriate boolean type for each database
    from nexusql import DatabaseType
    if db.config.database_type == DatabaseType.POSTGRESQL:
        db.execute("""
            CREATE TABLE test_booleans (
                id INTEGER PRIMARY KEY,
                is_active BOOLEAN,
                is_deleted BOOLEAN
            )
        """)
    elif db.config.database_type == DatabaseType.MYSQL:
        db.execute("""
            CREATE TABLE test_booleans (
                id INTEGER PRIMARY KEY,
                is_active BOOLEAN,
                is_deleted BOOLEAN
            )
        """)
    else:
        # SQLite and MSSQL use INTEGER for booleans
        db.execute("""
            CREATE TABLE test_booleans (
                id INTEGER PRIMARY KEY,
                is_active INTEGER,
                is_deleted INTEGER
            )
        """)

    # Insert with boolean values
    db.execute(
        "INSERT INTO test_booleans (id, is_active, is_deleted) VALUES (:id, :active, :deleted)",
        {'id': 1, 'active': True, 'deleted': False}
    )

    # Verify booleans were stored correctly
    result = db.fetch_one("SELECT * FROM test_booleans WHERE id = :id", {'id': 1})
    assert result is not None
    # Different databases return different types
    assert result['is_active'] in (True, 1)
    assert result['is_deleted'] in (False, 0)


def test_unicode_and_special_characters(db):
    """Test Unicode, emojis, and special characters in parameters"""
    try:
        db.execute("DROP TABLE IF EXISTS test_unicode")
    except:
        pass

    db.execute("""
        CREATE TABLE test_unicode (
            id INTEGER PRIMARY KEY,
            unicode_text TEXT
        )
    """)

    # Insert various Unicode characters
    test_strings = [
        '你好世界',  # Chinese
        'Привет мир',  # Russian
        '🎉🚀🎈',  # Emojis
        'Café',  # Accented characters
        'Tab\\tNew\\nLine',  # Escaped characters
    ]

    for i, text in enumerate(test_strings, 1):
        db.execute(
            "INSERT INTO test_unicode (id, unicode_text) VALUES (:id, :text)",
            {'id': i, 'text': text}
        )

    # Verify all were inserted correctly
    results = db.execute("SELECT * FROM test_unicode ORDER BY id")
    assert len(results) == len(test_strings)
    for i, result in enumerate(results):
        assert result['unicode_text'] == test_strings[i]


def test_fetch_one_with_mssql(db):
    """Test fetch_one() works correctly with MSSQL dict conversion"""
    try:
        db.execute("DROP TABLE IF EXISTS test_fetch_one")
    except:
        pass

    db.execute("""
        CREATE TABLE test_fetch_one (
            id INTEGER PRIMARY KEY,
            name TEXT,
            value INTEGER
        )
    """)

    db.execute(
        "INSERT INTO test_fetch_one (id, name, value) VALUES (:id, :name, :val)",
        {'id': 1, 'name': 'test', 'val': 42}
    )

    # Test fetch_one returns dict correctly
    result = db.fetch_one("SELECT * FROM test_fetch_one WHERE id = :id", {'id': 1})
    assert result is not None
    assert isinstance(result, dict)
    assert result['id'] == 1
    assert result['name'] == 'test'
    assert result['value'] == 42

    # Test fetch_one returns None when no match
    result = db.fetch_one("SELECT * FROM test_fetch_one WHERE id = :id", {'id': 999})
    assert result is None


def test_fetch_all_with_mssql(db):
    """Test execute() works correctly with MSSQL dict conversion"""
    try:
        db.execute("DROP TABLE IF EXISTS test_fetch_all")
    except:
        pass

    db.execute("""
        CREATE TABLE test_fetch_all (
            id INTEGER PRIMARY KEY,
            category TEXT,
            score INTEGER
        )
    """)

    # Insert multiple rows
    for i in range(1, 4):
        db.execute(
            "INSERT INTO test_fetch_all (id, category, score) VALUES (:id, :cat, :score)",
            {'id': i, 'cat': f'cat_{i}', 'score': i * 10}
        )

    # Test execute returns list of dicts
    results = db.execute("SELECT * FROM test_fetch_all ORDER BY id")
    assert len(results) == 3
    assert all(isinstance(r, dict) for r in results)

    for i, result in enumerate(results, 1):
        assert result['id'] == i
        assert result['category'] == f'cat_{i}'
        assert result['score'] == i * 10

    # Test execute returns empty list when no matches
    results = db.execute("SELECT * FROM test_fetch_all WHERE id > :id", {'id': 999})
    assert results == []


def test_multiple_nulls_in_same_query(db):
    """Test multiple NULL parameters in same query"""
    try:
        db.execute("DROP TABLE IF EXISTS test_multi_nulls")
    except:
        pass

    db.execute("""
        CREATE TABLE test_multi_nulls (
            id INTEGER PRIMARY KEY,
            col1 TEXT,
            col2 TEXT,
            col3 TEXT,
            col4 TEXT
        )
    """)

    # Insert with multiple NULLs
    db.execute("""
        INSERT INTO test_multi_nulls (id, col1, col2, col3, col4)
        VALUES (:id, :c1, :c2, :c3, :c4)
    """, {
        'id': 1,
        'c1': None,
        'c2': 'value',
        'c3': None,
        'c4': None
    })

    result = db.fetch_one("SELECT * FROM test_multi_nulls WHERE id = :id", {'id': 1})
    assert result['col1'] is None
    assert result['col2'] == 'value'
    assert result['col3'] is None
    assert result['col4'] is None


def test_parameter_with_quotes_and_special_chars(db):
    """Test parameters containing quotes and special SQL characters"""
    try:
        db.execute("DROP TABLE IF EXISTS test_special_chars")
    except:
        pass

    db.execute("""
        CREATE TABLE test_special_chars (
            id INTEGER PRIMARY KEY,
            text_data TEXT
        )
    """)

    # Insert strings with special characters
    special_strings = [
        "O'Reilly",  # Single quote
        'He said "Hello"',  # Double quotes
        "50% off; DROP TABLE users--",  # SQL injection attempt
        "Line1\\nLine2",  # Newline
        "Tab\\there",  # Tab
    ]

    for i, text in enumerate(special_strings, 1):
        db.execute(
            "INSERT INTO test_special_chars (id, text_data) VALUES (:id, :text)",
            {'id': i, 'text': text}
        )

    # Verify all were inserted correctly
    results = db.execute("SELECT * FROM test_special_chars ORDER BY id")
    assert len(results) == len(special_strings)
    for i, result in enumerate(results):
        assert result['text_data'] == special_strings[i]


def test_very_long_string_parameter(db):
    """Test parameter with very long string (test size limits)"""
    try:
        db.execute("DROP TABLE IF EXISTS test_long_strings")
    except:
        pass

    db.execute("""
        CREATE TABLE test_long_strings (
            id INTEGER PRIMARY KEY,
            long_text TEXT
        )
    """)

    # Create a 10KB string
    long_string = 'A' * 10000

    db.execute(
        "INSERT INTO test_long_strings (id, long_text) VALUES (:id, :text)",
        {'id': 1, 'text': long_string}
    )

    result = db.fetch_one("SELECT * FROM test_long_strings WHERE id = :id", {'id': 1})
    assert result is not None
    assert len(result['long_text']) == 10000
    assert result['long_text'] == long_string


def test_numeric_parameter_types(db):
    """Test various numeric parameter types"""
    try:
        db.execute("DROP TABLE IF EXISTS test_numbers")
    except:
        pass

    db.execute("""
        CREATE TABLE test_numbers (
            id INTEGER PRIMARY KEY,
            int_val INTEGER,
            float_val REAL
        )
    """)

    # Insert with int and float
    db.execute(
        "INSERT INTO test_numbers (id, int_val, float_val) VALUES (:id, :int, :float)",
        {'id': 1, 'int': 42, 'float': 3.14159}
    )

    result = db.fetch_one("SELECT * FROM test_numbers WHERE id = :id", {'id': 1})
    assert result['int_val'] == 42
    assert abs(result['float_val'] - 3.14159) < 0.00001
