"""
py_pgkit.db.methods.load
========================

High-performance data loading utilities for PostgreSQL.

These functions provide convenient ways to pull data from the database
into Python memory (list of dictionaries) for further processing,
analysis, or caching. They were originally part of `loaddb.py` in
infopypg and have been modernised with Pydantic settings and the
shared connection pool.

All functions are fully asynchronous and use the global pool for
maximum efficiency.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from py_pgkit.db import PgSettings, get_pool


async def load_table_to_memory(
    table_name: str,
    settings: PgSettings,
    columns: list[str] | None = None,
    limit: int | None = None,
    where_clause: str | None = None,
    params: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Load an entire table (or subset) into memory as a list of dictionaries.

    Parameters
    ----------
    table_name : str
        Name of the table to load (schema-qualified if needed, e.g. 'public.users').
    settings : PgSettings
        Connection settings (will use the shared pool).
    columns : list[str] | None, optional
        Specific columns to select. If None, selects all columns (`*`).
    limit : int | None, optional
        Maximum number of rows to return.
    where_clause : str | None, optional
        Raw WHERE clause (without the word 'WHERE').
    params : list[Any] | None, optional
        Parameters for the where_clause (to prevent SQL injection).

    Returns
    -------
    list[dict[str, Any]]
        List of row dictionaries. Each dict has column names as keys.

    Examples
    --------
    >>> import asyncio
    >>> from py_pgkit.db import PgSettings
    >>> from py_pgkit.db.methods.load import load_table_to_memory
    >>> settings = PgSettings(database="analytics")
    >>> users = asyncio.run(load_table_to_memory("users", settings, limit=100))
    >>> print(len(users))
    100
    """
    pool = await get_pool(settings)

    col_str = ", ".join(columns) if columns else "*"
    query = f"SELECT {col_str} FROM {table_name}"

    if where_clause:
        query += f" WHERE {where_clause}"
    if limit:
        query += f" LIMIT {limit}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *(params or []))
        return [dict(row) for row in rows]


async def load_query_to_memory(
    query: str,
    settings: PgSettings,
    params: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Execute a custom SELECT query and return the full result set in memory.

    This is the more flexible version of `load_table_to_memory` when you
    need joins, aggregations, or complex filters.

    Parameters
    ----------
    query : str
        Complete SELECT query (must start with SELECT).
    settings : PgSettings
        Connection settings.
    params : list[Any] | None, optional
        Query parameters for safe interpolation.

    Returns
    -------
    list[dict[str, Any]]
        List of dictionaries, one per row.

    Examples
    --------
    >>> query = "SELECT u.name, COUNT(o.id) as order_count FROM users u LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.name"
    >>> result = await load_query_to_memory(query, settings)
    """
    pool = await get_pool(settings)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *(params or []))
        return [dict(row) for row in rows]
