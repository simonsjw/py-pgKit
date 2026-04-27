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
)

__all__ = [
    "getLogger",
    "DBLogHandler",
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
