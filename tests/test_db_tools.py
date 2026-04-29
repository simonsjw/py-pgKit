"""
Tests for py_pgkit.db.methods.db_tools (ensure_functions_loaded, ensure_partition_exists)
"""

from unittest.mock import patch

import pytest

from py_pgkit.db.methods.db_tools import (
    ensure_functions_loaded,
    ensure_partition_exists,
)


@pytest.mark.asyncio
async def test_ensure_functions_loaded_from_list(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.return_value = "CREATE FUNCTION"

    funcs = [
        "CREATE OR REPLACE FUNCTION hello() RETURNS void AS $$ BEGIN END; $$ LANGUAGE plpgsql;",
        "CREATE OR REPLACE FUNCTION world() RETURNS int AS $$ SELECT 42; $$ LANGUAGE sql;",
    ]
    await ensure_functions_loaded(funcs, settings)

    print("DEBUG - execute calls:", mock_conn.execute.call_count)                         # remove later
    assert mock_conn.execute.call_count == 2


@pytest.mark.asyncio
async def test_ensure_functions_loaded_from_string(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.return_value = "OK"

    sql = "CREATE FUNCTION test() RETURNS void AS $$ $$ LANGUAGE plpgsql;"
    await ensure_functions_loaded(sql, settings)
    mock_conn.execute.assert_awaited_once_with(sql)


@pytest.mark.asyncio
async def test_ensure_partition_exists(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.return_value = "CREATE TABLE"

    await ensure_partition_exists(
        "events", "events_2026_04_28", "2026-04-28", "2026-04-29", settings
    )
    mock_conn.execute.assert_awaited()
    stmt = mock_conn.execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS events_2026_04_28" in stmt
    assert "PARTITION OF events" in stmt


@pytest.mark.asyncio
async def test_ensure_partition_exists_duplicate_ignored(settings, patch_get_pool):
    from asyncpg.exceptions import DuplicateTableError

    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.side_effect = DuplicateTableError("already exists")

    # Should not raise
    await ensure_partition_exists(
        "logs", "logs_2026_04_27", "2026-04-27", "2026-04-28", settings
    )
