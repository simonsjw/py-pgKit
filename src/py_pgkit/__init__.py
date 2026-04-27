"""
py_pgkit
========

The lightweight PostgreSQL toolkit — asyncpg + SQLAlchemy + structured
DB logging, with zero friction.

This package is the unified successor to `infopypg` and `logger`
(published under simonsjw). It provides:

- Modern Pydantic settings (`PgSettings`)
- Shared, lazy asyncpg connection pools (`get_pool`)
- Full incremental database builder (`DatabaseBuilder`)
- Drop-in logging with optional PostgreSQL backend (`py_pgkit.logging`)

All original functionality is preserved while dramatically reducing
complexity through Pydantic and cleaner APIs.

Quick start
-----------
```python
import py_pgkit as pgk
from py_pgkit.db import PgSettings

settings = PgSettings(
    host="localhost",
    database="mydb",
    user="postgres",
    password="secret",
)

# One-time configuration (optional but recommended)
pgk.configure_logging(settings)

logger = pgk.logging.getLogger(__name__)
logger.info("Application started", extra={"obj": {"version": "1.0"}})

# Or use the builder
from py_pgkit.db import DatabaseBuilder
builder = DatabaseBuilder(settings)
await builder.build()
```

Import style
------------
`import py_pgkit as pgk` (as requested).

You may also do:
- `from py_pgkit.db import PgSettings, get_pool, DatabaseBuilder`
- `from py_pgkit import logging as logging` (recommended over bare `import logging`)
"""

from __future__ import annotations

# Re-export logging submodule for the nice `pgk.logging.getLogger` experience
from . import logging
from .db import DatabaseBuilder, PgSettings, close_all_pools, get_pool
from .logging import getLogger as logging_getLogger                                       # for convenience

__all__ = [
    "PgSettings",
    "get_pool",
    "close_all_pools",
    "DatabaseBuilder",
    "logging",
    "logging_getLogger",
]


def configure_logging(
    default_settings: PgSettings | None = None,
    level: int = 20,                                                                      # INFO
) -> None:
    """
    One-time global configuration helper.

    After calling this, plain `pgk.logging.getLogger("name")` calls
    will automatically include a DB handler (if `default_settings` is
    supplied).

    This is the recommended way to set up logging for an entire
    application or library ecosystem.
    """
    if default_settings is not None:
        # Prime the pool and attach a root DB handler
        root_logger = logging_getLogger(None, conn=default_settings, level=level)
        root_logger.info("py-pgkit logging configured with DB backend")
    else:
        # Just set level on root
        import logging as std_logging

        std_logging.getLogger().setLevel(level)
