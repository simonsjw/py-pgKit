"""
py_pgkit.logging.core
=====================

Core implementation of the structured, database-backed logging system
for the ``py-pgkit`` PostgreSQL toolkit.

This module provides a **drop-in replacement** for the Python standard
library's :mod:`logging` module while transparently adding high-performance,
PostgreSQL-backed structured logging whenever a :class:`~py_pgkit.db.settings.PgSettings`
(or equivalent connection object) is supplied to :func:`getLogger`.

Design Principles
-----------------
The implementation follows these core principles (matching the original
design requirements of the ``logger`` and ``infopypg`` packages that
``py-pgkit`` unifies):

- **Single logging methodology** — applications use one consistent
  ``getLogger`` API whether or not a database backend is configured.
- **Seamless stdlib fallback** — when no connection/settings object is
  ever supplied, the module behaves *exactly* like ``import logging``.
- **Connection-aware logger factory** — ``getLogger(name, conn=settings)``
  or ``getLogger(settings)`` automatically attaches a :class:`DBLogHandler`
  that writes to the ``logs`` table via the shared :func:`~py_pgkit.db.pool.get_pool`
  singleton.
- **Fully async-safe and non-blocking** — the handler never blocks the
  event loop. Inserts are scheduled as ``asyncio.Task`` objects.
- **Structured data support** — the conventional ``extra={"obj": {...}}``
  payload is stored natively as PostgreSQL ``JSONB``.
- **Automatic, race-safe table provisioning** — the ``logs`` table (and
  its ``BIGSERIAL`` sequence) is created on first use with defensive
  exception handling for the well-known PostgreSQL catalog races that
  occur during rapid development restarts.
- **Failure isolation** — database errors during logging are never
  propagated to application code; they are reported to stderr only.
- **Testability and graceful shutdown** — pending insert tasks are
  tracked so that the async :meth:`aflush` coroutine can be awaited in
  tests or during application teardown, even after
  :func:`~py_pgkit.db.pool.close_all_pools` has been called.

Table Schema (idempotent)
-------------------------
The handler automatically ensures the following table exists::

    CREATE TABLE IF NOT EXISTS logs (
        idx        BIGSERIAL PRIMARY KEY,
        tstamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
        loglvl     TEXT NOT NULL,
        logger     TEXT,
        message    TEXT,
        obj        JSONB
    );

The ``obj`` column stores arbitrary JSON-serialisable payloads passed via
``extra={"obj": ...}``. Non-dict/list objects are wrapped as
``{"value": str(obj)}`` for safety.

See Also
--------
:mod:`py_pgkit.logging`
    Public re-export and convenience wrappers.
:func:`py_pgkit.db.get_pool`
    Shared ``asyncpg`` pool factory (singleton per connection key).
:func:`py_pgkit.__init__.configure_logging`
    One-shot global configuration helper that attaches a root handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
from datetime import datetime, timezone

# Re-export everything from stdlib logging so users can do
# ``from py_pgkit.logging import getLogger, DEBUG, INFO, ...``
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
from logging import getLogger as _stdlib_getLogger
from typing import Any

import asyncpg

from py_pgkit.db import PgSettings, get_pool


class DBLogHandler(logging.Handler):
    """
    Asynchronous PostgreSQL logging handler.

    This handler writes structured :class:`logging.LogRecord` objects to the
    ``logs`` table in a PostgreSQL database using a shared :class:`asyncpg.Pool`
    obtained from :func:`py_pgkit.db.get_pool`.  It is designed to be
    **completely non-blocking** from the caller's perspective: the
    :meth:`emit` override (called by the logging framework) merely schedules
    the real work on the running event loop and returns immediately.

    If no running event loop is detected (for example during interpreter
    shutdown or when logging from a synchronous context), a best-effort
    synchronous fallback using :func:`asyncio.run` is employed.

    Key Features
    ------------
    - **Automatic table creation** — the ``logs`` table is created on first
      use via :meth:`_ensure_table`.  The implementation is idempotent and
      contains defensive handling for PostgreSQL's well-known catalog races
      on ``BIGSERIAL`` sequences and relation names (common when an
      application is started and stopped rapidly during development).
    - **Structured JSONB storage** — any ``extra={"obj": <json-serialisable>}``
      argument passed to logger methods (``info``, ``warning``, ``error``,
      ``exception``, …) is stored in the ``obj`` column.  Non-dict/list
      objects are safely wrapped.
    - **Pending-task tracking** — every scheduled insert task is kept in the
      private set ``self._pending``.  This enables the public coroutine
      :meth:`aflush` which can be awaited by tests or shutdown code to
      guarantee that all log records have been persisted.
    - **Failure isolation** — all exceptions raised by ``asyncpg`` or during
      JSON serialisation are caught inside :meth:`_emit_async` and reported
      to stderr via ``print(..., flush=True)``.  The application is never
      disrupted by logging failures.
    - **Pool lifecycle awareness** — works correctly even when
      :func:`py_pgkit.db.close_all_pools` has been called; the next insert
      will transparently obtain a fresh pool.
    - **Stdlib contract preserved** — :meth:`flush` remains a synchronous
      no-op so that the Python logging framework (which calls it during
      ``close()`` / ``shutdown()``) never receives a coroutine object.

    The handler is normally instantiated and attached automatically by
    :func:`getLogger` when a :class:`~py_pgkit.db.settings.PgSettings`
    object is supplied.  It can also be attached manually::

        from py_pgkit.db import PgSettings
        from py_pgkit.logging import DBLogHandler
        import logging

        settings = PgSettings(database="mydb")
        handler = DBLogHandler(settings, level=logging.INFO)
        logging.getLogger("myapp").addHandler(handler)

    Attributes
    ----------
    settings : PgSettings
        The connection settings used to obtain the shared pool.
    _pending : set[asyncio.Task]
        Set of still-pending insert tasks.  Tasks are automatically removed
        when they complete (via ``add_done_callback``).
    _table_created : bool
        Internal flag indicating whether :meth:`_ensure_table` has already
        succeeded for this handler instance.

    See Also
    --------
    getLogger : Factory that attaches a ``DBLogHandler`` when a settings
        object is supplied.
    aflush : Public coroutine to await completion of all pending inserts.
    flush_all_handlers : Module-level helper that flushes every DB handler.
    """

    def __init__(
        self,
        settings: PgSettings,
        level: int = logging.NOTSET,
    ) -> None:
        """
        Initialise a new database-backed logging handler.

        Parameters
        ----------
        settings : PgSettings
            Validated connection settings (host, port, database, user,
            password, pool sizing, etc.).  The same settings object (or any
            other object that produces an identical connection key) will
            share a single :class:`asyncpg.Pool` via the global registry in
            :mod:`py_pgkit.db.pool`.
        level : int, optional
            Minimum logging level for this handler (default: ``NOTSET``,
            i.e. inherit from the logger).  Standard values are
            ``logging.DEBUG``, ``logging.INFO``, ``logging.WARNING``, etc.

        Notes
        -----
        The ``_pool`` attribute is retained for backwards compatibility with
        earlier internal versions but is no longer used; the handler always
        obtains the current pool via :func:`get_pool` on every insert.  This
        guarantees that the handler continues to work after
        :func:`~py_pgkit.db.close_all_pools` has been called.
        """
        super().__init__(level)
        self.settings = settings
        self._pool: asyncpg.Pool | None = None                                            # retained for API compatibility
        self._table_created: bool = False
        self._pending: set[asyncio.Task[None]] = set()

    async def _ensure_table(self) -> None:
        """
        Create the ``logs`` table if it does not already exist.

        The method is idempotent and race-safe.  It uses
        ``CREATE TABLE IF NOT EXISTS`` together with defensive exception
        handling because PostgreSQL can raise spurious
        ``DuplicateTableError`` or unique-constraint violations on the
        implicit sequence / relation catalog entries when an application
        is started and stopped rapidly during development.

        The method is **private**; it is called automatically from
        :meth:`_emit_async` on first use.

        Notes
        -----
        After successful execution the instance flag ``self._table_created``
        is set to ``True`` so that subsequent calls become a cheap no-op.
        The flag is **per-handler instance**; if you attach multiple
        handlers with different settings objects they will each perform
        their own (idempotent) check.
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
                # Table already exists — perfectly fine (another task won the race)
                pass
            except Exception as exc:
                if "duplicate key value violates unique constraint" in str(exc).lower():
                    # Sequence already exists from a previous partial creation
                    pass
                else:
                    print(
                        f"[py_pgkit.logging] Table creation warning: {exc}",
                        flush=True,
                    )
        self._table_created = True

    async def _emit_async(self, record: LogRecord) -> None:
        """
        Perform the actual asynchronous INSERT into the ``logs`` table.

        This coroutine is the heart of the handler.  It is scheduled by
        :meth:`emit` (or executed directly via :func:`asyncio.run` in the
        synchronous fallback path).

        The method never raises into the logging framework or the
        application; every exception path is caught and reported to stderr.

        Parameters
        ----------
        record : logging.LogRecord
            The log record produced by a logger call such as
            ``logger.info("msg", extra={"obj": {"user": 42}})``.

        Notes
        -----
        - ``record.getMessage()`` is used so that any ``%``-style or
          ``{}-style`` formatting has already been applied by the logging
          machinery.
        - The ``obj`` payload is taken from ``record.obj`` (the attribute
          that the logging framework injects from the ``extra`` dict).
        - If the payload is not already a ``dict`` or ``list`` it is wrapped
          as ``{"value": str(obj)}`` so that it remains valid JSONB.
        """
        try:
            await self._ensure_table()
            pool = await get_pool(self.settings)

            obj: Any = getattr(record, "obj", None)
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
            # Never let logging errors crash the application or the event loop
            print(f"[py_pgkit.logging] DB log insert failed: {exc}", flush=True)

    def emit(self, record: LogRecord) -> None:
        """
        Synchronous entry point called by the :mod:`logging` framework.

        This override of :meth:`logging.Handler.emit` is what makes the
        handler non-blocking.  It obtains the running event loop (if any)
        and schedules :meth:`_emit_async` as a background task.  The task
        reference is stored in ``self._pending`` so that :meth:`aflush` can
        later await its completion.

        When no running loop exists (for example during ``atexit`` or when
        logging from a thread without an event loop), the method falls back
        to a synchronous ``asyncio.run(...)`` call.  This fallback is best
        effort only; it may raise if the loop is already closed.

        Parameters
        ----------
        record : logging.LogRecord
            The log record to be emitted.

        Notes
        -----
        Because ``create_task`` is used, the actual database work may occur
        *after* the caller's ``logger.info(...)`` statement has returned.
        This is the intended behaviour for high-throughput async
        applications.  Tests that need deterministic ordering should call
        ``await handler.aflush()`` (or the module-level helper
        :func:`flush_all_handlers`) before asserting on the ``logs`` table
        contents.
        """
        try:
            loop = asyncio.get_running_loop()
            task: asyncio.Task[None] = loop.create_task(self._emit_async(record))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        except RuntimeError:
            # No running loop (shutdown, sync context, or nested event loop)
            # Fall back to a blocking call — best effort only
            try:
                asyncio.run(self._emit_async(record))
            except Exception:
                # Swallow any error in the fallback path; logging must never
                # break the application
                pass

    def flush(self) -> None:
        """
        Synchronous flush hook required by the :mod:`logging` framework.

        This method is called by :meth:`logging.Handler.close` and by
        :func:`logging.shutdown` during interpreter exit.  Because the real
        persistence work is performed by background ``asyncio.Task``
        objects, this synchronous method is intentionally a **no-op**.

        Calling code that needs to *guarantee* that all pending inserts have
        reached the database must use the **async** method :meth:`aflush`
        (or the module-level :func:`flush_all_handlers`).

        This design preserves full compatibility with the standard library
        logging machinery while still providing a clean async API for
        modern code.
        """
        # Intentionally empty — stdlib contract satisfied.
        # Real work happens in the background tasks created by emit().
        pass

    async def aflush(self) -> None:
        """
        **Async** flush — await completion of all currently pending log
        record inserts.

        This is the primary mechanism for tests and graceful shutdown code
        to guarantee that every log record emitted up to this point has
        been persisted to the database.

        Usage in tests::

            logger.info("test message", extra={"obj": {"k": 1}})
            await handler.aflush()
            rows = await query_logs(settings)
            assert len(rows) == 1

        Usage at application shutdown::

            await handler.aflush()
            await close_all_pools()

        Notes
        -----
        - The method uses :func:`asyncio.gather` with
          ``return_exceptions=True`` so that a single failing insert does
          not prevent other pending inserts from completing.
        - After the gather, any tasks that were still in ``_pending`` are
          removed by their done-callbacks; the set will be empty on return
          (barring extremely rapid re-emission).
        - Calling ``aflush`` on a handler that has never emitted anything is
          a cheap no-op.
        """
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


async def flush_all_handlers() -> None:
    """
    Flush every :class:`DBLogHandler` currently attached anywhere in the
    logging hierarchy.

    This convenience coroutine walks the standard library's logging manager
    (root logger + all named loggers) and concurrently awaits
    :meth:`DBLogHandler.aflush` on every discovered handler.  It is the
    recommended way to ensure all structured log records have been written
    to PostgreSQL before shutting down an application or finishing a test
    suite.

    The function is **idempotent** and **safe to call even when no DB
    handlers are present** — it simply becomes a no-op.

    Typical usage at shutdown
    -------------------------
    ```python
    import asyncio
    import py_pgkit as pgk
    from py_pgkit.db import close_all_pools

    async def main():
        logger = pgk.logging.getLogger(__name__)
        logger.info("Application finished", extra={"obj": {"status": "ok"}})

        await pgk.flush_all_handlers()   # guarantee persistence
        await pgk.close_all_pools()

    asyncio.run(main())
    ```

    In test teardown (pytest-asyncio style)
    ---------------------------------------
    ```python
    @pytest.fixture(scope="function")
    async def db_logger(settings):
        logger = logging.getLogger("test")
        # ... attach handler or use configure_logging ...
        yield logger
        await logging.flush_all_handlers()
    ```

    Notes
    -----
    - Only :class:`DBLogHandler` instances are flushed; regular
      :class:`logging.StreamHandler`, :class:`logging.FileHandler`, etc.
      are ignored.
    - The implementation deduplicates handlers (a single handler attached
      to multiple loggers is flushed only once).
    - Because it uses :func:`asyncio.gather(..., return_exceptions=True)`,
      a failure in one handler does not prevent the others from flushing.

    See Also
    --------
    DBLogHandler.aflush : Per-handler async flush method.
    py_pgkit.db.close_all_pools : Often called immediately after this helper.
    """
    std_logging = logging                                                                 # local alias to avoid shadowing

    handlers: set[DBLogHandler] = set()

    # 1. Root logger
    for h in std_logging.getLogger().handlers:
        if isinstance(h, DBLogHandler):
            handlers.add(h)

    # 2. All named loggers (including PlaceHolder objects that wrap loggers)
    manager = std_logging.Logger.manager
    for name, logger in list(manager.loggerDict.items()):
        if isinstance(logger, std_logging.Logger):
            for h in logger.handlers:
                if isinstance(h, DBLogHandler):
                    handlers.add(h)
        else:
            # PlaceHolder — the real logger lives in .logger
            real_logger = getattr(logger, "logger", None)
            if isinstance(real_logger, std_logging.Logger):
                for h in real_logger.handlers:
                    if isinstance(h, DBLogHandler):
                        handlers.add(h)

    if handlers:
        await asyncio.gather(
            *(h.aflush() for h in handlers),
            return_exceptions=True,
        )


def getLogger(
    name: str | PgSettings | None = None,
    conn: PgSettings | None = None,
    level: int | None = None,
    **kwargs: Any,
) -> Logger:
    """
    Smart logger factory supporting both stdlib and DB-backed logging.

    This is the central entry point for all logging in ``py-pgkit``
    applications.  It returns a normal :class:`logging.Logger` that has
    been optionally enhanced with a :class:`DBLogHandler` when a
    :class:`~py_pgkit.db.settings.PgSettings` object is supplied.

    Parameters
    ----------
    name : str or PgSettings or None, optional
        Logger name (e.g. ``__name__``) **or** a ``PgSettings`` instance.
        When a settings object is passed as the first positional argument,
        DB-backed logging is enabled automatically and ``name`` defaults
        to the root logger.
    conn : PgSettings or None, optional
        Explicit settings object (alternative syntax to passing it as the
        first argument).
    level : int or None, optional
        Logging level to set on the returned logger (e.g. ``logging.DEBUG``).
    **kwargs : Any
        Additional keyword arguments are accepted for future expansion but
        currently ignored.

    Returns
    -------
    logging.Logger
        A fully configured logger instance.  If no connection information
        was supplied, the returned object is identical to the result of
        ``logging.getLogger(name)``.  If connection information *was*
        supplied, the logger has an attached :class:`DBLogHandler` (and
        a :class:`logging.StreamHandler` for console output if none exists).

    Examples
    --------
    Pure stdlib usage (no database)::

        from py_pgkit.logging import getLogger
        logger = getLogger(__name__)
        logger.info("Hello from stdlib only")

    Recommended DB-backed pattern::

        from py_pgkit import logging as pgk_logging
        from py_pgkit.db import PgSettings

        settings = PgSettings(host="localhost", database="appdb")
        logger = pgk_logging.getLogger(__name__, conn=settings)
        logger.info("User logged in", extra={"obj": {"user_id": 42, "ip": "10.0.0.1"}})

    Pass settings as first argument (compact syntax)::

        logger = pgk_logging.getLogger(settings)
        logger.warning("Low disk space", extra={"obj": {"free_gb": 3.2}})

    See Also
    --------
    DBLogHandler : The handler class that performs the actual inserts.
    py_pgkit.configure_logging : One-shot helper that configures the root
        logger for an entire application.
    """
    # Determine if we are in DB mode
    settings: PgSettings | None = None

    if isinstance(name, PgSettings):
        settings = name
        name = None                                                                       # use root / default name
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


# Public re-exports so users can do:
#     from py_pgkit.logging import getLogger, DBLogHandler, flush_all_handlers, ...
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
