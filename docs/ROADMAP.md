# Remaining Database Issues and Implementation Plan

This document tracks remaining issues that need attention and provides an implementation plan for fixes and tests.

## ✅ **Resolved Issues**

The following issues have been **fixed and tested**:

1. ✅ **fetch_one() MSSQL dict conversion** - Fixed in [manager.py:692-694](../nexusql/manager.py#L692-L694)
2. ✅ **fetch_all() MSSQL dict conversion** - Fixed; `fetch_all` was later removed in favour of `execute()`, which returns the same rows
3. ✅ **NULL parameter handling** - Tested in [test_database_edge_cases.py:24](../tests/integration/test_database_edge_cases.py#L24)
4. ✅ **Datetime parameter handling** - Tested in [test_database_edge_cases.py:53](../tests/integration/test_database_edge_cases.py#L53)
5. ✅ **Empty string vs NULL** - Tested in [test_database_edge_cases.py:82](../tests/integration/test_database_edge_cases.py#L82)
6. ✅ **Boolean parameters** - Tested in [test_database_edge_cases.py:110](../tests/integration/test_database_edge_cases.py#L110)
7. ✅ **Unicode/special characters** - Tested in [test_database_edge_cases.py:140](../tests/integration/test_database_edge_cases.py#L140)
8. ✅ **Multiple NULLs in query** - Tested in [test_database_edge_cases.py:246](../tests/integration/test_database_edge_cases.py#L246)
9. ✅ **Quotes and special SQL chars** - Tested in [test_database_edge_cases.py:282](../tests/integration/test_database_edge_cases.py#L282)
10. ✅ **Large string parameters** - Tested in [test_database_edge_cases.py:318](../tests/integration/test_database_edge_cases.py#L318)
11. ✅ **Numeric parameter types** - Tested in [test_database_edge_cases.py:346](../tests/integration/test_database_edge_cases.py#L346)

---

## ✅ **Recently Resolved Issues**

### ✅ Issue #1: execute_script() SQL Splitting is Fragile - **FIXED**

**Location**: `nexusql/manager.py:792-935`

**Problem**: For non-SQLite databases, `execute_script()` was using naive `;` splitting which failed on SQL containing semicolons in string literals or function bodies.

**Solution**: Implemented robust SQL statement parser `_split_sql_statements()` that properly handles:
- Single-quoted strings with escaped quotes (`'a''b'`)
- Double-quoted identifiers with escaped quotes (`"table""name"`)
- Dollar-quoted strings for PostgreSQL functions (`$$`, `$tag$`)
- Single-line comments (`--`)
- Multi-line comments (`/* */`)
- Comment-only statements are filtered out to prevent empty query errors

**Tests Added**:
- Unit tests: [tests/unit/test_sql_splitting.py](../tests/unit/test_sql_splitting.py) - 14 test cases
- Integration tests: [tests/integration/test_execute_script.py](../tests/integration/test_execute_script.py) - 35 test cases across PostgreSQL, MySQL, and MSSQL

**Test Results**: ✅ All 49 tests passing across all database backends

### ✅ Issue #2: Binary/BLOB Parameter Handling - **FIXED**

**Location**: No code changes required - binary data already handled correctly by `_convert_params()`

**Problem**: No tests existed to verify that binary data (bytes) can be passed as parameters and retrieved correctly across different database backends.

**Solution**: Created comprehensive test suite demonstrating that binary data handling works correctly without code modifications. The existing parameter conversion system properly handles `bytes` objects.

**Database-Specific Types Tested**:
- SQLite: BLOB
- PostgreSQL: BYTEA
- MySQL: LONGBLOB (for larger data)
- MSSQL: VARBINARY(MAX)

**Tests Added**:
- Integration tests: [tests/integration/test_binary_data.py](../tests/integration/test_binary_data.py) - 60 test cases covering:
  - Basic binary INSERT/SELECT operations
  - Empty bytes and NULL handling
  - All byte values (0-255)
  - Special sequences (PNG headers, null bytes, high-entropy data)
  - Various sizes (1KB, 64KB, 1MB)
  - Multiple rows and UPDATE operations
  - Edge cases (SQL injection patterns, repeated bytes)

**Test Results**: ✅ All 60 tests passing across SQLite, PostgreSQL, MySQL, and MSSQL

### ✅ Issue #3: Connection Pooling/Concurrent Access - **FIXED**

**Location**: Documentation added to [README.md](../README.md#thread-safety)

**Problem**: No documentation or tests existed for thread safety or concurrent database access patterns.

**Solution**:
1. **Documented thread safety limitations** - Added comprehensive section to README explaining:
   - Why `DatabaseManager` is NOT thread-safe
   - Unsafe patterns to avoid (shared instances)
   - Safe patterns to use (per-thread instances)
   - Thread pool pattern examples
   - Future connection pooling plans

2. **Created comprehensive concurrency test suite** - Validates that separate `DatabaseManager` instances work safely in parallel

**Tests Added**:
- Integration tests: [tests/integration/test_concurrency.py](../tests/integration/test_concurrency.py) - 28 test cases covering:
  - Concurrent read operations (multiple readers accessing same data)
  - Concurrent write operations (parallel inserts and updates)
  - Mixed read/write operations
  - High concurrency stress tests (50+ concurrent workers)
  - Rapid connect/disconnect patterns

**Test Results**: ✅ All 28 tests passing across SQLite, PostgreSQL, MySQL, and MSSQL

**Key Findings**:
- ✅ Multiple `DatabaseManager` instances work safely in parallel
- ✅ Each instance maintains its own connection and state
- ✅ No race conditions when using separate instances per thread
- ⚠️ Users must create separate instances - sharing is unsafe (now documented)

---

### ✅ Issue #4: Auto-increment ID Retrieval - **FIXED**

**Location**: `DatabaseManager.execute(..., return_id=True)` and `AsyncPostgresBackend.execute(..., return_id=True)`

**Problem**: After INSERT with an auto-increment primary key, every database handed the ID back differently, so callers reached for `db._connection.cursor().lastrowid` (wrong under pooling) or wrote per-backend SQL.

**Solution**: One flag, implemented per backend behind the same call:
- SQLite: `cursor.lastrowid`
- PostgreSQL: the table's primary-key column is read from `pg_index` once per table (cached) and `RETURNING "<pk>"` is appended to the INSERT. `lastval()` was rejected: on a pooled connection it reports a stale sequence value when the table has no sequence at all.
- MySQL: `cursor.lastrowid` (pymysql's `LAST_INSERT_ID()`), with 0 mapped to None
- MSSQL: `SELECT CAST(SCOPE_IDENTITY() AS BIGINT)` appended to the INSERT batch, since SCOPE_IDENTITY() is NULL in a separate batch

```python
user_id = db.execute(
    "INSERT INTO users (name) VALUES (:name)", {'name': 'John'}, return_id=True)
```

Returns None when no row was inserted. An INSERT that already carries RETURNING (or OUTPUT on MSSQL) returns the first column of its first row. A PostgreSQL table with no, or a composite, primary key raises ValueError pointing at an explicit RETURNING.

**Tests Added**:
- Unit tests: [tests/unit/test_return_id.py](../tests/unit/test_return_id.py) - SQLite driver path, MySQL/MSSQL/PostgreSQL branches against driver-shaped fakes, helper parsing
- Integration tests: [tests/integration/test_return_id.py](../tests/integration/test_return_id.py) - parametrised over all four backends, plus PostgreSQL edge cases and the async backend

---

## 🔴 **Outstanding Issues (Future Enhancements)**

None at present; see the forward-looking features below.

---

## 📊 **Summary and Priorities**

| Issue | Priority | Effort | Status |
|-------|----------|--------|--------|
| ~~execute_script() splitting~~ | 🔴 High | ~~4-6h~~ | ✅ **Completed** |
| ~~Binary/BLOB support~~ | 🟡 Medium | ~~3-4h~~ | ✅ **Completed** |
| ~~Concurrency docs/tests~~ | 🟢 Low | ~~2-3h~~ | ✅ **Completed** |
| ~~Auto-increment ID~~ | 🔵 Future | ~~8-12h~~ | ✅ **Completed** |

**Phase 1 Critical Issues: All Complete! 🎉**

---

## 🔮 **Forward-Looking Features** (From FORWARD_LOOKING_DATABASE_REQUIREMENTS.md)

The following features are **not yet implemented** but may be needed for future use cases:

### 🔴 High Priority (Production Readiness)
| Feature | Description | Effort | Priority |
|---------|-------------|--------|----------|
| ~~Connection Pooling~~ | Done in 0.3.0: pooled server backends, see [POOLING_DESIGN.md](POOLING_DESIGN.md) | — | Done |
| **Savepoints** | `transaction()` shipped in 0.3.0; nested use joins the outer transaction, so savepoints remain | 4-6h | High |
| **Streaming Results** | Yield large result sets in chunks (avoid memory issues) | 6-8h | High |
| **Query Performance Tools** | Slow query logging, EXPLAIN analysis | 4-6h | High |

### 🟡 Medium Priority (Advanced Features)
| Feature | Description | Effort | Priority |
|---------|-------------|--------|----------|
| ~~Row-Level Security~~ | Done: `tenant()` / `enable_row_security()` / `scoped()`; PostgreSQL policies, scoped views elsewhere, see [ROW_LEVEL_SECURITY.md](ROW_LEVEL_SECURITY.md) | — | Done |
| ~~Migration Rollback~~ | Done: `MigrationRunner.rollback()`, `rollback_to()`, `plan_rollback()`, see [MIGRATIONS.md](MIGRATIONS.md) | — | Done |
| ~~Backup/Recovery~~ | Done: native `backup()`/`restore()` on every backend plus portable `export_tables()`/`import_tables()`, see [BACKUP_RECOVERY.md](BACKUP_RECOVERY.md). Point-in-time recovery is server-side and out of scope | — | Done |
| ~~Full-Text Search~~ | Done: `db.fts` create_index / drop_index / search / suggest on all four backends, see [FULL_TEXT_SEARCH.md](FULL_TEXT_SEARCH.md) | — | Done |

### 🟢 Low Priority (Scale & Advanced)
| Feature | Description | Effort | Priority |
|---------|-------------|--------|----------|
| ~~Sharding/Partitioning~~ | Done: `ShardedDatabaseManager` with key, hash and range routers, single-shard transactions, fan-out reads; see [SHARDING.md](SHARDING.md) | — | Done |
| ~~Change Data Capture~~ | Done: trigger-based `db.cdc`, see [CHANGE_DATA_CAPTURE.md](CHANGE_DATA_CAPTURE.md) | — | Done |
| **Encryption/Compliance** | Column-level encryption, PII masking, GDPR | 12-16h | Low |
| ~~Query Builder/ORM~~ | Done: `nexusql.query.Q`, a typed builder emitting canonical `:name` SQL (no ORM), see [QUERY_BUILDER.md](QUERY_BUILDER.md) | — | Done |
| ~~Test Fixtures~~ | Done: `nexusql.testing` factories, snapshots, `truncate` and the pytest plugin, see [TESTING.md](TESTING.md) | — | Done |
| ~~Schema Validation~~ | Done: `introspect` / `list_tables` / `validate_schema` / `check_params`, see [SCHEMA_VALIDATION.md](SCHEMA_VALIDATION.md) | — | Done |

### ❌ Not Applicable / Out of Scope
- **Analytics/OLAP** - Use dedicated analytics databases (ClickHouse, BigQuery)
- **Graph Queries** - Use graph databases (Neo4j)
- **Time-Series** - Use specialized databases (InfluxDB, TimescaleDB)

**Total Estimated Effort for All Forward-Looking Features**: ~140-200 hours

---

## 🎯 **Recommended Implementation Order**

### ✅ Phase 1: Fix Critical Issues - **COMPLETE! 🎉**
1. ✅ Execute_script() SQL splitting - **DONE**
2. ✅ Binary/BLOB support - **DONE**
3. ✅ Concurrency documentation and tests - **DONE**

### Phase 2: Production Readiness (30-42 hours)
4. ✅ Connection pooling - **DONE** (0.3.0)
5. Savepoints for nested `transaction()` (the context manager itself is done in 0.3.0)
6. Streaming query results
7. Slow query logging

### Phase 3: Advanced Features (36-50 hours)
8. ✅ Row-level security helpers - **DONE** ([ROW_LEVEL_SECURITY.md](ROW_LEVEL_SECURITY.md))
9. ✅ Migration rollback support - **DONE**
10. ✅ Backup/restore utilities - **DONE** ([BACKUP_RECOVERY.md](BACKUP_RECOVERY.md))
11. ✅ Full-text search support - **DONE** (`db.fts`, see [FULL_TEXT_SEARCH.md](FULL_TEXT_SEARCH.md))

### Phase 4: Scale & Enterprise (80-120 hours)
12. ✅ Database sharding framework - **DONE** ([SHARDING.md](SHARDING.md))
13. ✅ Change data capture - **DONE** (trigger-based `db.cdc`)
14. Encryption/compliance
15. ✅ Query builder API - **DONE** ([QUERY_BUILDER.md](QUERY_BUILDER.md))
16. ✅ Test data factories - **DONE** ([TESTING.md](TESTING.md))
17. ✅ Schema validation - **DONE** (see [SCHEMA_VALIDATION.md](SCHEMA_VALIDATION.md))

---

## 📝 **Dependencies to Add**

Update `pyproject.toml`:
```toml
[project.optional-dependencies]
# Future dependencies for advanced features
pooling = [
    "sqlalchemy>=2.0.0"  # For connection pooling
]
validation = [
    "pydantic>=2.0.0"  # For schema validation
]
```
