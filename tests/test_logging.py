"""
Tests for py_pgkit.logging (getLogger, DBLogHandler integration)

These are mostly unit tests around handler attachment and fallback behavior.
"""

import logging as std_logging
import pytest
from unittest.mock import patch, AsyncMock

import py_pgkit as pgk
from py_pgkit.logging.core import DBLogHandler, getLogger


def test_get_logger_stdlib_fallback(settings):
    """Without conn/settings, behaves exactly like stdlib logging."""
    logger = getLogger("test.std")
    assert isinstance(logger, std_logging.Logger)
    assert logger.name == "test.std"


def test_get_logger_with_db_handler(settings):
    """When PgSettings passed, a DBLogHandler should be attached (idempotent)."""
    logger = getLogger("test.db", conn=settings)
    handlers = [h for h in logger.handlers if isinstance(h, DBLogHandler)]
    assert len(handlers) == 1

    # Calling again should not duplicate
    logger2 = getLogger("test.db", conn=settings)
    handlers2 = [h for h in logger2.handlers if isinstance(h, DBLogHandler)]
    assert len(handlers2) == 1


@pytest.mark.asyncio
async def test_db_log_handler_emit(settings):
    """DBLogHandler.emit schedules async write (best-effort, never raises)."""
    handler = DBLogHandler(settings)
    record = std_logging.LogRecord(
        name="test", level=20, pathname="", lineno=0,
        msg="Hello %s", args=("world",), exc_info=None
    )
    record.obj = {"user": 42}  # extra for JSONB

    with patch("py_pgkit.logging.core.asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value = AsyncMock()
        # Should not raise even if emit fails internally
        handler.emit(record)

    # In real use it would call _emit_async which uses pool


def test_configure_logging(settings):
    """The top-level configure_logging helper sets up DB backend."""
    pgk.configure_logging(settings)
    root = std_logging.getLogger()
    # Strict: must attach DB handler (if this fails, configure_logging or DBLogHandler
    # attachment logic is broken / not aligned with best practice for drop-in logging)
    assert any(isinstance(h, DBLogHandler) for h in root.handlers), \
        "configure_logging did not attach DBLogHandler to root logger"
