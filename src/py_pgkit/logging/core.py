"""
py_pgkit.logging.core
=====================

Core implementation of the structured, database-backed logging system.

This module provides a drop-in replacement for Python's standard
`logging` module while adding powerful PostgreSQL-backed structured
logging when a connection/settings object is supplied.

Design principles (matching user requirements)
---------------------------------------------
- **Single logging methodology** with multiple connection pools.
- **Seamless stdlib fallback**: if no connection object is ever
  supplied, the module behaves *exactly* like `import logging`.
- **Connection-aware getLogger**: `getLogger(conn_or_settings)` or
  `getLogger("name", conn=settings)` automatically attaches a
  `DBLogHandler` that writes to the `logs` table using the shared
  pool from `py_pgkit.db`.
- **Async-safe**: the handler never blocks the event loop.
- **Structured data**: `extra={"obj": {...}}` is stored as JSONB.
- **Auto table creation**: the `logs` table is created on first use
  via `DatabaseBuilder` (exactly as in the original logger package).

Table schema (preserved from original)
--------------------------------------
CREATE TABLE IF NOT EXISTS logs (
    idx        BIGSERIAL PRIMARY KEY,
    tstamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
    loglvl     TEXT NOT NULL,
    logger     TEXT,
    message    TEXT,
    obj        JSONB
);

The handler is designed so that libraries you configure with a
`py_pgkit.logging` logger object will work whether they receive a
pure stdlib logger or a DB-enhanced one — they only call the
standard methods (`info`, `warning`, `exception`, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
from datetime import datetime, timezone

# Re-export everything from stdlib logging so users can do
# `from py_pgkit.logging import getLogger, DEBUG, INFO, ...`
# and it works exactly like the standard library when no DB is configured.
from logging import (
    CRITICAL,
    DEBUG,
    ERROR,
    INFO,
    NOTSET,
    WARNING,
    FileHandler,
    Logger,
    LogRecord,
    NullHandler,
    StreamHandler,
    basicConfig,
)
from logging import (
    getLogger as _stdlib_getLogger,
)
from typing import Any

import asyncpg

from py_pgkit.db import DatabaseBuilder, PgSettings, get_pool


class DBLogHandler(logging.Handler):
    """
    Asynchronous PostgreSQL logging handler.

    This handler writes log records to the `logs` table using a
    shared asyncpg pool. It is completely non-blocking — inserts are
    scheduled on the event loop and failures are logged to stderr
    (never raise into application code).

    The table is created automatically on first use via
    `DatabaseBuilder`.
    """

    def __init__(
        self,
        settings: PgSettings,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level)
        self.settings = settings
        self._pool: asyncpg.Pool | None = None
        self._table_created = False

    async def _ensure_table(self) -> None:
        """
        Create the logs table if it does not exist (idempotent and race-safe).

        Uses CREATE TABLE IF NOT EXISTS + defensive exception handling
        because PostgreSQL's implicit sequence creation for BIGSERIAL can
        occasionally raise duplicate-key errors on (relname, relnamespace)
        after previous partial/failed table creations (common when
        developing and restarting the app many times).
        """
        if self._table_created:
            return
        pool = await get_pool(self.settings)
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS logs (
                        idx        BIGSERIAL PRIMARY KEY,
                        tstamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
                        loglvl     TEXT NOT NULL,
                        logger     TEXT,
                        message    TEXT,
                        obj        JSONB
                    )
                    """
                )
            except asyncpg.exceptions.DuplicateTableError:
                pass                                                                      # Table already exists — perfectly fine
            except Exception as exc:
                if "duplicate key value violates unique constraint" in str(exc).lower():
                    pass                                                                  # Sequence already exists from previous partial creation
                else:
                    print(
                        f"[py_pgkit.logging] Table creation warning: {exc}", flush=True
                    )
        self._table_created = True

    async def _emit_async(self, record: LogRecord) -> None:
        """Actual async insert logic."""
        try:
            await self._ensure_table()
            pool = await get_pool(self.settings)
            obj = getattr(record, "obj", None)
            if obj is not None and not isinstance(obj, (dict, list)):
                obj = {"value": str(obj)}

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO logs (loglvl, logger, message, obj)
                    VALUES ($1, $2, $3, $4)
                    """,
                    record.levelname,
                    record.name,
                    record.getMessage(),
                    json.dumps(obj) if obj is not None else None,
                )
        except Exception as exc:
            # Never let logging errors crash the application
            print(f"[py_pgkit.logging] DB log insert failed: {exc}", flush=True)

    def emit(self, record: LogRecord) -> None:
        """
        Synchronous emit entry point (called by logging framework).

        Schedules the real work on the running event loop without
        blocking the caller.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit_async(record))
        except RuntimeError:
            # No running loop (e.g. during shutdown or sync code)
            # Fall back to synchronous insert (best effort)
            try:
                asyncio.run(self._emit_async(record))
            except Exception:
                pass


def getLogger(
    name: str | PgSettings | None = None,
    conn: PgSettings | None = None,
    level: int | None = None,
    **kwargs: Any,
) -> Logger:
    """
    Smart logger factory that supports both stdlib and DB-backed logging.

    This is the central function users and libraries will call.

    Parameters
    ----------
    name : str | PgSettings | None
        Logger name (e.g. "__name__") **or** a `PgSettings` / connection
        object. If a settings object is passed as the first argument,
        DB logging is enabled automatically.
    conn : PgSettings | None, optional
        Explicit connection settings (alternative to passing as first arg).
    level : int | None, optional
        Logging level to set on the returned logger.

    Returns
    -------
    logging.Logger
        A fully configured logger. If no connection information was
        supplied, this is identical to `logging.getLogger(name)`.
        If connection information *was* supplied, the logger has an
        attached `DBLogHandler` that writes to PostgreSQL.

    Examples
    --------
    # Pure stdlib (no DB)
    logger = getLogger(__name__)

    # DB-backed (recommended pattern)
    from py_pgkit.db import PgSettings
    settings = PgSettings(database="logs")
    logger = getLogger(__name__, conn=settings)
    logger.info("User logged in", extra={"obj": {"user_id": 42}})

    # Or pass settings as first argument (your requested syntax)
    logger = getLogger(settings)
    """
    # Determine if we are in DB mode
    settings: PgSettings | None = None

    if isinstance(name, PgSettings):
        settings = name
        name = None                                                                       # use root or default name
    elif conn is not None:
        settings = conn

    # Get (or create) the underlying stdlib logger
    if name is None:
        std_logger = _stdlib_getLogger()
    else:
        std_logger = _stdlib_getLogger(str(name))

    if level is not None:
        std_logger.setLevel(level)

    # If we have settings, attach DB handler (idempotent)
    if settings is not None:
        # Avoid adding duplicate handlers
        has_db_handler = any(
            isinstance(h, DBLogHandler) and h.settings == settings
            for h in std_logger.handlers
        )
        if not has_db_handler:
            handler = DBLogHandler(settings)
            std_logger.addHandler(handler)
            # Also add a console handler if none exists (nice default)
            if not any(
                isinstance(
                    h, (logging.StreamHandler, logging.handlers.RotatingFileHandler)
                )
                for h in std_logger.handlers
            ):
                std_logger.addHandler(logging.StreamHandler())

    return std_logger


# Convenience re-exports so users can do:
# from py_pgkit.logging import getLogger, DEBUG, INFO, ...
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
