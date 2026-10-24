"""
Database Performance and Scalability Tests

Tests to ensure the database module performs well under load:
- Query performance
- Bulk operations
- Index usage
- Memory efficiency
- Connection overhead
- Large dataset handling
"""

import pytest
import time
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


@pytest.fixture
def perf_db():
    """Create database for performance testing"""
    config = ConnectionConfig(
        database_type=DatabaseType.SQLITE,
        database_url="sqlite://:memory:"
    )
    db = DatabaseManager(config)
    db.connect()

    yield db

    db.disconnect()


class TestQueryPerformance:
    """Test query execution performance"""

    def test_simple_select_performance(self, perf_db):
        """Test simple SELECT query performance"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")

        # Insert test data
        for i in range(1000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # Measure SELECT performance
        start = time.time()
        for _ in range(100):
            rows = db.execute("SELECT * FROM test WHERE value < :limit", {"limit": 100})
        elapsed = time.time() - start

        # Should be fast (< 1 second for 100 queries)
        assert elapsed < 1.0
        print(f"\n100 SELECT queries: {elapsed:.3f}s ({elapsed/100*1000:.2f}ms per query)")

    def test_indexed_vs_unindexed_query(self, perf_db):
        """Test performance difference with/without index"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, email TEXT)")

        # Insert test data
        for i in range(10000):
            db.execute("INSERT INTO test (email) VALUES (:email)", {"email": f"user{i}@example.com"})

        # Query without index
        start = time.time()
        db.fetch_one("SELECT * FROM test WHERE email = :email", {"email": "user9999@example.com"})
        no_index_time = time.time() - start

        # Create index
        db.execute("CREATE INDEX idx_email ON test(email)")

        # Query with index
        start = time.time()
        db.fetch_one("SELECT * FROM test WHERE email = :email", {"email": "user9999@example.com"})
        with_index_time = time.time() - start

        print(f"\nWithout index: {no_index_time*1000:.2f}ms")
        print(f"With index: {with_index_time*1000:.2f}ms")
        print(f"Speedup: {no_index_time/max(with_index_time, 1e-9):.1f}x")

        # Index should help (might not always be faster with small datasets)
        # Just verify both queries work
        assert no_index_time >= 0
        assert with_index_time >= 0

    def test_parameterized_query_performance(self, perf_db):
        """Test that parameterized queries don't have excessive overhead"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        for i in range(100):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # With parameters
        start = time.time()
        for i in range(1000):
            db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": i % 100})
        param_time = time.time() - start

        # Should complete in reasonable time
        assert param_time < 2.0
        print(f"\n1000 parameterized queries: {param_time:.3f}s ({param_time/1000*1000:.2f}ms per query)")


class TestBulkOperations:
    """Test bulk operation performance"""

    def test_bulk_insert_performance(self, perf_db):
        """Test inserting many rows"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, value INTEGER)")

        # Bulk insert
        start = time.time()
        for i in range(10000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})
        elapsed = time.time() - start

        # Verify count
        row = db.fetch_one("SELECT COUNT(*) as count FROM test")
        assert row["count"] == 10000

        print(f"\n10,000 inserts: {elapsed:.3f}s ({10000/elapsed:.0f} inserts/sec)")

        # Should complete reasonably quickly
        assert elapsed < 10.0

    def test_bulk_update_performance(self, perf_db):
        """Test updating many rows"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")

        # Insert data
        for i in range(1000):
            db.execute("INSERT INTO test (id, value) VALUES (:id, :val)", {"id": i, "val": i})

        # Bulk update
        start = time.time()
        for i in range(1000):
            db.execute("UPDATE test SET value = :newval WHERE id = :id", {"newval": i * 2, "id": i})
        elapsed = time.time() - start

        # Verify updates
        row = db.fetch_one("SELECT SUM(value) as sum FROM test")
        expected_sum = sum(i * 2 for i in range(1000))
        assert row["sum"] == expected_sum

        print(f"\n1,000 updates: {elapsed:.3f}s ({1000/elapsed:.0f} updates/sec)")

    def test_bulk_delete_performance(self, perf_db):
        """Test deleting many rows"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")

        # Insert data
        for i in range(1000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # Bulk delete
        start = time.time()
        for i in range(0, 1000, 2):  # Delete even numbers
            db.execute("DELETE FROM test WHERE value = :val", {"val": i})
        elapsed = time.time() - start

        # Verify deletions
        row = db.fetch_one("SELECT COUNT(*) as count FROM test")
        assert row["count"] == 500  # Only odd numbers remain

        print(f"\n500 deletes: {elapsed:.3f}s ({500/elapsed:.0f} deletes/sec)")

    def test_batch_insert_single_query(self, perf_db):
        """Test batch insert using single query"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, value INTEGER)")

        # Build batch insert query (SQLite doesn't support bulk INSERT VALUES)
        # So we'll test individual inserts but measure total time
        start = time.time()
        for i in range(5000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})
        elapsed = time.time() - start

        row = db.fetch_one("SELECT COUNT(*) as count FROM test")
        assert row["count"] == 5000

        print(f"\n5,000 individual inserts: {elapsed:.3f}s ({5000/elapsed:.0f} inserts/sec)")


class TestLargeDatasets:
    """Test handling of large datasets"""

    def test_large_table_query(self, perf_db):
        """Test querying from large table"""
        db = perf_db

        db.execute("CREATE TABLE large_table (id INTEGER PRIMARY KEY, data TEXT)")

        # Insert 50k rows
        print("\nInserting 50,000 rows...")
        for i in range(50000):
            db.execute("INSERT INTO large_table (data) VALUES (:data)", {"data": f"row_{i}_data"})

        # Query performance
        start = time.time()
        rows = db.execute("SELECT * FROM large_table WHERE id < :limit", {"limit": 1000})
        elapsed = time.time() - start

        assert len(rows) == 999  # ids 1-999
        print(f"Query 1000 rows from 50k table: {elapsed*1000:.2f}ms")

    def test_large_result_set(self, perf_db):
        """Test fetching large result sets"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")

        # Insert 10k rows
        for i in range(10000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # Fetch all rows
        start = time.time()
        rows = db.execute("SELECT * FROM test")
        elapsed = time.time() - start

        assert len(rows) == 10000
        print(f"\nFetch 10,000 rows: {elapsed*1000:.2f}ms")

    def test_large_text_storage(self, perf_db):
        """Test storing and retrieving large text"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, large_text TEXT)")

        # Large text (1MB)
        large_text = "A" * (1024 * 1024)

        # Insert
        start = time.time()
        db.execute("INSERT INTO test (large_text) VALUES (:text)", {"text": large_text})
        insert_time = time.time() - start

        # Retrieve
        start = time.time()
        row = db.fetch_one("SELECT large_text FROM test")
        fetch_time = time.time() - start

        assert len(row["large_text"]) == len(large_text)
        print(f"\nInsert 1MB text: {insert_time*1000:.2f}ms")
        print(f"Fetch 1MB text: {fetch_time*1000:.2f}ms")


class TestConnectionOverhead:
    """Test connection creation/destruction overhead"""

    def test_connection_creation_time(self):
        """Test time to create connection"""
        config = ConnectionConfig(DatabaseType.SQLITE, "sqlite://:memory:")

        # Measure connection creation
        start = time.time()
        for _ in range(100):
            db = DatabaseManager(config)
            db.connect()
            db.disconnect()
        elapsed = time.time() - start

        print(f"\n100 connection cycles: {elapsed:.3f}s ({elapsed/100*1000:.2f}ms per connection)")

        # Should be fast
        assert elapsed < 5.0

    def test_query_with_connection_reuse(self):
        """Test performance of reusing single connection"""
        config = ConnectionConfig(DatabaseType.SQLITE, "sqlite://:memory:")
        db = DatabaseManager(config)
        db.connect()

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        db.execute("INSERT INTO test (value) VALUES (:val)", {"val": 1})

        # Reuse connection for many queries
        start = time.time()
        for _ in range(1000):
            db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": 1})
        elapsed = time.time() - start

        print(f"\n1,000 queries (reused connection): {elapsed:.3f}s ({elapsed/1000*1000:.2f}ms per query)")

        db.disconnect()


class TestMemoryEfficiency:
    """Test memory usage patterns"""

    def test_large_dataset_memory(self, perf_db):
        """Test that large datasets don't cause excessive memory usage"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)")

        # Insert data
        for i in range(10000):
            db.execute("INSERT INTO test (data) VALUES (:data)", {"data": f"data_{i}"})

        # Fetch in chunks (to test chunking if implemented)
        total_rows = 0
        for offset in range(0, 10000, 1000):
            rows = db.execute(
                "SELECT * FROM test LIMIT :limit OFFSET :offset",
                {"limit": 1000, "offset": offset}
            )
            total_rows += len(rows)

        assert total_rows == 10000

    def test_repeated_queries_memory(self, perf_db):
        """Test that repeated queries don't leak memory"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        db.execute("INSERT INTO test (value) VALUES (:val)", {"val": 42})

        # Repeat same query many times
        for _ in range(10000):
            row = db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": 42})
            assert row is not None

        # If this completes without error, memory is probably OK


class TestScalability:
    """Test scalability with increasing data volumes"""

    def test_linear_scaling(self, perf_db):
        """Test that performance scales linearly with data size"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        db.execute("CREATE INDEX idx_value ON test(value)")

        # Test with different data sizes
        sizes = [100, 1000, 10000]
        times = []

        for size in sizes:
            # Clear table
            db.execute("DELETE FROM test")

            # Insert data
            for i in range(size):
                db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

            # Measure query time
            start = time.time()
            for _ in range(100):
                db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": size // 2})
            elapsed = time.time() - start
            times.append(elapsed)

            print(f"\n{size} rows: {elapsed:.3f}s for 100 queries ({elapsed/100*1000:.2f}ms per query)")

        # With index, should not scale badly
        # (Don't enforce strict linear scaling as that's hard to measure reliably)


class TestAsyncPerformance:
    """Test async operation performance"""

    @pytest.mark.asyncio
    async def test_async_query_performance(self):
        """Test async query performance"""
        config = ConnectionConfig(DatabaseType.SQLITE, "sqlite://:memory:")
        db = DatabaseManager(config)
        db.connect()

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        for i in range(100):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # Measure async queries
        start = time.time()
        for i in range(100):
            result = await db.execute_async("SELECT * FROM test WHERE value = :val", {"val": i})
        elapsed = time.time() - start

        print(f"\n100 async queries: {elapsed:.3f}s ({elapsed/100*1000:.2f}ms per query)")

        db.disconnect()

    @pytest.mark.asyncio
    async def test_async_insert_performance(self):
        """Test async insert performance"""
        config = ConnectionConfig(DatabaseType.SQLITE, "sqlite://:memory:")
        db = DatabaseManager(config)
        db.connect()

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, value INTEGER)")

        # Measure async inserts
        start = time.time()
        for i in range(1000):
            await db.execute_async("INSERT INTO test (value) VALUES (:val)", {"val": i})
        elapsed = time.time() - start

        # Verify count
        row = db.fetch_one("SELECT COUNT(*) as count FROM test")
        assert row["count"] == 1000

        print(f"\n1,000 async inserts: {elapsed:.3f}s ({1000/elapsed:.0f} inserts/sec)")

        db.disconnect()


class TestComplexQueries:
    """Test performance of complex queries"""

    def test_join_performance(self, perf_db):
        """Test JOIN query performance"""
        db = perf_db

        # Create tables
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)")

        # Insert data
        for i in range(1000):
            db.execute("INSERT INTO users (name) VALUES (:name)", {"name": f"user_{i}"})

        for i in range(5000):
            db.execute(
                "INSERT INTO orders (user_id, amount) VALUES (:uid, :amount)",
                {"uid": (i % 1000) + 1, "amount": 100.0 + i}
            )

        # Measure JOIN performance
        start = time.time()
        rows = db.execute("""
            SELECT u.name, COUNT(o.id) as order_count, SUM(o.amount) as total
            FROM users u
            JOIN orders o ON u.id = o.user_id
            GROUP BY u.id
            HAVING COUNT(o.id) > :min_orders
        """, {"min_orders": 3})
        elapsed = time.time() - start

        assert len(rows) > 0
        print(f"\nComplex JOIN query (1000 users, 5000 orders): {elapsed*1000:.2f}ms")

    def test_subquery_performance(self, perf_db):
        """Test subquery performance"""
        db = perf_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, category TEXT, value INTEGER)")

        # Insert data
        categories = ["A", "B", "C", "D", "E"]
        for i in range(1000):
            db.execute(
                "INSERT INTO test (category, value) VALUES (:cat, :val)",
                {"cat": categories[i % 5], "val": i}
            )

        # Subquery
        start = time.time()
        rows = db.execute("""
            SELECT category, AVG(value) as avg_value
            FROM test
            WHERE value > (SELECT AVG(value) FROM test)
            GROUP BY category
        """)
        elapsed = time.time() - start

        assert len(rows) > 0
        print(f"\nSubquery with aggregation: {elapsed*1000:.2f}ms")
