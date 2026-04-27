# py-pgkit

**The lightweight PostgreSQL toolkit** — `asyncpg` + `SQLAlchemy` + structured DB logging with zero friction.

`py-pgkit` (imported as `pgk`) is the unified successor to the `infopypg` and `logger` packages. It provides all their functionality with dramatically less boilerplate thanks to Pydantic.

## Installation

```bash
pip install py-pgkit
# or for development
pip install -e .
```

## Quick Start

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

    # One-time setup (recommended)
    pgk.configure_logging(settings)

    logger = pgk.logging.getLogger(__name__)
    logger.info("Application starting", extra={"obj": {"env": "prod"}})

    # Full database setup (idempotent)
    builder = pgk.db.DatabaseBuilder(settings)
    await builder.build()

    # Or just get a pool
    pool = await pgk.db.get_pool(settings)
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT version()")
        print(version)

asyncio.run(main())
```

## Key Features

- **PgSettings** — Pydantic model with env var support (replaces manual `ResolvedSettingsDict`)
- **Shared asyncpg pools** — lazy, cached, high-performance
- **DatabaseBuilder** — incremental tablespace/DB/extensions/tables/triggers/partitioning
- **Smart logging** — `pgk.logging.getLogger(conn)` gives you DB-backed structured logs; plain usage is 100% stdlib compatible
- **SQLAlchemy integration** — `Base` and engine helpers included

## Logging Philosophy (as requested)

You can use **either**:

```python
from py_pgkit import logging as logging
logger = logging.getLogger(__name__)          # DB if configured, else stdlib
```

or pass a connection explicitly:

```python
logger = logging.getLogger(settings)          # forces DB mode
logger = logging.getLogger(__name__, conn=settings)
```

Libraries you configure will receive a normal `logging.Logger` object — they won't know or care whether DB logging is active.

## Migration from infopypg + logger

- Replace `from infopypg import ...` with `from py_pgkit.db import ...`
- Replace `from logger import setup_logger` with `from py_pgkit import logging`
- Use `PgSettings(...)` instead of raw dicts (or `PgSettings.model_validate(old_dict)`)

## License

MIT — © 2026 Simon (simonsjw)
