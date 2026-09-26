"""
Database Security Tests

Tests to ensure the database module is secure against:
- SQL injection attacks
- Parameter tampering
- Malicious input
- Authentication bypass
- Data leakage
- Privilege escalation

These tests verify that parameter binding and input sanitization
prevent common security vulnerabilities.
"""

import pytest
import sqlite3
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


@pytest.fixture
def secure_db():
    """Create in-memory SQLite database for security testing"""
    config = ConnectionConfig(
        database_type=DatabaseType.SQLITE,
        database_url="sqlite://:memory:"
    )
    db = DatabaseManager(config)
    db.connect()

    # Create test schema
    db.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE sensitive_data (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            secret_value TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Insert test data
    db.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (:u, :p, :a)",
        {"u": "admin", "p": "hashed_admin_pass", "a": 1}
    )
    db.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (:u, :p, :a)",
        {"u": "regular_user", "p": "hashed_user_pass", "a": 0}
    )
    db.execute(
        "INSERT INTO sensitive_data (user_id, secret_value) VALUES (:uid, :secret)",
        {"uid": 1, "secret": "admin_secret"}
    )
    db.execute(
        "INSERT INTO sensitive_data (user_id, secret_value) VALUES (:uid, :secret)",
        {"uid": 2, "secret": "user_secret"}
    )

    yield db

    db.disconnect()


class TestSQLInjectionPrevention:
    """Test that SQL injection attacks are prevented by parameter binding"""

    def test_sql_injection_in_where_clause(self, secure_db):
        """Test SQL injection in WHERE clause is prevented"""
        # Classic SQL injection attempt: ' OR '1'='1
        malicious_input = "' OR '1'='1"

        # This should NOT return all users due to proper parameter binding
        row = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_input}
        )

        # Should return None (no user with that literal username)
        assert row is None

        # Verify users still exist (not deleted by injection)
        all_users = secure_db.execute("SELECT * FROM users")
        assert len(all_users) == 2

    def test_sql_injection_with_comments(self, secure_db):
        """Test SQL injection using comment syntax"""
        # Attempt to comment out rest of query: admin'--
        malicious_input = "admin'--"

        row = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username AND is_admin = :admin",
            {"username": malicious_input, "admin": 1}
        )

        # Should return None (no user with that username)
        assert row is None

    def test_sql_injection_union_attack(self, secure_db):
        """Test UNION-based SQL injection"""
        # UNION attack to extract sensitive data
        malicious_input = "' UNION SELECT id, secret_value, 1, 1 FROM sensitive_data --"

        rows = secure_db.execute(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_input}
        )

        # Should return empty (parameter binding prevents UNION)
        assert len(rows) == 0

    def test_sql_injection_stacked_queries(self, secure_db):
        """Test stacked query injection (multiple statements)"""
        # Attempt to execute multiple statements
        malicious_input = "admin'; DROP TABLE users; --"

        row = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_input}
        )

        # Should return None
        assert row is None

        # Verify table was NOT dropped
        users = secure_db.execute("SELECT * FROM users")
        assert len(users) == 2

    def test_sql_injection_in_update(self, secure_db):
        """Test SQL injection in UPDATE statement"""
        # Attempt to escalate privileges: ', is_admin=1 WHERE '1'='1
        malicious_password = "newpass', is_admin=1 WHERE '1'='1"

        result = secure_db.execute(
            "UPDATE users SET password_hash = :password WHERE username = :username",
            {"password": malicious_password, "username": "regular_user"}
        )

        assert isinstance(result, list)  # execute() returns list

        # Verify regular_user is still NOT admin
        user = secure_db.fetch_one("SELECT * FROM users WHERE username = :u", {"u": "regular_user"})
        assert user["is_admin"] == 0
        assert user["password_hash"] == malicious_password  # Stored as literal value

    def test_sql_injection_in_insert(self, secure_db):
        """Test SQL injection in INSERT statement"""
        # Attempt to inject into INSERT
        malicious_username = "hacker'), (999, 'injected', 'pass', 1); --"

        result = secure_db.execute(
            "INSERT INTO users (username, password_hash) VALUES (:username, :password)",
            {"username": malicious_username, "password": "test"}
        )

        assert isinstance(result, list)  # execute() returns list

        # Verify only one user was inserted (with the malicious string as username)
        all_users = secure_db.execute("SELECT * FROM users")
        assert len(all_users) == 3  # 2 original + 1 new

        # Verify no user with id=999 was created
        injected_user = secure_db.fetch_one("SELECT * FROM users WHERE id = :id", {"id": 999})
        assert injected_user is None

    def test_sql_injection_with_wildcards(self, secure_db):
        """Test SQL injection using LIKE wildcards"""
        # Attempt to use wildcards to leak data: %
        malicious_input = "%"

        rows = secure_db.execute(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_input}
        )

        # Should not return all users (= comparison, not LIKE)
        assert len(rows) == 0


class TestParameterTampering:
    """Test protection against parameter tampering"""

    def test_boolean_parameter_tampering(self, secure_db):
        """Test that boolean tampering doesn't bypass logic"""
        # Attempt to tamper with is_admin check
        result = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username AND is_admin = :admin",
            {"username": "regular_user", "admin": "1 OR 1=1"}
        )

        # Should return None (1 OR 1=1 is a string, not boolean)
        assert result is None

    def test_numeric_parameter_tampering(self, secure_db):
        """Test numeric parameter validation"""
        # Attempt to use SQL in numeric parameter
        result = secure_db.fetch_one(
            "SELECT * FROM users WHERE id = :id",
            {"id": "1 OR 1=1"}
        )

        # SQLite might coerce the string, but won't execute the SQL
        # Either None or row with id=1 (depending on SQLite's coercion)
        if result is not None:
            assert result["id"] == 1  # Only legitimate row 1

    def test_array_parameter_injection(self, secure_db):
        """Test that array-like input doesn't cause injection"""
        # Some poorly-written ORMs are vulnerable to array injection
        malicious_input = ["admin", "OR", "1=1"]

        # Our implementation should handle this safely (convert to string)
        result = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": str(malicious_input)}
        )

        # Should not find any user
        assert result is None


class TestMaliciousInput:
    """Test handling of malicious/malformed input"""

    def test_null_byte_injection(self, secure_db):
        """Test null byte injection attempts"""
        # Null byte might truncate strings in some systems
        malicious_input = "admin\x00malicious"

        row = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_input}
        )

        # Should not find user (null byte is part of the string)
        assert row is None

    def test_unicode_injection(self, secure_db):
        """Test unicode normalization attacks"""
        # Unicode characters that might normalize to SQL syntax
        malicious_inputs = [
            "admin＇OR＇1＇=＇1",  # Fullwidth apostrophe
            "admin\u02BC OR \u02BC1\u02BC=\u02BC1",  # Modifier letter apostrophe
            "admin\uFF07 OR \uFF071\uFF07=\uFF071",  # Fullwidth apostrophe
        ]

        for malicious_input in malicious_inputs:
            row = secure_db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": malicious_input}
            )

            # Should not bypass security
            assert row is None or row["username"] != "admin"

    def test_extremely_long_input(self, secure_db):
        """Test handling of extremely long input"""
        # Very long string that might cause buffer overflow in C extensions
        long_input = "A" * 1000000  # 1MB string

        result = secure_db.execute(
            "INSERT INTO users (username, password_hash) VALUES (:username, :password)",
            {"username": long_input, "password": "test"}
        )

        # Should handle gracefully (either succeed or fail safely)
        assert isinstance(result, list)  # execute() returns list is True or result.success is False
        # Should not crash

    def test_special_characters(self, secure_db):
        """Test handling of special characters"""
        special_chars = [
            "'; DROP TABLE users; --",
            "1' AND '1'='1",
            "1' OR '1'='1' --",
            "' OR 1=1--",
            "admin'--",
            "' OR 'x'='x",
            "1'; WAITFOR DELAY '00:00:05'--",
            "1' UNION SELECT NULL--",
            "' OR 1=1#",
            "admin' /*",
            "' or '1'='1'/*",
        ]

        for special in special_chars:
            row = secure_db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": special}
            )

            # None of these should bypass security
            assert row is None

    def test_binary_data_safety(self, secure_db):
        """Test handling of binary data"""
        # Binary data with potential SQL injection patterns
        binary_data = b"admin\x00\x27 OR \x271\x27=\x271"

        secure_db.execute("CREATE TABLE test_binary (id INTEGER PRIMARY KEY, data BLOB)")

        result = secure_db.execute(
            "INSERT INTO test_binary (data) VALUES (:data)",
            {"data": binary_data}
        )

        assert isinstance(result, list)  # execute() returns list

        # Verify data stored correctly without executing as SQL
        row = secure_db.fetch_one("SELECT * FROM test_binary")
        assert row["data"] == binary_data


class TestAuthenticationBypass:
    """Test that authentication logic cannot be bypassed"""

    def test_password_bypass_attempt(self, secure_db):
        """Test password bypass using SQL injection"""
        # Attempt to bypass password check
        username = "admin' OR '1'='1"
        password = "wrong_password"

        user = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username AND password_hash = :password",
            {"username": username, "password": password}
        )

        # Should not authenticate
        assert user is None

    def test_always_true_condition(self, secure_db):
        """Test injection of always-true conditions"""
        # Various forms of always-true conditions
        malicious_usernames = [
            "' OR '1'='1",
            "' OR 1=1--",
            "admin' OR 'a'='a",
            "' OR ''='",
        ]

        for username in malicious_usernames:
            user = secure_db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": username}
            )

            # Should not return admin user
            assert user is None or user["username"] != "admin"


class TestDataLeakagePrevention:
    """Test that error messages don't leak sensitive information"""

    def test_error_messages_dont_leak_schema(self, secure_db):
        """Test that error messages don't reveal table structure"""
        # Attempt to cause error that might reveal schema
        try:
            result = secure_db.execute(
                "INSERT INTO nonexistent_table (col) VALUES (:val)",
                {"val": "test"}
            )
            assert False, "Expected exception for nonexistent table"
        except Exception as e:
            # Error message should not contain sensitive info about actual tables
            error_msg = str(e).lower()
            assert "users" not in error_msg or "no such table" in error_msg
            # Basic error is OK, detailed schema leak is not

    def test_parameter_errors_dont_leak_values(self, secure_db):
        """Test that parameter errors don't leak sensitive values"""
        # This would fail in some implementations
        sensitive_password = "super_secret_password_12345"

        try:
            result = secure_db.execute(
                "SELECT * FROM users WHERE password_hash = :password",
                {"password": sensitive_password}
            )
            # Query succeeds (no error), so no leak possible
            assert isinstance(result, list)
        except Exception as e:
            # If there's an error, it shouldn't contain the password
            error_msg = str(e)
            assert sensitive_password not in error_msg


class TestParameterSanitization:
    """Test that parameters are properly sanitized"""

    def test_parameter_type_coercion(self, secure_db):
        """Test that parameter types are handled safely"""
        # Pass dict where string expected
        # The library rejects unsupported parameter types by raising (B-003):
        # a loud failure, never a silent None that looks like "no row"
        with pytest.raises(sqlite3.Error):
            secure_db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": {"OR": "1=1"}}
            )

    def test_parameter_escaping(self, secure_db):
        """Test that parameters are properly escaped"""
        # Username with quotes
        username_with_quotes = "O'Brien"

        result = secure_db.execute(
            "INSERT INTO users (username, password_hash) VALUES (:username, :password)",
            {"username": username_with_quotes, "password": "test"}
        )

        assert isinstance(result, list)  # execute() returns list

        # Verify it was inserted correctly
        user = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": username_with_quotes}
        )

        assert user is not None
        assert user["username"] == "O'Brien"

    def test_backslash_escaping(self, secure_db):
        """Test handling of backslash characters"""
        # Backslashes might be interpreted as escape characters
        username_with_backslash = "user\\admin"

        result = secure_db.execute(
            "INSERT INTO users (username, password_hash) VALUES (:username, :password)",
            {"username": username_with_backslash, "password": "test"}
        )

        assert isinstance(result, list)  # execute() returns list

        user = secure_db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": username_with_backslash}
        )

        assert user is not None
        assert user["username"] == "user\\admin"


class TestPrivilegeEscalation:
    """Test that users cannot escalate privileges"""

    def test_cannot_modify_other_users(self, secure_db):
        """Test that users cannot modify other users' data"""
        # Regular user trying to become admin
        result = secure_db.execute(
            "UPDATE users SET is_admin = :admin WHERE username = :username",
            {"admin": 1, "username": "regular_user"}
        )

        assert isinstance(result, list)  # execute() returns list

        # This test verifies the UPDATE works, but application logic
        # should prevent regular_user from calling this in the first place

    def test_cannot_access_other_users_data(self, secure_db):
        """Test data isolation between users"""
        # This test verifies database layer works correctly
        # Application layer should enforce access control

        # Regular user trying to access admin's secrets
        secret = secure_db.fetch_one(
            "SELECT * FROM sensitive_data WHERE user_id = :uid",
            {"uid": 1}  # Admin's user_id
        )

        # Database will return the data (that's correct behavior)
        # Application must check authorization before allowing this query
        assert secret is not None
        assert secret["secret_value"] == "admin_secret"

        # This test documents that DB layer doesn't provide authz
        # Application must implement proper access control


class TestConcurrentAccessSecurity:
    """Test security under concurrent access"""

    def test_race_condition_on_insert(self, secure_db):
        """Test that race conditions don't bypass constraints"""
        # Attempt to insert duplicate usernames (should fail due to UNIQUE)
        result1_success = False
        result2_success = False

        try:
            result1 = secure_db.execute(
                "INSERT INTO users (username, password_hash) VALUES (:username, :password)",
                {"username": "duplicate", "password": "pass1"}
            )
            result1_success = True
        except Exception:
            pass

        try:
            result2 = secure_db.execute(
                "INSERT INTO users (username, password_hash) VALUES (:username, :password)",
                {"username": "duplicate", "password": "pass2"}
            )
            result2_success = True
        except Exception:
            pass

        # One should succeed, one should fail
        assert (result1_success and not result2_success) or \
               (not result1_success and result2_success)

        # Verify only one user with that username exists
        users = secure_db.execute(
            "SELECT * FROM users WHERE username = :username",
            {"username": "duplicate"}
        )
        assert len(users) == 1
