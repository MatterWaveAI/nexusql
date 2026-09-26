"""
Integration tests for execute_script with SQL statement splitting
Tests across PostgreSQL, MySQL, and MSSQL databases
"""

import os
import pytest
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


@pytest.fixture(params=[
    pytest.param('postgresql', marks=pytest.mark.postgresql),
    pytest.param('mysql', marks=pytest.mark.mysql),
    pytest.param('mssql', marks=pytest.mark.mssql),
])
def db_config(request):
    """Provide database configurations for all supported databases"""
    db_type = request.param

    env_vars = {
        'postgresql': ('TEST_POSTGRESQL_URL', DatabaseType.POSTGRESQL),
        'mysql': ('TEST_MYSQL_URL', DatabaseType.MYSQL),
        'mssql': ('TEST_MSSQL_URL', DatabaseType.MSSQL),
    }

    env_var, db_type_enum = env_vars[db_type]
    db_url = os.getenv(env_var)

    if not db_url:
        pytest.skip(f"{env_var} not set")

    return ConnectionConfig(
        database_type=db_type_enum,
        database_url=db_url
    )


@pytest.fixture
def db_manager(db_config):
    """Create and connect database manager"""
    db = DatabaseManager(db_config)
    db.connect()

    # Clean up any existing test tables (MSSQL doesn't support CASCADE)
    try:
        db.execute("DROP TABLE IF EXISTS test_script_comments")
    except:
        pass
    try:
        db.execute("DROP TABLE IF EXISTS test_script_posts")
    except:
        pass
    try:
        db.execute("DROP TABLE IF EXISTS test_script_users")
    except:
        pass

    yield db

    # Clean up after tests
    try:
        db.execute("DROP TABLE IF EXISTS test_script_comments")
    except:
        pass
    try:
        db.execute("DROP TABLE IF EXISTS test_script_posts")
    except:
        pass
    try:
        db.execute("DROP TABLE IF EXISTS test_script_users")
    except:
        pass

    db.disconnect()


class TestExecuteScriptBasic:
    """Basic execute_script tests"""

    @pytest.mark.asyncio
    async def test_simple_multi_statement_script(self, db_manager):
        """Test executing a simple multi-statement script"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(255)
            );

            INSERT INTO test_script_users (name, email) VALUES ('Alice', 'alice@example.com');
            INSERT INTO test_script_users (name, email) VALUES ('Bob', 'bob@example.com');
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        # Verify data was inserted
        rows = db_manager.execute("SELECT * FROM test_script_users ORDER BY name")
        assert len(rows) == 2
        assert rows[0]['name'] == 'Alice'
        assert rows[1]['name'] == 'Bob'

    @pytest.mark.asyncio
    async def test_semicolon_in_string_literal(self, db_manager):
        """Test that semicolons in string literals are preserved (Issue #1)"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                bio TEXT
            );

            INSERT INTO test_script_users (name, bio)
            VALUES ('John', 'Loves SQL; enjoys programming');

            INSERT INTO test_script_users (name, bio)
            VALUES ('Jane', 'Expert at databases; writes complex queries');
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        # Verify semicolons in bio field are preserved
        rows = db_manager.execute("SELECT * FROM test_script_users ORDER BY name")
        assert len(rows) == 2
        assert rows[1]['name'] == 'John'
        assert rows[1]['bio'] == 'Loves SQL; enjoys programming'
        assert rows[0]['name'] == 'Jane'
        assert rows[0]['bio'] == 'Expert at databases; writes complex queries'

    @pytest.mark.asyncio
    async def test_escaped_quotes_in_strings(self, db_manager):
        """Test handling of escaped quotes in string literals"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                bio TEXT
            );

            INSERT INTO test_script_users (name, bio)
            VALUES ('O''Brien', 'It''s a test; he''s an expert');
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        # Verify escaped quotes are handled correctly
        rows = db_manager.execute("SELECT * FROM test_script_users")
        assert len(rows) == 1
        assert rows[0]['name'] == "O'Brien"
        assert rows[0]['bio'] == "It's a test; he's an expert"


class TestExecuteScriptComments:
    """Test execute_script with SQL comments"""

    @pytest.mark.asyncio
    async def test_single_line_comments(self, db_manager):
        """Test handling of single-line comments"""
        script = """
            -- Create users table
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) -- User's full name
            );

            -- Insert test data; this semicolon should be ignored
            INSERT INTO test_script_users (name) VALUES ('Alice');
            INSERT INTO test_script_users (name) VALUES ('Bob'); -- End of script
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        rows = db_manager.execute("SELECT * FROM test_script_users ORDER BY name")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_multi_line_comments(self, db_manager):
        """Test handling of multi-line comments"""
        script = """
            /*
             * Create users table
             * This comment has multiple lines; with semicolons
             */
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100)
            );

            /* Insert test data */ INSERT INTO test_script_users (name) VALUES ('Alice');
            INSERT INTO test_script_users (name) VALUES ('Bob'); /* Comment at end; with semicolon */
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        rows = db_manager.execute("SELECT * FROM test_script_users ORDER BY name")
        assert len(rows) == 2


class TestExecuteScriptComplex:
    """Test execute_script with complex scenarios"""

    @pytest.mark.asyncio
    async def test_create_and_populate_multiple_tables(self, db_manager):
        """Test creating and populating multiple related tables"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(255)
            );

            CREATE TABLE test_script_posts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                title VARCHAR(200),
                content TEXT
            );

            INSERT INTO test_script_users (name, email) VALUES ('Alice', 'alice@example.com');
            INSERT INTO test_script_users (name, email) VALUES ('Bob', 'bob@example.com');

            INSERT INTO test_script_posts (user_id, title, content)
            VALUES (1, 'First Post', 'This is Alice''s first post; welcome!');

            INSERT INTO test_script_posts (user_id, title, content)
            VALUES (2, 'Bob''s Introduction', 'Hello; I''m new here!');
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        # Verify users
        users = db_manager.execute("SELECT * FROM test_script_users ORDER BY id")
        assert len(users) == 2

        # Verify posts
        posts = db_manager.execute("SELECT * FROM test_script_posts ORDER BY id")
        assert len(posts) == 2
        assert posts[0]['content'] == "This is Alice's first post; welcome!"
        assert posts[1]['content'] == "Hello; I'm new here!"

    @pytest.mark.asyncio
    async def test_mixed_special_characters(self, db_manager):
        """Test script with various special characters in strings"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100),
                bio TEXT,
                notes TEXT
            );

            INSERT INTO test_script_users (name, bio, notes) VALUES
            (
                'John "The Expert" Doe',
                'Loves SQL; enjoys: INSERT, UPDATE, DELETE',
                'Contact at: john@example.com; Available: Mon-Fri'
            );

            INSERT INTO test_script_users (name, bio, notes) VALUES
            (
                'Jane O''Connor',
                'Database expert; specializes in: PostgreSQL, MySQL, MSSQL',
                'Fun fact: She''s written 100+ queries; Still learning!'
            );
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        rows = db_manager.execute("SELECT * FROM test_script_users ORDER BY id")
        assert len(rows) == 2
        assert 'Loves SQL; enjoys: INSERT, UPDATE, DELETE' in rows[0]['bio']
        assert "She's written 100+ queries; Still learning!" in rows[1]['notes']

    @pytest.mark.asyncio
    async def test_script_with_no_trailing_semicolon(self, db_manager):
        """Test that scripts work even without trailing semicolon"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100)
            );

            INSERT INTO test_script_users (name) VALUES ('Alice')
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        rows = db_manager.execute("SELECT * FROM test_script_users")
        assert len(rows) == 1
        assert rows[0]['name'] == 'Alice'

    @pytest.mark.asyncio
    async def test_empty_statements_ignored(self, db_manager):
        """Test that empty statements (multiple semicolons) are ignored"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100)
            );;;

            ;;INSERT INTO test_script_users (name) VALUES ('Alice');;

            ;;;
        """

        result = await db_manager.execute_script(script)
        assert result.success is True

        rows = db_manager.execute("SELECT * FROM test_script_users")
        assert len(rows) == 1


class TestExecuteScriptErrorHandling:
    """Test error handling in execute_script"""

    @pytest.mark.asyncio
    async def test_syntax_error_in_script(self, db_manager):
        """Test that syntax errors are properly reported"""
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100)
            );

            INVALID SQL STATEMENT HERE;

            INSERT INTO test_script_users (name) VALUES ('Alice');
        """

        result = await db_manager.execute_script(script)
        assert result.success is False
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_table_already_exists_error(self, db_manager):
        """Test error when trying to create existing table"""
        # Create table first
        db_manager.execute("""
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100)
            )
        """)

        # Try to create again in script
        script = """
            CREATE TABLE test_script_users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100)
            );
        """

        result = await db_manager.execute_script(script)
        assert result.success is False
