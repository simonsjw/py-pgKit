# py-pgkit

**The lightweight, batteries-included PostgreSQL toolkit for Python 3.12+**

`py-pgkit` (imported as `pgk`) provides a clean, modern, and powerful set of tools built on `asyncpg`, `SQLAlchemy`, and `Pydantic` — with optional high-performance structured logging directly to PostgreSQL.

---

## ✨ Key Features

- **Modern Settings** — `PgSettings` (Pydantic) with env-var support and full backward compatibility
- **Shared Connection Pools** — Lazy, cached `asyncpg` pools via `PgPoolManager`
- **Incremental Database Builder** — Create tablespaces, databases, extensions, tables (with dependency ordering), functions, and partitions
- **Data Loading Helpers** — `load_table_to_memory`, `load_query_to_memory`, and high-performance `bulk_insert` (COPY protocol)
- **Query Execution** — Safe single queries, multi-statement scripts, and `query_logs` (structured log retrieval)
- **Database Maintenance** — `ensure_functions_loaded` and `ensure_partition_exists`
- **Structured DB Logging** — Drop-in replacement for `logging` with automatic PostgreSQL storage (JSONB `obj` column)
- **Graceful Async Shutdown** — `flush_all_handlers()` ensures all asynchronous structured log messages are persisted before closing the connection pool with `close_all_pools()`
- **Zero Boilerplate** — One `configure_logging()` call and you're done

---

## 🚀 Quick Start

```python
import asyncio
import py_pgkit as pgk
from py_pgkit.db import PgSettings

async def main():
    settings = PgSettings(
        host="localhost",
        database="myapp",
        user="postgres",
        password="secret",
        extensions=["uuid-ossp", "pg_trgm"],
    )

    # === 1. One-time logging setup (optional but recommended) ===
    pgk.configure_logging(settings)

    logger = pgk.logging.getLogger(__name__)
    logger.info("Application started", extra={"obj": {"version": "1.0"}})

    # === 2. Full database setup ===
    builder = pgk.db.DatabaseBuilder(
        settings=settings,
        functions="/app/sql/functions/",      # load custom SQL functions
        partition_strategy="daily",           # auto-create daily partitions
    )
    await builder.build()

    # === 3. Load data into memory ===
    recent_users = await pgk.load_table_to_memory(
        "users", settings, limit=100, where_clause="created_at > $1", params=["2026-01-01"]
    )

    # === 4. High-volume insert (new in this release) ===
    new_records = [{"name": "Bob", "email": "bob@example.com"}, ...]
    await pgk.bulk_insert("users", new_records, settings)

    # === 5. Run a migration script ===
    with open("migrations/003_add_indexes.sql") as f:
        await pgk.run_multi_statement_sql_script(f.read(), settings)

    print("✅ py-pgkit setup complete!")

    # === 6. Graceful shutdown (NEW — recommended when using DB logging) ===
    await pgk.flush_all_handlers()   # ensure all structured logs are persisted
    await pgk.close_all_pools()

asyncio.run(main())
```

---

## 📦 Installation

```bash
pip install py-pgkit
```

For development:

```bash
git clone https://github.com/simonsjw/py-pgkit.git
cd py-pgkit
pip install -e ".[dev]"
```

---

## 🧩 Module Overview

### `py_pgkit.db`

| Function / Class              | Description |
|-------------------------------|-----------|
| `PgSettings`                  | Modern Pydantic settings (env vars, validation, dict-like access) |
| `get_pool(settings)`          | Get (or create) a cached asyncpg connection pool |
| `close_all_pools()`           | Gracefully close **all** cached asyncpg pools (call `flush_all_handlers()` first when DB logging is active) |
| `DatabaseBuilder`             | Full incremental database setup |
| `load_table_to_memory`        | Load table (or filtered subset) into list of dicts |
| `load_query_to_memory`        | Execute any SELECT and return results |
| `bulk_insert`                 | High-performance bulk loading via COPY (thousands–millions of rows) |
| `execute_query`               | Safe query execution with optional result fetching |
| `run_multi_statement_sql_script` | Run `.sql` migration/setup scripts |
| `query_logs`                  | Convenient filtered querying of the structured `logs` table (`level`, `logger_name`, `start_time`/`end_time`, `limit`, `order_by`) |
| `ensure_functions_loaded`     | Load custom SQL functions from directory/file/list |
| `ensure_partition_exists`     | Create range partitions on demand |

### `py_pgkit.logging`

Drop-in replacement for the standard library with optional DB backend:

```python
from py_pgkit import logging as logging

logger = logging.getLogger(__name__)           # stdlib by default
logger = logging.getLogger(settings)           # DB-backed
logger.info("Event", extra={"obj": {"user_id": 42}})
```

**New in this release**: `flush_all_handlers()` — async module-level helper that walks the entire logging hierarchy and awaits completion of every pending DB insert. Use it before `close_all_pools()` to guarantee zero message loss.

---

## 🔧 Advanced Usage

### DatabaseBuilder with Functions & Partitioning

```python
builder = pgk.db.DatabaseBuilder(
    settings=settings,
    models=[Base],                    # your SQLAlchemy models
    functions=Path("sql/functions/"), # load all .sql files
    partition_strategy="daily",
)
await builder.build()
```

### Logging with Multiple Databases

```python
app_logger = pgk.logging.getLogger("app", conn=app_settings)
audit_logger = pgk.logging.getLogger("audit", conn=audit_settings)
```

### Graceful Shutdown (Structured Logging + Pools)

When using the PostgreSQL-backed logger, log records are inserted asynchronously (fire-and-forget) to keep your application responsive.

**Always flush before closing pools** to avoid losing the final log messages:

```python
import asyncio
import py_pgkit as pgk
from py_pgkit.db import close_all_pools

async def main():
    settings = PgSettings(...)          # or via configure_logging
    pgk.configure_logging(settings)

    logger = pgk.logging.getLogger(__name__)
    logger.info("Starting up", extra={"obj": {"pid": os.getpid()}})

    # ... application logic ...

    logger.info("Shutting down cleanly", extra={"obj": {"status": "ok"}})

    await pgk.flush_all_handlers()      # ← NEW: guarantees all DB logs are written
    await close_all_pools()             # now safe to close

asyncio.run(main())
```

**In pytest-asyncio fixtures** (recommended pattern):

```python
@pytest.fixture(scope="function")
async def app_logger(settings):
    pgk.configure_logging(settings)
    logger = pgk.logging.getLogger("test-app")
    yield logger
    await pgk.flush_all_handlers()      # flush before pool teardown
```

The `DBLogHandler` automatically tracks every pending insert task. `flush_all_handlers()` discovers **all** attached handlers (even on child loggers) and awaits them concurrently with `asyncio.gather(..., return_exceptions=True)`.

### Querying the logs table

```python
import py_pgkit as pgk
from py_pgkit.db import PgSettings
from datetime import datetime, timedelta

settings = PgSettings(...)
pgk.configure_logging(settings)

# Recent errors from a specific logger (uses correct parameters: logger_name, start_time/end_time)
errors = await pgk.query_logs(
    settings,
    level="ERROR",
    logger_name="ai_api.core",
    start_time=(datetime.utcnow() - timedelta(days=1)).isoformat(),
    limit=50,
    order_by="tstamp DESC",
)

for log in errors:
    print(log["tstamp"], log["loglvl"], log["message"], log.get("obj"))
```

---

## Testing

All tests are unit tests with heavy mocking (no live PostgreSQL required).
See [tests/README.md](tests/README.md) for full details, debugging tips, and how to add new tests.

### Why This Approach Works So Well

The suite is deliberately **mock-centric** (using `patch_get_pool`, `AsyncMock` + proper async context managers, `side_effect` lists, and strict `call_args` assertions). This gives you:

- **Zero external dependencies** for running tests (fast CI, works on any laptop).
- **Precise verification** of generated SQL, chunking logic, error handling, and comment stripping without ever touching a real DB.
- **Excellent isolation** via per-test event loops, autouse registry clearing, and the `settings` + `patch_get_pool` fixture combo.

### Quick Reference (from the new README)

**Running & investigating:**
```bash
pytest -q                          # normal run (asyncio already configured)
pytest -v -s --tb=short            # verbose + see prints + concise tracebacks
pytest -k "bulk_insert and error"  # keyword filtering
pytest --pdb tests/test_query.py   # debugger on failure
```

**Key fixtures you’ll use in almost every new test:**
- `settings` → minimal `PgSettings` (never connects)
- `patch_get_pool` → the universal DB mock (patches `get_pool` in all modules)
- `sample_records_dict` / `multipart_sql_script` → ready-made test data

**Drafting a new test (copy-paste template):**
```python
@pytest.mark.asyncio
async def test_your_new_feature(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"id": 42}]

    result = await your_function("arg", settings)

    mock_conn.fetch.assert_awaited_once()
    sql = mock_conn.fetch.call_args[0][0]
    assert "SELECT" in sql and "WHERE" in sql
    assert result == [{"id": 42}]
```

## 📄 License

MIT License — © 2026 Simon (simonsjw)
