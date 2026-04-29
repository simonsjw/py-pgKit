"""
Pytest configuration and shared fixtures for py-pgkit tests.

Provides:
- A minimal valid PgSettings fixture (uses test database, no real connection).
- A reusable async mock pool + connection fixture for unit testing DB methods
  without requiring a live PostgreSQL instance.
- Automatic asyncio mode via pyproject.toml.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from py_pgkit.db.pool import get_pool                                                     # adjust import path as needed
from py_pgkit.db.settings import PgSettings

# Note: asyncio mode is configured via pyproject.toml [tool.pytest.ini_options]
# No anyio_backend fixture needed unless pytest-anyio is also in dev deps.


@pytest.fixture
def settings():
    """
    Minimal valid PgSettings for testing.
    Uses a non-existent 'testdb' — all DB interactions are mocked in tests.
    """

    return PgSettings(
        host="localhost",
        port=5432,
        database="testdb",
        user="testuser",
        password="testpass",
        extensions=["uuid-ossp"],
        pool_min_size=1,
        pool_max_size=2,
    )


@pytest.fixture
def mock_pool_conn():
    """
    Returns (mock_pool, mock_conn) where:
    - mock_pool.acquire() returns an async context manager that yields mock_conn
    - mock_conn has fetch, execute, copy_records_to_table, fetchval as AsyncMocks
    - Works reliably with both real asyncpg and patched get_pool.
    """
    mock_conn = AsyncMock(
        name="mock_conn", spec=["fetch", "execute", "copy_records_to_table", "fetchval"]
    )

    # Create a proper async context manager for pool.acquire()
    mock_acquire_cm = AsyncMock(name="acquire_cm")
    mock_acquire_cm.__aenter__.return_value = mock_conn
    mock_acquire_cm.__aexit__.return_value = False

    mock_pool = AsyncMock(name="mock_pool")
    mock_pool.acquire.return_value = mock_acquire_cm
    return mock_pool, mock_conn


@pytest.fixture
def patch_get_pool(monkeypatch):
    """Robust mock for asyncpg pool that works regardless of import style."""
    mock_conn = AsyncMock(name="mock_conn")
    mock_conn.execute.return_value = "OK"
    mock_conn.fetch.return_value = []
    mock_conn.copy_records_to_table.return_value = None

    # Proper async context manager for acquire()
    mock_acquire_cm = AsyncMock(name="acquire_cm")
    mock_acquire_cm.__aenter__.return_value = mock_conn
    mock_acquire_cm.__aexit__.return_value = None

    mock_pool = MagicMock(name="mock_pool")
    mock_pool.acquire.return_value = mock_acquire_cm

    # Patch in the pool module + all possible places the functions might import it from
    patches = [
        patch(
            "py_pgkit.db.pool.get_pool", new_callable=AsyncMock, return_value=mock_pool
        ),
        patch(
            "py_pgkit.db.methods.db_tools.get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ),
        patch(
            "py_pgkit.db.methods.load.get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ),
        patch(
            "py_pgkit.db.methods.query.get_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ),
    ]

    for p in patches:
        p.start()

    yield mock_pool, mock_conn

    for p in patches:
        p.stop()


# Additional fixtures for specific edge-case data
@pytest.fixture
def sample_records_dict():
    return [
        {"id": 1, "name": "Alice", "email": "alice@example.com"},
        {"id": 2, "name": "Bob", "email": "bob@example.com"},
        {"id": 3, "name": "Charlie", "email": "charlie@example.com"},
    ]


@pytest.fixture
def sample_records_tuples():
    return [
        (1, "Alice", "alice@example.com"),
        (2, "Bob", "bob@example.com"),
    ]


@pytest.fixture
def multipart_sql_script():
    """
    A realistic multi-statement SQL script with comments, SELECT, DML,
    and a RETURNING statement to exercise the parser in run_multi_statement_sql_script.
    """
    return """
    -- This is a single-line comment
    CREATE TABLE IF NOT EXISTS test_users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL
    );

    /* Block comment
       spanning multiple lines */
    INSERT INTO test_users (name) VALUES ('Test User') RETURNING id;

    -- Another comment
    SELECT * FROM test_users WHERE name = 'Test User';

    UPDATE test_users SET name = 'Updated' WHERE id = 1;
    """
