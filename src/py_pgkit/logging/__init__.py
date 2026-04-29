"""
py_pgkit.logging
================

Drop-in replacement for the standard `logging` module with optional
high-performance PostgreSQL structured logging.

Usage
-----
```python
import py_pgkit as pgk
from py_pgkit import logging as logging   # recommended

# Or for maximum stdlib compatibility:
# import logging
# pgk.configure_logging(settings)  # adds DB handler to root

logger = logging.getLogger(__name__)
logger.info("Hello from py-pgkit", extra={"obj": {"key": "value"}})
```

When a `PgSettings` (or connection object) is passed to `getLogger`,
a `DBLogHandler` is attached that writes to the `logs` table using
the shared pool from `py_pgkit.db`. All other behaviour is identical
to the standard library.

New in this version
-------------------
A module-level helper :func:`flush_all_handlers` is provided for
graceful shutdown and deterministic test teardown.  See its
documentation for usage patterns.
"""

from .core import (
    CRITICAL,
    DEBUG,
    ERROR,
    INFO,
    NOTSET,
    WARNING,
    DBLogHandler,
    FileHandler,
    Logger,
    LogRecord,
    NullHandler,
    StreamHandler,
    basicConfig,
    getLogger,
    flush_all_handlers,   # NEW — await this to guarantee all DB logs are persisted
)

__all__ = [
    "getLogger",
    "DBLogHandler",
    "flush_all_handlers",
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "NOTSET",
    "basicConfig",
    "StreamHandler",
    "FileHandler",
    "NullHandler",
    "Logger",
    "LogRecord",
]
