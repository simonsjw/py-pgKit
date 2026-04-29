"""
Tests for py_pgkit.db.methods.load (load_table_to_memory, load_query_to_memory, bulk_insert)

Covers:
- Basic happy paths with mocked pool
- bulk_insert: chunking / batch_size variations (the 'troublesome' part)
- Edge cases: empty input, dict vs tuple records, missing columns, errors
"""

from unittest.mock import AsyncMock, patch

import pytest

from py_pgkit.db.methods.load import (
    _chunked,
    bulk_insert,
    load_query_to_memory,
    load_table_to_memory,
)


def test_chunked_helper():
    data = list(range(10))
    chunks = list(_chunked(data, 3))
    assert chunks == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]
    assert list(_chunked([], 5)) == []
    assert list(_chunked([1], 100)) == [[1]]


@pytest.mark.asyncio
async def test_load_table_to_memory_basic(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"},
    ]

    result = await load_table_to_memory(
        "users", settings, limit=10, where_clause="active = $1", params=[True]
    )

    mock_conn.fetch.assert_awaited_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "SELECT * FROM users" in query
    assert "WHERE active = $1" in query
    assert "LIMIT 10" in query
    assert result == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]


@pytest.mark.asyncio
async def test_load_query_to_memory(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"count": 42}]

    result = await load_query_to_memory(
        "SELECT COUNT(*) as count FROM orders", settings
    )
    assert result == [{"count": 42}]


# ====================== BULK INSERT TESTS (key focus) ======================


@pytest.mark.asyncio
async def test_bulk_insert_empty(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    count = await bulk_insert("users", [], settings)
    assert count == 0
    mock_conn.copy_records_to_table.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_insert_dicts_default_chunk(
    settings, sample_records_dict, patch_get_pool
):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.copy_records_to_table.return_value = None                                   # success

    inserted = await bulk_insert("users", sample_records_dict, settings)
    assert inserted == 3
    # Default batch_size=1000 → one call
    mock_conn.copy_records_to_table.assert_awaited_once()
    args, kwargs = mock_conn.copy_records_to_table.call_args
    # table_name can be positional (args[0]) or keyword
    table_name_arg = args[0] if args else kwargs.get("table_name")
    assert table_name_arg == "users"
    assert len(kwargs["records"]) == 3
    assert kwargs["columns"] == ["id", "name", "email"]


@pytest.mark.asyncio
async def test_bulk_insert_different_chunk_sizes(
    settings, sample_records_dict, patch_get_pool
):
    """Test the 'troublesome edge cases' for differing chunk / batch_size values."""
    mock_pool, mock_conn = patch_get_pool
    mock_conn.copy_records_to_table.return_value = None

    # Small chunk size (forces multiple batches)
    inserted = await bulk_insert("users", sample_records_dict, settings, batch_size=2)
    assert inserted == 3
    assert mock_conn.copy_records_to_table.call_count == 2                                # 2 + 1

    mock_conn.copy_records_to_table.reset_mock()

    # Large chunk size (single batch even for bigger data)
    big_data = sample_records_dict * 10                                                   # 30 rows
    inserted = await bulk_insert("users", big_data, settings, batch_size=10000)
    assert inserted == 30
    assert mock_conn.copy_records_to_table.call_count == 1


@pytest.mark.asyncio
async def test_bulk_insert_tuples_requires_columns(
    settings, sample_records_tuples, patch_get_pool
):
    mock_pool, mock_conn = patch_get_pool

    with pytest.raises(ValueError, match="columns parameter is required"):
        await bulk_insert("users", sample_records_tuples, settings)

    # With columns
    inserted = await bulk_insert(
        "users",
        sample_records_tuples,
        settings,
        columns=["id", "name", "email"],
        batch_size=1,
    )
    assert inserted == 2
    assert mock_conn.copy_records_to_table.call_count == 2


@pytest.mark.asyncio
async def test_bulk_insert_error_propagates(
    settings, sample_records_dict, patch_get_pool
):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.copy_records_to_table.side_effect = Exception("connection lost")

    with pytest.raises(Exception, match="connection lost"):
        await bulk_insert("users", sample_records_dict, settings, batch_size=1)


@pytest.mark.asyncio
async def test_bulk_insert_explicit_columns_on_dicts_reorders_and_subsets(
    settings, sample_records_dict, patch_get_pool
):
    """Edge case: when columns= is provided for list[dict], it should use exactly
    those columns (in that order) for the COPY, allowing reordering or projection.
    This exercises the 'columns is not None' path even for dict input.
    """
    mock_pool, mock_conn = patch_get_pool
    mock_conn.copy_records_to_table.return_value = None

    # Request only a subset, in different order
    inserted = await bulk_insert(
        "users",
        sample_records_dict,
        settings,
        columns=["email", "name"],                                                        # subset + reorder
        batch_size=10,
    )
    assert inserted == 3
    mock_conn.copy_records_to_table.assert_awaited_once()
    kwargs = mock_conn.copy_records_to_table.call_args.kwargs
    assert kwargs["columns"] == ["email", "name"]
    # First record should have been converted using only those columns
    assert kwargs["records"][0] == ("alice@example.com", "Alice")
