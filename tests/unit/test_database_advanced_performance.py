"""
Advanced Database Performance Tests

Additional performance tests covering:
- Connection pooling
- Query plan optimization
- Cache effectiveness
- Batch operations optimization
- Database-specific optimizations
- Resource cleanup
- Load testing
- Stress testing
"""

import pytest
import time
import gc
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from nexusql import DatabaseManager, ConnectionConfig, DatabaseType


@pytest.fixture
def perf_test_db():
    """Create database for advanced performance testing"""
    config = ConnectionConfig(
        database_type=DatabaseType.SQLITE,
        database_url="sqlite://:memory:"
    )
    db = DatabaseManager(config)
    db.connect()

    yield db

    db.disconnect()


class TestConnectionPooling:
    """Test connection pool behavior and performance"""

    def test_connection_reuse_performance(self):
        """Test performance benefit of connection reuse"""
        config = ConnectionConfig(DatabaseType.SQLITE, "sqlite://:memory:")

        # Scenario 1: Create new connection for each query
        start = time.time()
        for _ in range(100):
            db = DatabaseManager(config)
            db.connect()
            db.execute("SELECT 1")
            db.disconnect()
        new_connection_time = time.time() - start

        # Scenario 2: Reuse single connection
        db = DatabaseManager(config)
        db.connect()
        start = time.time()
        for _ in range(100):
            db.execute("SELECT 1")
        reuse_time = time.time() - start
        db.disconnect()

        print(f"\nNew connection each time: {new_connection_time:.3f}s")
        print(f"Reused connection: {reuse_time:.3f}s")
        print(f"Speedup: {new_connection_time/reuse_time:.1f}x")

        # Reuse should be faster
        assert reuse_time < new_connection_time

    def test_concurrent_connection_limit(self):
        """Test behavior under many concurrent connections"""
        import tempfile
        import os

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            config = ConnectionConfig(DatabaseType.SQLITE, f"sqlite:///{db_path}")

            # Setup
            setup_db = DatabaseManager(config)
            setup_db.connect()
            setup_db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
            setup_db.disconnect()

            # Create many concurrent connections
            def worker(worker_id):
                db = DatabaseManager(config)
                db.connect()
                try:
                    result = db.execute("INSERT INTO test (value) VALUES (:val)", {"val": worker_id})
                    db.disconnect()
                    return True
                except Exception:
                    db.disconnect()
                    return False

            start = time.time()
            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(worker, i) for i in range(100)]
                results = [f.result() for f in as_completed(futures)]
            elapsed = time.time() - start

            success_count = sum(1 for r in results if r)
            print(f"\n100 concurrent connections: {elapsed:.3f}s")
            print(f"Successful: {success_count}/100")

            # Should handle reasonably (SQLite might have contention)
            assert success_count > 0

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestQueryOptimization:
    """Test query optimization and plan usage"""

    def test_prepare_statement_reuse(self, perf_test_db):
        """Test prepared statement performance"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")

        # Insert test data
        for i in range(1000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # Same query with different parameters (should reuse plan)
        start = time.time()
        for i in range(1000):
            db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": i})
        elapsed = time.time() - start

        print(f"\n1000 parameterized queries: {elapsed:.3f}s ({elapsed/1000*1000:.2f}ms each)")

        # Should be reasonably fast
        assert elapsed < 5.0

    def test_index_scan_vs_table_scan(self, perf_test_db):
        """Test index scan performance vs full table scan"""
        db = perf_test_db

        db.execute("CREATE TABLE large_table (id INTEGER PRIMARY KEY, search_field TEXT, data TEXT)")

        # Insert 10k rows
        for i in range(10000):
            db.execute(
                "INSERT INTO large_table (search_field, data) VALUES (:sf, :data)",
                {"sf": f"value_{i}", "data": f"data_{i}"}
            )

        # Full table scan (no index)
        start = time.time()
        db.fetch_one("SELECT * FROM large_table WHERE search_field = :sf", {"sf": "value_9999"})
        no_index_time = time.time() - start

        # Create index
        db.execute("CREATE INDEX idx_search_field ON large_table(search_field)")

        # Index scan
        start = time.time()
        db.fetch_one("SELECT * FROM large_table WHERE search_field = :sf", {"sf": "value_9999"})
        with_index_time = time.time() - start

        print(f"\nTable scan (10k rows): {no_index_time*1000:.2f}ms")
        print(f"Index scan (10k rows): {with_index_time*1000:.2f}ms")

        if no_index_time > 0 and with_index_time > 0:
            print(f"Speedup: {no_index_time/with_index_time:.1f}x")

    def test_covering_index_performance(self, perf_test_db):
        """Test covering index optimization"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, col1 TEXT, col2 INTEGER, col3 TEXT)")

        # Insert data
        for i in range(5000):
            db.execute(
                "INSERT INTO test (col1, col2, col3) VALUES (:c1, :c2, :c3)",
                {"c1": f"value_{i}", "c2": i, "c3": f"data_{i}"}
            )

        # Query without covering index
        start = time.time()
        db.execute("SELECT col1, col2 FROM test WHERE col1 = :val", {"val": "value_100"})
        no_covering_time = time.time() - start

        # Create covering index (includes all queried columns)
        db.execute("CREATE INDEX idx_covering ON test(col1, col2)")

        # Query with covering index
        start = time.time()
        db.execute("SELECT col1, col2 FROM test WHERE col1 = :val", {"val": "value_100"})
        covering_time = time.time() - start

        print(f"\nWithout covering index: {no_covering_time*1000:.2f}ms")
        print(f"With covering index: {covering_time*1000:.2f}ms")


class TestBatchOperations:
    """Test batch operation optimizations"""

    def test_transaction_batching(self, perf_test_db):
        """Test performance of batching operations in transactions"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, value INTEGER)")

        # Individual inserts (auto-commit each)
        start = time.time()
        for i in range(1000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})
        individual_time = time.time() - start

        # Clear table
        db.execute("DELETE FROM test")

        # Batched in single transaction (if supported)
        # SQLite auto-commits each statement, so this tests the overhead
        start = time.time()
        for i in range(1000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})
        batch_time = time.time() - start

        print(f"\n1000 individual inserts: {individual_time:.3f}s")
        print(f"1000 batched inserts: {batch_time:.3f}s")

    def test_bulk_update_strategies(self, perf_test_db):
        """Test different bulk update strategies"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER, status TEXT)")

        # Insert test data
        for i in range(5000):
            db.execute("INSERT INTO test (id, value, status) VALUES (:id, :val, :status)",
                       {"id": i, "val": i, "status": "pending"})

        # Strategy 1: Individual updates
        start = time.time()
        for i in range(0, 1000):
            db.execute("UPDATE test SET status = :status WHERE id = :id",
                       {"status": "processed", "id": i})
        individual_time = time.time() - start

        # Strategy 2: Single UPDATE with IN clause (if we had multiple IDs)
        # For this test, use a range
        start = time.time()
        db.execute("UPDATE test SET status = :status WHERE id >= :start AND id < :end",
                   {"status": "completed", "start": 1000, "end": 2000})
        bulk_time = time.time() - start

        print(f"\n1000 individual UPDATEs: {individual_time:.3f}s")
        print(f"1 bulk UPDATE (1000 rows): {bulk_time*1000:.2f}ms")
        print(f"Speedup: {individual_time/bulk_time:.1f}x")

        assert bulk_time < individual_time


class TestMemoryManagement:
    """Test memory usage and cleanup"""

    def test_large_result_set_memory(self, perf_test_db):
        """Test memory usage with large result sets"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, data TEXT)")

        # Insert 10k rows with substantial data
        large_data = "X" * 1000  # 1KB per row
        for i in range(10000):
            db.execute("INSERT INTO test (data) VALUES (:data)", {"data": large_data})

        # Measure memory before
        gc.collect()
        mem_before = sys.getsizeof(gc.get_objects())

        # Fetch all rows
        rows = db.execute("SELECT * FROM test")

        # Measure memory after
        gc.collect()
        mem_after = sys.getsizeof(gc.get_objects())

        mem_used_mb = (mem_after - mem_before) / (1024 * 1024)
        print(f"\nFetched 10k rows (~10MB data)")
        print(f"Memory delta: {mem_used_mb:.2f}MB")

        # Verify we got all rows
        assert len(rows) == 10000

    def test_connection_cleanup(self):
        """Test that connections are properly cleaned up"""
        config = ConnectionConfig(DatabaseType.SQLITE, "sqlite://:memory:")

        # Create and destroy many connections
        for _ in range(100):
            db = DatabaseManager(config)
            db.connect()
            db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO test (id) VALUES (1)")
            db.disconnect()

        # Force garbage collection
        gc.collect()

        # Should not have leaked connections (hard to test directly, but should not crash)

    def test_repeated_query_memory_leak(self, perf_test_db):
        """Test that repeated queries don't leak memory"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        db.execute("INSERT INTO test (value) VALUES (:val)", {"val": 42})

        # Measure initial memory
        gc.collect()
        initial_objects = len(gc.get_objects())

        # Execute same query many times
        for _ in range(10000):
            row = db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": 42})
            assert row is not None

        # Measure final memory
        gc.collect()
        final_objects = len(gc.get_objects())

        # Should not have excessive object growth
        object_growth = final_objects - initial_objects
        print(f"\nObject growth after 10k queries: {object_growth}")

        # Allow some growth, but not 10k objects
        assert object_growth < 1000


class TestResourceLimits:
    """Test behavior under resource constraints"""

    def test_max_connections_handling(self):
        """Test behavior when hitting connection limits"""
        import tempfile
        import os

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            config = ConnectionConfig(DatabaseType.SQLITE, f"sqlite:///{db_path}")

            # Try to create many simultaneous connections
            connections = []
            max_connections = 50

            for i in range(max_connections):
                db = DatabaseManager(config)
                db.connect()
                connections.append(db)

            # All connections should work (or some might fail gracefully)
            print(f"\nCreated {len(connections)} concurrent connections")

            # Cleanup
            for db in connections:
                db.disconnect()

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_large_transaction_handling(self, perf_test_db):
        """Test handling of very large transactions"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")

        # Very large transaction (10k inserts)
        start = time.time()
        for i in range(10000):
            db.execute("INSERT INTO test (data) VALUES (:data)", {"data": f"row_{i}"})
        elapsed = time.time() - start

        # Verify all inserted
        row = db.fetch_one("SELECT COUNT(*) as count FROM test")
        assert row["count"] == 10000

        print(f"\n10k inserts in single transaction: {elapsed:.3f}s ({10000/elapsed:.0f} ops/sec)")


class TestCachingBehavior:
    """Test query result caching (if implemented)"""

    def test_query_cache_hit_rate(self, perf_test_db):
        """Test query performance with repeated identical queries"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        db.execute("INSERT INTO test (value) VALUES (:val)", {"val": 42})

        # First query (cache miss)
        start = time.time()
        db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": 42})
        first_time = time.time() - start

        # Repeated queries (potential cache hits)
        times = []
        for _ in range(100):
            start = time.time()
            db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": 42})
            times.append(time.time() - start)

        avg_time = sum(times) / len(times)

        print(f"\nFirst query: {first_time*1000:.3f}ms")
        print(f"Average repeated query: {avg_time*1000:.3f}ms")

        # Without cache, times should be similar


class TestDatabaseLocking:
    """Test database locking performance and behavior"""

    def test_read_lock_contention(self):
        """Test read performance under lock contention"""
        import tempfile
        import os

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            config = ConnectionConfig(DatabaseType.SQLITE, f"sqlite:///{db_path}")

            # Setup
            setup_db = DatabaseManager(config)
            setup_db.connect()
            setup_db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
            for i in range(1000):
                setup_db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})
            setup_db.disconnect()

            # Multiple readers
            def reader(reader_id):
                db = DatabaseManager(config)
                db.connect()
                start = time.time()
                for _ in range(100):
                    db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": reader_id})
                elapsed = time.time() - start
                db.disconnect()
                return elapsed

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(reader, i) for i in range(10)]
                times = [f.result() for f in as_completed(futures)]

            avg_time = sum(times) / len(times)
            print(f"\n10 concurrent readers, 100 queries each")
            print(f"Average time per reader: {avg_time:.3f}s")

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_write_lock_wait_time(self):
        """Test write lock wait behavior"""
        import tempfile
        import os
        import threading

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            config = ConnectionConfig(DatabaseType.SQLITE, f"sqlite:///{db_path}")

            # Setup
            setup_db = DatabaseManager(config)
            setup_db.connect()
            setup_db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, value INTEGER)")
            setup_db.disconnect()

            # Two writers trying to write simultaneously
            results = {"times": []}

            def writer(value):
                db = DatabaseManager(config)
                db.connect()
                start = time.time()
                try:
                    result = db.execute("INSERT INTO test (value) VALUES (:val)", {"val": value})
                    elapsed = time.time() - start
                    db.disconnect()
                    results["times"].append(elapsed)
                    return True
                except Exception:
                    elapsed = time.time() - start
                    db.disconnect()
                    results["times"].append(elapsed)
                    return False

            # Start two writers concurrently
            threads = [
                threading.Thread(target=writer, args=(1,)),
                threading.Thread(target=writer, args=(2,)),
            ]

            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

            print(f"\n2 concurrent writes:")
            for i, t in enumerate(results["times"]):
                print(f"  Writer {i+1}: {t*1000:.2f}ms")

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestComplexQueryPerformance:
    """Test performance of complex queries"""

    def test_nested_subquery_performance(self, perf_test_db):
        """Test nested subquery performance"""
        db = perf_test_db

        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)")
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, region TEXT)")

        # Insert data
        for i in range(1000):
            db.execute("INSERT INTO users (name, region) VALUES (:name, :region)",
                       {"name": f"user_{i}", "region": f"region_{i % 10}"})

        for i in range(5000):
            db.execute("INSERT INTO orders (user_id, amount) VALUES (:uid, :amount)",
                       {"uid": (i % 1000) + 1, "amount": 100.0 + i})

        # Complex nested query
        start = time.time()
        rows = db.execute("""
            SELECT u.region, AVG(subq.total) as avg_total
            FROM users u
            JOIN (
                SELECT user_id, SUM(amount) as total
                FROM orders
                GROUP BY user_id
            ) subq ON u.id = subq.user_id
            GROUP BY u.region
        """)
        elapsed = time.time() - start

        print(f"\nComplex nested query (1000 users, 5000 orders): {elapsed*1000:.2f}ms")
        assert len(rows) > 0

    def test_multiple_join_performance(self, perf_test_db):
        """Test multiple JOIN performance"""
        db = perf_test_db

        # Create schema
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER)")
        db.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)")

        # Insert data
        for i in range(500):
            db.execute("INSERT INTO users (name) VALUES (:name)", {"name": f"user_{i}"})
            db.execute("INSERT INTO products (name, price) VALUES (:name, :price)",
                       {"name": f"product_{i}", "price": 10.0 + i})

        for i in range(2000):
            db.execute("INSERT INTO orders (user_id, product_id) VALUES (:uid, :pid)",
                       {"uid": (i % 500) + 1, "pid": (i % 500) + 1})

        # Multiple JOINs
        start = time.time()
        rows = db.execute("""
            SELECT u.name, p.name as product, p.price
            FROM orders o
            JOIN users u ON o.user_id = u.id
            JOIN products p ON o.product_id = p.id
            WHERE p.price > :min_price
        """, {"min_price": 100.0})
        elapsed = time.time() - start

        print(f"\nMultiple JOINs query (500 users, 2000 orders, 500 products): {elapsed*1000:.2f}ms")
        print(f"Result rows: {len(rows)}")


class TestLoadTesting:
    """Test sustained load performance"""

    def test_sustained_query_load(self, perf_test_db):
        """Test performance under sustained query load"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, value INTEGER)")
        for i in range(1000):
            db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})

        # Sustained load for 5 seconds
        query_count = 0
        start = time.time()
        duration = 5.0

        while time.time() - start < duration:
            db.fetch_one("SELECT * FROM test WHERE value = :val", {"val": query_count % 1000})
            query_count += 1

        elapsed = time.time() - start
        qps = query_count / elapsed

        print(f"\nSustained load for {duration}s:")
        print(f"Total queries: {query_count}")
        print(f"Queries per second: {qps:.0f}")

        # Should handle decent throughput
        assert qps > 100  # At least 100 QPS

    def test_mixed_workload_performance(self, perf_test_db):
        """Test mixed read/write workload"""
        db = perf_test_db

        db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT, value INTEGER)")

        # Mixed workload
        read_count = 0
        write_count = 0
        start = time.time()

        for i in range(1000):
            if i % 5 == 0:
                # 20% writes
                db.execute("INSERT INTO test (value) VALUES (:val)", {"val": i})
                write_count += 1
            else:
                # 80% reads
                db.execute("SELECT * FROM test WHERE value < :val", {"val": i})
                read_count += 1

        elapsed = time.time() - start

        print(f"\nMixed workload (1000 operations):")
        print(f"Reads: {read_count}, Writes: {write_count}")
        print(f"Total time: {elapsed:.3f}s")
        print(f"Throughput: {1000/elapsed:.0f} ops/sec")
