"""Tests for SQL statement splitting functionality"""

import pytest
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


class TestSQLSplitting:
    """Test the _split_sql_statements method"""

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

    def test_simple_statements(self, db_manager):
        """Test splitting simple statements"""
        script = "SELECT 1; SELECT 2; SELECT 3;"
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 3
        assert statements[0] == "SELECT 1"
        assert statements[1] == "SELECT 2"
        assert statements[2] == "SELECT 3"

    def test_semicolon_in_single_quoted_string(self, db_manager):
        """Test that semicolons in single-quoted strings are preserved"""
        script = """
            INSERT INTO users (name, bio) VALUES ('John', 'Loves SQL; enjoys programming');
            CREATE TABLE posts (id INT, content TEXT);
        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert "'Loves SQL; enjoys programming'" in statements[0]
        assert "CREATE TABLE posts" in statements[1]

    def test_escaped_single_quotes(self, db_manager):
        """Test handling of escaped single quotes"""
        script = "INSERT INTO t VALUES ('It''s a test'); SELECT 1;"
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert "'It''s a test'" in statements[0]

    def test_double_quoted_identifiers(self, db_manager):
        """Test handling of double-quoted identifiers with semicolons"""
        script = 'CREATE TABLE "my;table" (id INT); SELECT * FROM "my;table";'
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert '"my;table"' in statements[0]
        assert '"my;table"' in statements[1]

    def test_escaped_double_quotes(self, db_manager):
        """Test handling of escaped double quotes in identifiers"""
        script = 'CREATE TABLE "my""quoted""table" (id INT); SELECT 1;'
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert '"my""quoted""table"' in statements[0]

    def test_single_line_comments(self, db_manager):
        """Test handling of single-line comments"""
        script = """
            SELECT 1; -- This is a comment; with semicolon
            SELECT 2;
        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert "SELECT 1" in statements[0]
        # Comment after semicolon belongs to next statement
        assert "-- This is a comment; with semicolon" in statements[1]
        assert "SELECT 2" in statements[1]

    def test_multi_line_comments(self, db_manager):
        """Test handling of multi-line comments"""
        script = """
            SELECT 1; /* This is a
            multi-line comment; with semicolon */
            SELECT 2;
        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert "SELECT 1" in statements[0]
        # Comment after semicolon belongs to next statement
        assert "/* This is a" in statements[1]
        assert "SELECT 2" in statements[1]

    def test_dollar_quoted_strings_postgresql(self, db_manager):
        """Test handling of PostgreSQL dollar-quoted strings"""
        script = """
            CREATE FUNCTION test() RETURNS INT AS $$
            BEGIN
                -- Function body with semicolons;
                RETURN 1;
            END;
            $$ LANGUAGE plpgsql;
            SELECT 1;
        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        # First statement should contain the entire function
        assert "CREATE FUNCTION" in statements[0]
        assert "RETURN 1;" in statements[0]
        assert "END;" in statements[0]
        assert "$$ LANGUAGE plpgsql" in statements[0]
        assert statements[1].strip() == "SELECT 1"

    def test_dollar_quoted_with_tag(self, db_manager):
        """Test handling of PostgreSQL dollar-quoted strings with tags"""
        script = """
            CREATE FUNCTION test() RETURNS TEXT AS $body$
            SELECT 'text; with semicolon';
            $body$ LANGUAGE sql;
            SELECT 2;
        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert "$body$" in statements[0]
        assert "text; with semicolon" in statements[0]
        assert "SELECT 2" in statements[1]

    def test_no_trailing_semicolon(self, db_manager):
        """Test that last statement without semicolon is included"""
        script = "SELECT 1; SELECT 2"
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert statements[0] == "SELECT 1"
        assert statements[1] == "SELECT 2"

    def test_empty_statements_filtered(self, db_manager):
        """Test that empty statements are filtered out"""
        script = "SELECT 1;;; SELECT 2;"
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert statements[0] == "SELECT 1"
        assert statements[1] == "SELECT 2"

    def test_whitespace_handling(self, db_manager):
        """Test that whitespace is properly trimmed"""
        script = """

            SELECT 1;


            SELECT 2;

        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 2
        assert "SELECT 1" in statements[0]
        assert "SELECT 2" in statements[1]

    def test_complex_mixed_case(self, db_manager):
        """Test complex case with multiple features"""
        script = """
            -- Create users table
            CREATE TABLE users (
                id INT PRIMARY KEY,
                name VARCHAR(100),
                bio TEXT
            );

            /* Insert test data with special characters */
            INSERT INTO users (id, name, bio) VALUES
                (1, 'John', 'Loves SQL; enjoys programming'),
                (2, 'Jane', 'Expert in DB''s');

            -- Query with comment
            SELECT * FROM users WHERE bio LIKE '%SQL;%';
        """
        statements = db_manager._split_sql_statements(script)
        assert len(statements) == 3
        assert "CREATE TABLE users" in statements[0]
        assert "INSERT INTO users" in statements[1]
        assert "'Loves SQL; enjoys programming'" in statements[1]
        assert "SELECT * FROM users" in statements[2]


class TestExecuteScriptIntegration:
    """Integration tests for execute_script with different databases"""

    @pytest.mark.asyncio
    async def test_execute_script_sqlite(self):
        """Test execute_script with SQLite"""
        config = ConnectionConfig(
            database_type=DatabaseType.SQLITE,
            database_url="sqlite://:memory:"
        )
        db = DatabaseManager(config)
        db.connect()

        script = """
            CREATE TABLE test_users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                bio TEXT
            );

            INSERT INTO test_users (name, bio) VALUES ('John', 'Loves SQL; programming');
            INSERT INTO test_users (name, bio) VALUES ('Jane', 'Expert''s choice');
        """

        result = await db.execute_script(script)
        assert result.success is True

        # Verify data was inserted correctly
        rows = db.execute("SELECT * FROM test_users ORDER BY id")
        assert len(rows) == 2
        assert rows[0]['name'] == 'John'
        assert rows[0]['bio'] == 'Loves SQL; programming'
        assert rows[1]['name'] == 'Jane'
        assert rows[1]['bio'] == "Expert's choice"

        db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_script_postgresql(self):
        """Test execute_script with PostgreSQL"""
        import os
        db_url = os.getenv('TEST_POSTGRESQL_URL')
        if not db_url:
            pytest.skip("TEST_POSTGRESQL_URL not set")

        config = ConnectionConfig(
            database_type=DatabaseType.POSTGRESQL,
            database_url=db_url
        )
        db = DatabaseManager(config)
        db.connect()

        # Clean up from previous tests
        db.execute("DROP TABLE IF EXISTS test_users_script CASCADE")

        script = """
            CREATE TABLE test_users_script (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                bio TEXT
            );

            INSERT INTO test_users_script (name, bio) VALUES ('John', 'Loves SQL; programming');
            INSERT INTO test_users_script (name, bio) VALUES ('Jane', 'Expert''s choice');
        """

        result = await db.execute_script(script)
        assert result.success is True

        # Verify data was inserted correctly
        rows = db.execute("SELECT * FROM test_users_script ORDER BY id")
        assert len(rows) == 2
        assert rows[0]['name'] == 'John'
        assert rows[0]['bio'] == 'Loves SQL; programming'
        assert rows[1]['name'] == 'Jane'
        assert rows[1]['bio'] == "Expert's choice"

        # Clean up
        db.execute("DROP TABLE test_users_script CASCADE")
        db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_script_mysql(self):
        """Test execute_script with MySQL"""
        import os
        db_url = os.getenv('TEST_MYSQL_URL')
        if not db_url:
            pytest.skip("TEST_MYSQL_URL not set")

        config = ConnectionConfig(
            database_type=DatabaseType.MYSQL,
            database_url=db_url
        )
        db = DatabaseManager(config)
        db.connect()

        # Clean up from previous tests
        try:
            db.execute("DROP TABLE IF EXISTS test_users_script")
        except:
            pass

        script = """
            CREATE TABLE test_users_script (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100),
                bio TEXT
            );

            INSERT INTO test_users_script (name, bio) VALUES ('John', 'Loves SQL; programming');
            INSERT INTO test_users_script (name, bio) VALUES ('Jane', 'Expert''s choice');
        """

        result = await db.execute_script(script)
        assert result.success is True

        # Verify data was inserted correctly
        rows = db.execute("SELECT * FROM test_users_script ORDER BY id")
        assert len(rows) == 2
        assert rows[0]['name'] == 'John'
        assert rows[0]['bio'] == 'Loves SQL; programming'
        assert rows[1]['name'] == 'Jane'
        assert rows[1]['bio'] == "Expert's choice"

        # Clean up
        db.execute("DROP TABLE test_users_script")
        db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_script_mssql(self):
        """Test execute_script with MSSQL"""
        import os
        db_url = os.getenv('TEST_MSSQL_URL')
        if not db_url:
            pytest.skip("TEST_MSSQL_URL not set")

        config = ConnectionConfig(
            database_type=DatabaseType.MSSQL,
            database_url=db_url
        )
        db = DatabaseManager(config)
        db.connect()

        # Clean up from previous tests
        try:
            db.execute("DROP TABLE IF EXISTS test_users_script")
        except:
            pass

        script = """
            CREATE TABLE test_users_script (
                id INT IDENTITY(1,1) PRIMARY KEY,
                name NVARCHAR(100),
                bio NVARCHAR(MAX)
            );

            INSERT INTO test_users_script (name, bio) VALUES ('John', 'Loves SQL; programming');
            INSERT INTO test_users_script (name, bio) VALUES ('Jane', 'Expert''s choice');
        """

        result = await db.execute_script(script)
        assert result.success is True

        # Verify data was inserted correctly
        rows = db.execute("SELECT * FROM test_users_script ORDER BY id")
        assert len(rows) == 2
        assert rows[0]['name'] == 'John'
        assert rows[0]['bio'] == 'Loves SQL; programming'
        assert rows[1]['name'] == 'Jane'
        assert rows[1]['bio'] == "Expert's choice"

        # Clean up
        db.execute("DROP TABLE test_users_script")
        db.disconnect()
