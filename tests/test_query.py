"""
Tests for py_pgkit.db.methods.query (execute_query, run_multi_statement_sql_script, query_logs)

Focus: Troublesome edge cases for multipart SQL scripts in run_multi_statement_sql_script
- Comment stripping (-- and /* */)
- Statement splitting on ;
- SELECT vs non-SELECT (incl. RETURNING)
- stop_on_error behavior
- Error collection and partial results
"""

import pytest
from unittest.mock import patch

from py_pgkit.db.methods.query import (
    execute_query,
    run_multi_statement_sql_script,
    query_logs,
)


@pytest.mark.asyncio
async def test_execute_query_fetch(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"id": 1}]

    result = await execute_query("SELECT 1 as id", settings, fetch=True)
    assert result == [{"id": 1}]
    mock_conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_query_execute(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.return_value = "INSERT 0 1"

    result = await execute_query("INSERT INTO t VALUES (1)", settings, fetch=False)
    assert result == "INSERT 0 1"
    mock_conn.execute.assert_awaited_once()


# ====================== MULTIPART SQL EDGE CASES ======================

@pytest.mark.asyncio
async def test_run_multi_statement_basic(settings, patch_get_pool, multipart_sql_script):
    mock_pool, mock_conn = patch_get_pool
    # Simulate realistic responses
    mock_conn.fetch.side_effect = [
        [{"id": 1}],           # for the RETURNING INSERT
        [{"id": 1, "name": "Test User"}],  # SELECT
    ]
    mock_conn.execute.side_effect = [
        "CREATE TABLE",       # CREATE
        "UPDATE 1",           # UPDATE
    ]

    results = await run_multi_statement_sql_script(multipart_sql_script, settings)

    assert len(results) == 4
    assert results[0] == "CREATE TABLE"          # DDL
    assert results[1] == [{"id": 1}]             # RETURNING → fetch
    assert results[2] == [{"id": 1, "name": "Test User"}]  # SELECT
    assert results[3] == "UPDATE 1"              # DML


@pytest.mark.asyncio
async def test_run_multi_statement_comment_stripping(settings, patch_get_pool):
    """Test that -- and /* */ comments are correctly removed before splitting."""
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.return_value = "OK"

    script = """
    -- header comment
    CREATE TABLE t (id int);  /* inline block */
    /* multi
       line
       block */ INSERT INTO t VALUES (1);
    -- trailing
    """
    results = await run_multi_statement_sql_script(script, settings, stop_on_error=True)
    assert len(results) == 2
    assert all(r == "OK" for r in results)
    # Stricter: verify actual statements executed had comments stripped
    executed_stmts = [call[0][0] for call in mock_conn.execute.call_args_list]
    assert "CREATE TABLE t (id int)" in executed_stmts[0]
    assert "INSERT INTO t VALUES (1)" in executed_stmts[1]


@pytest.mark.asyncio
async def test_run_multi_statement_stop_on_error(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    # Script starts with non-SELECT so we can use execute side_effect only
    mock_conn.execute.side_effect = [
        "OK1",
        Exception("syntax error at statement 2"),
        "OK3",  # should not be reached
    ]

    script = "CREATE TABLE t; BAD SQL; CREATE TABLE t2;"
    results = await run_multi_statement_sql_script(script, settings, stop_on_error=True)

    assert len(results) == 2
    assert results[0] == "OK1"
    assert "ERROR in statement: BAD SQL" in str(results[1])
    # Verify it stopped early (did not process third statement)
    assert mock_conn.execute.call_count == 2


@pytest.mark.asyncio
async def test_run_multi_statement_continue_on_error(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.side_effect = [
        "OK1",
        Exception("bad stmt"),
        "OK3",
    ]
    mock_conn.fetch.return_value = []

    script = "CREATE 1; BAD; CREATE 3;"
    results = await run_multi_statement_sql_script(script, settings, stop_on_error=False)

    assert len(results) == 3
    assert results[0] == "OK1"
    assert "ERROR in statement: BAD" in results[1]
    assert results[2] == "OK3"


@pytest.mark.asyncio
async def test_run_multi_statement_empty_and_whitespace(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool

    results = await run_multi_statement_sql_script("   ; ; -- only comments\n  ", settings)
    assert results == []

    results2 = await run_multi_statement_sql_script("", settings)
    assert results2 == []


@pytest.mark.asyncio
async def test_query_logs_basic(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"idx": 1, "message": "hello"}]

    logs = await query_logs(settings, level="ERROR", limit=5)
    assert len(logs) == 1
    # Check query construction contains WHERE
    called_query = mock_conn.fetch.call_args[0][0]
    assert "loglvl = $1" in called_query
    assert "LIMIT 5" in called_query


def test_public_api_exports():
    """Verify that all functions advertised in the top-level __init__.py and README
    are actually importable. This test will fail until the missing re-exports in
    src/py_pgkit/db/__init__.py are fixed (bulk_insert and query_logs are imported
    from .db in package __init__ but not defined/exported in db/__init__.py).

    This is best-practice API surface testing and prevents "it works if you import
    the submodule directly" bugs.
    """
    import py_pgkit as pgk

    # These should not raise ImportError
    assert hasattr(pgk, "bulk_insert") and callable(pgk.bulk_insert)
    assert hasattr(pgk, "query_logs") and callable(pgk.query_logs)

    # Also test direct from db (should work after fix)
    from py_pgkit.db import bulk_insert, query_logs
    assert callable(bulk_insert)
    assert callable(query_logs)


@pytest.mark.asyncio
async def test_run_multi_statement_malformed_comments_and_heuristic_edge_cases(
    settings, patch_get_pool
):
    """Edge cases for the naive comment stripper and SELECT/RETURNING heuristic:
    - Unclosed block comment (should not crash the whole script)
    - RETURNING appearing in a column alias (heuristic will still treat as fetch)
    - Statement with ; inside a string literal (naive splitter will break it - documents limitation)
    """
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"id": 99}]
    mock_conn.execute.return_value = "OK"

    # Unclosed block comment + RETURNING in alias (not real RETURNING clause)
    script = """
    /* unclosed comment without end
    SELECT id AS "has RETURNING in name" FROM t;
    INSERT INTO t (name) VALUES ('foo;bar');  -- ; inside string
    """
    results = await run_multi_statement_sql_script(script, settings, stop_on_error=False)

    # Should still produce results for the parsable parts; the unclosed comment
    # leaves garbage but the regex is non-greedy so it may leave the rest.
    # We assert it didn't raise and produced at least one result.
    assert len(results) >= 1
    # The heuristic treated the alias one as SELECT (fetch called)
    assert mock_conn.fetch.called
