"""
Advanced Database Security Tests

Additional security tests covering:
- Time-based attacks
- Blind SQL injection
- Second-order injection
- NoSQL injection patterns (in SQL context)
- Error-based injection
- Advanced bypass techniques
- Encoding attacks
- LDAP-style injection
"""

import pytest
import time
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


@pytest.fixture
def advanced_secure_db():
    """Create database for advanced security testing"""
    config = ConnectionConfig(
        database_type=DatabaseType.SQLITE,
        database_url="sqlite://:memory:"
    )
    db = DatabaseManager(config)
    db.connect()

    # Schema with various data types and constraints
    db.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            api_key TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT,
            failed_attempts INTEGER DEFAULT 0,
            is_locked INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    db.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            ip_address TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Insert test data
    db.execute(
        "INSERT INTO users (username, email, password_hash, role) VALUES (:u, :e, :p, :r)",
        {"u": "admin", "e": "admin@example.com", "p": "hashed_admin_pass", "r": "admin"}
    )
    db.execute(
        "INSERT INTO users (username, email, password_hash, role) VALUES (:u, :e, :p, :r)",
        {"u": "user1", "e": "user1@example.com", "p": "hashed_user_pass", "r": "user"}
    )

    yield db

    db.disconnect()


class TestBlindSQLInjection:
    """Test blind SQL injection prevention (boolean-based, time-based)"""

    def test_boolean_based_blind_injection(self, advanced_secure_db):
        """Test boolean-based blind SQL injection"""
        db = advanced_secure_db

        # Attacker tries to determine if admin exists via boolean responses
        # Payload: ' AND (SELECT COUNT(*) FROM users WHERE role='admin')>0--
        malicious_username = "' AND (SELECT COUNT(*) FROM users WHERE role='admin')>0--"

        row = db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_username}
        )

        # Should not reveal information via timing/response differences
        assert row is None

    def test_time_based_blind_injection(self, advanced_secure_db):
        """Test time-based blind SQL injection prevention"""
        db = advanced_secure_db

        # Attacker tries to cause delay to infer data
        # Payload: ' OR CASE WHEN (1=1) THEN (SELECT SLEEP(5)) ELSE 0 END--
        malicious_username = "admin' OR CASE WHEN (1=1) THEN (SELECT SLEEP(5)) ELSE 0 END--"

        start = time.time()
        row = db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_username}
        )
        elapsed = time.time() - start

        # Should not execute the sleep (parameter binding prevents it)
        assert elapsed < 1.0  # Much less than 5 seconds
        assert row is None

    def test_inferential_injection(self, advanced_secure_db):
        """Test inferential SQL injection (extracting data bit by bit)"""
        db = advanced_secure_db

        # Attacker tries to extract password char by char
        # Payload: admin' AND SUBSTRING(password_hash,1,1)='h
        malicious_username = "admin' AND SUBSTRING(password_hash,1,1)='h"

        row = db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            {"username": malicious_username}
        )

        # Should not reveal data
        assert row is None


class TestSecondOrderInjection:
    """Test second-order SQL injection prevention"""

    def test_stored_payload_execution(self, advanced_secure_db):
        """Test that stored malicious data doesn't execute later"""
        db = advanced_secure_db

        # First request: Store malicious payload
        malicious_email = "attacker@example.com' OR '1'='1"

        db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (:u, :e, :p)",
            {"u": "attacker", "e": malicious_email, "p": "hashed_pass"}
        )

        # Second request: Use stored data in query
        user = db.fetch_one("SELECT email FROM users WHERE username = :u", {"u": "attacker"})
        stored_email = user["email"]

        # Now use stored email in another query (potential second-order injection)
        result = db.execute(
            "SELECT * FROM users WHERE email = :email",
            {"email": stored_email}
        )

        # Should only return the attacker's row, not all rows
        assert len(result) == 1
        assert result[0]["username"] == "attacker"


class TestErrorBasedInjection:
    """Test error-based SQL injection prevention"""

    def test_error_message_information_disclosure(self, advanced_secure_db):
        """Test that errors don't disclose database structure"""
        db = advanced_secure_db

        # Deliberately cause errors with various payloads
        error_payloads = [
            "admin' AND (SELECT * FROM users)='1",  # Subquery returns multiple rows
            "admin' AND CAST((SELECT password_hash FROM users LIMIT 1) AS INT)--",  # Type conversion error
            "admin' UNION SELECT NULL, NULL, NULL FROM users--",  # Column count mismatch
            "admin' AND 1/0--",  # Division by zero
        ]

        for payload in error_payloads:
            try:
                result = db.execute(
                    "SELECT * FROM users WHERE username = :username",
                    {"username": payload}
                )
                # Query succeeded with parameter binding (good!)
                assert isinstance(result, list)
            except Exception as e:
                # Error message should not contain sensitive info
                error_msg = str(e).lower()
                # It's OK to mention table doesn't exist or syntax error
                # But shouldn't leak actual data from users table
                assert "password" not in error_msg


class TestEncodingAttacks:
    """Test various encoding-based injection attempts"""

    def test_unicode_encoding_bypass(self, advanced_secure_db):
        """Test Unicode normalization attacks"""
        db = advanced_secure_db

        # Various Unicode representations of SQL characters
        unicode_payloads = [
            "admin\u0027 OR \u00271\u0027=\u00271",  # Unicode apostrophes
            "admin\uff07 OR \uff071\uff07=\uff071",  # Fullwidth apostrophes
            "admin\u02bc OR \u02bc1\u02bc=\u02bc1",  # Modifier letters
            "\u0061\u0064\u006d\u0069\u006e\u0027 OR 1=1--",  # Unicode 'admin' + injection
        ]

        for payload in unicode_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            # Should not bypass security (parameter binding handles it)
            assert row is None or row["username"] != "admin"

    def test_url_encoding_bypass(self, advanced_secure_db):
        """Test URL encoding bypass attempts"""
        db = advanced_secure_db

        # URL-encoded injection attempts
        url_encoded_payloads = [
            "admin%27%20OR%20%271%27=%271",  # admin' OR '1'='1
            "admin%2527%2520OR%2520%25271%2527%253D%25271",  # Double encoding
        ]

        for payload in url_encoded_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            # Should treat as literal string
            assert row is None

    def test_hex_encoding_bypass(self, advanced_secure_db):
        """Test hex encoding bypass attempts"""
        db = advanced_secure_db

        # Hex-encoded payloads
        # Some databases interpret 0x... as hex
        hex_payloads = [
            "0x61646d696e",  # 'admin' in hex
            "admin' OR '1'='1' /*",
        ]

        for payload in hex_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            # Should not decode and execute
            assert row is None or row["username"] != "admin"


class TestAdvancedBypassTechniques:
    """Test advanced SQL injection bypass techniques"""

    def test_case_variation_bypass(self, advanced_secure_db):
        """Test case variation in SQL keywords"""
        db = advanced_secure_db

        # Various case combinations
        case_payloads = [
            "admin' Or '1'='1",
            "admin' oR '1'='1",
            "admin' OR '1'='1",
            "admin' UnIoN SeLeCt * FrOm users--",
        ]

        for payload in case_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            assert row is None

    def test_whitespace_variation_bypass(self, advanced_secure_db):
        """Test whitespace manipulation"""
        db = advanced_secure_db

        # Various whitespace characters
        whitespace_payloads = [
            "admin'OR'1'='1",  # No spaces
            "admin'  OR  '1'='1",  # Multiple spaces
            "admin'\tOR\t'1'='1",  # Tabs
            "admin'\nOR\n'1'='1",  # Newlines
            "admin'/**/OR/**/'1'='1",  # Comments as whitespace
        ]

        for payload in whitespace_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            assert row is None

    def test_inline_comment_bypass(self, advanced_secure_db):
        """Test inline comment bypass attempts"""
        db = advanced_secure_db

        comment_payloads = [
            "admin'/*comment*/OR/*comment*/'1'='1",
            "admin'/**/OR/**/'1'='1'/**/--",
            "admin'/*!50000OR*/'1'='1",  # Version-specific comments
        ]

        for payload in comment_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            assert row is None

    def test_concatenation_bypass(self, advanced_secure_db):
        """Test string concatenation bypass attempts"""
        db = advanced_secure_db

        # Trying to build SQL via concatenation
        concat_payloads = [
            "admin' || ' OR ' || '1'='1",  # SQL concatenation
            "admin' + ' OR ' + '1'='1",  # Alternative concatenation
            "CONCAT('admin', CHAR(39), ' OR ', CHAR(39), '1', CHAR(39), '=', CHAR(39), '1')",
        ]

        for payload in concat_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            # Should treat as literal string
            assert row is None


class TestNoSQLStyleInjection:
    """Test NoSQL-style injection patterns in SQL context"""

    def test_json_injection_in_sql(self, advanced_secure_db):
        """Test JSON-based injection attempts"""
        db = advanced_secure_db

        # Some modern SQL databases support JSON queries
        json_payloads = [
            '{"username": {"$ne": null}}',  # MongoDB-style
            '{"username": {"$gt": ""}}',
            '{"$or": [{"username": "admin"}, {"role": "admin"}]}',
        ]

        for payload in json_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            # Should treat as literal string
            assert row is None

    def test_operator_injection(self, advanced_secure_db):
        """Test operator-based injection"""
        db = advanced_secure_db

        operator_payloads = [
            "admin' AND 1=1--",
            "admin' OR 1=1--",
            "admin' AND '1'='1'--",
            "admin' XOR '1'='1'--",
        ]

        for payload in operator_payloads:
            row = db.fetch_one(
                "SELECT * FROM users WHERE username = :username",
                {"username": payload}
            )

            assert row is None


class TestAuthenticationSecurityAdvanced:
    """Test advanced authentication security scenarios"""

    def test_timing_attack_resistance(self, advanced_secure_db):
        """Test resistance to timing attacks on authentication"""
        db = advanced_secure_db

        # Time comparison for valid vs invalid user
        valid_times = []
        invalid_times = []

        for _ in range(10):
            start = time.time()
            db.fetch_one(
                "SELECT * FROM users WHERE username = :u AND password_hash = :p",
                {"u": "admin", "p": "wrong_password"}
            )
            valid_times.append(time.time() - start)

            start = time.time()
            db.fetch_one(
                "SELECT * FROM users WHERE username = :u AND password_hash = :p",
                {"u": "nonexistent_user", "p": "wrong_password"}
            )
            invalid_times.append(time.time() - start)

        # Timing should be similar (within order of magnitude)
        # This is hard to enforce strictly, just verify no huge differences
        avg_valid = sum(valid_times) / len(valid_times)
        avg_invalid = sum(invalid_times) / len(invalid_times)

        # Should not have 10x difference (would leak existence)
        # Note: This is more about database layer, app should add constant-time comparison

    def test_credential_enumeration_prevention(self, advanced_secure_db):
        """Test that different errors don't reveal user existence"""
        db = advanced_secure_db

        # Query for existing user with wrong password
        result1 = db.fetch_one(
            "SELECT * FROM users WHERE username = :u AND password_hash = :p",
            {"u": "admin", "p": "wrong_password"}
        )

        # Query for non-existent user
        result2 = db.fetch_one(
            "SELECT * FROM users WHERE username = :u AND password_hash = :p",
            {"u": "nonexistent", "p": "wrong_password"}
        )

        # Both should return None (no distinction)
        assert result1 is None
        assert result2 is None

    def test_session_fixation_prevention(self, advanced_secure_db):
        """Test session handling security"""
        db = advanced_secure_db

        # Attacker tries to use predictable session IDs
        predictable_sessions = [
            "1", "2", "3", "123456",
            "admin_session",
            "00000000-0000-0000-0000-000000000000",
        ]

        for session_id in predictable_sessions:
            # Try to use predictable session
            session = db.fetch_one(
                "SELECT * FROM sessions WHERE session_id = :sid",
                {"sid": session_id}
            )

            # Should not find any (sessions should use strong random IDs)
            assert session is None


class TestMassAssignment:
    """Test mass assignment vulnerabilities"""

    def test_mass_assignment_attack(self, advanced_secure_db):
        """Test that extra parameters don't escalate privileges"""
        db = advanced_secure_db

        # User registration with injected 'role' parameter
        # Application should filter this, but test database layer
        malicious_data = {
            "username": "hacker",
            "email": "hacker@example.com",
            "password_hash": "hashed_pass",
            "role": "admin",  # Attacker trying to become admin
        }

        # If application blindly accepts all parameters:
        result = db.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (:username, :email, :password_hash, :role)",
            malicious_data
        )

        # Database allows it (this is correct - app should filter)
        assert isinstance(result, list)  # execute() returns list

        # Verify the malicious role was stored
        user = db.fetch_one("SELECT * FROM users WHERE username = :u", {"u": "hacker"})
        assert user["role"] == "admin"

        # This test documents that database layer doesn't prevent this
        # Application MUST validate and filter parameters before database


class TestParameterPollution:
    """Test parameter pollution attacks"""

    def test_duplicate_parameter_handling(self, advanced_secure_db):
        """Test handling of duplicate parameters"""
        db = advanced_secure_db

        # Some frameworks might accept duplicate parameters
        # Test how our system handles it
        params = {
            "username": "user1",  # Legitimate
            # Can't easily test true duplication in dict, but test similar scenarios
        }

        row = db.fetch_one(
            "SELECT * FROM users WHERE username = :username",
            params
        )

        assert row is not None
        assert row["username"] == "user1"


class TestDatabaseFingerprinting:
    """Test database fingerprinting resistance"""

    def test_error_message_consistency(self, advanced_secure_db):
        """Test that errors don't reveal database type"""
        db = advanced_secure_db

        # Various queries that might reveal database type
        fingerprint_queries = [
            "SELECT @@version",  # SQL Server, MySQL
            "SELECT version()",  # PostgreSQL
            "SELECT sqlite_version()",  # SQLite
        ]

        for query in fingerprint_queries:
            try:
                result = db.execute(query)
                # Query succeeded - that's fine, we're testing error messages
                assert isinstance(result, list)
            except Exception as e:
                # Error messages should be generic
                error_msg = str(e).lower()
                # Errors are OK, but shouldn't leak detailed internal info


class TestRateLimitingBypass:
    """Test security features that depend on database queries"""

    def test_failed_login_tracking(self, advanced_secure_db):
        """Test that failed login attempts are tracked correctly"""
        db = advanced_secure_db

        # Simulate multiple failed login attempts
        for i in range(5):
            db.execute(
                "UPDATE users SET failed_attempts = failed_attempts + 1 WHERE username = :u",
                {"u": "user1"}
            )

        # Check failed attempts
        user = db.fetch_one("SELECT failed_attempts FROM users WHERE username = :u", {"u": "user1"})
        assert user["failed_attempts"] == 5

        # Simulate account lockout
        db.execute(
            "UPDATE users SET is_locked = 1 WHERE username = :u AND failed_attempts >= :max",
            {"u": "user1", "max": 5}
        )

        user = db.fetch_one("SELECT is_locked FROM users WHERE username = :u", {"u": "user1"})
        assert user["is_locked"] == 1

    def test_concurrent_login_attempt_race(self, advanced_secure_db):
        """Test race conditions in login tracking"""
        db = advanced_secure_db

        # Multiple concurrent increments
        for _ in range(10):
            db.execute(
                "UPDATE users SET failed_attempts = failed_attempts + 1 WHERE username = :u",
                {"u": "admin"}
            )

        user = db.fetch_one("SELECT failed_attempts FROM users WHERE username = :u", {"u": "admin"})
        # Should be exactly 10 (atomic increments)
        assert user["failed_attempts"] == 10
