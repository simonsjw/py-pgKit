"""
Pytest configuration and shared fixtures for py-pgkit tests.

Provides:
- A minimal valid PgSettings fixture (uses test database, no real connection).
- A reusable async mock pool + connection fixture for unit testing DB methods
  without requiring a live PostgreSQL instance.
- Automatic asyncio mode via pyproject.toml.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from py_pgkit.db.settings import PgSettings


# Note: asyncio mode is configured via pyproject.toml [tool.pytest.ini_options]
# No anyio_backend fixture needed unless pytest-anyio is also in dev deps.


@pytest.fixture
def settings() -> PgSettings:
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
    - mock_pool.acquire() is an async context manager returning mock_conn
    - mock_conn has fetch, execute, copy_records_to_table, fetchval as AsyncMocks
    - Common setup for testing load/query/bulk/ensure functions.
    """
    mock_conn = AsyncMock(spec=["fetch", "execute", "copy_records_to_table", "fetchval"])
    mock_pool = AsyncMock()
    # Make acquire() return an async context manager
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = False
    return mock_pool, mock_conn


@pytest.fixture
def patch_get_pool(mock_pool_conn):
    """
    Pytest fixture that temporarily patches get_pool() in all relevant submodules
    so that DB-dependent functions use the provided mock pool/connection instead
    of trying to connect to a real database.

    Usage:
        def test_something(settings, patch_get_pool):
            mock_pool, mock_conn = patch_get_pool
            ...
    """
    mock_pool, mock_conn = mock_pool_conn
    with patch("py_pgkit.db.get_pool", return_value=mock_pool), \
         patch("py_pgkit.db.methods.load.get_pool", return_value=mock_pool), \
         patch("py_pgkit.db.methods.query.get_pool", return_value=mock_pool), \
         patch("py_pgkit.db.methods.db_tools.get_pool", return_value=mock_pool), \
         patch("py_pgkit.db.pool.get_pool", return_value=mock_pool):
        yield mock_pool, mock_conn


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