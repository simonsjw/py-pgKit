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
| `DatabaseBuilder`             | Full incremental database setup |
| `load_table_to_memory`        | Load table (or filtered subset) into list of dicts |
| `load_query_to_memory`        | Execute any SELECT and return results |
| `bulk_insert`                 | High-performance bulk loading via COPY (thousands–millions of rows) |
| `execute_query`               | Safe query execution with optional result fetching |
| `run_multi_statement_sql_script` | Run `.sql` migration/setup scripts |
| `query_logs`                  | Convenient filtered querying of the structured `logs` table |
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

## 📄 License

MIT License — © 2026 Simon (simonsjw)


