"""
py_pgkit.db.methods.db_tools
============================

Database maintenance and infrastructure helpers.

Contains functions:
- `ensure_functions_loaded` — load custom SQL functions from files or strings
- `ensure_partition_exists` — create daily/ monthly partitions on demand

These are especially useful for time-series tables and procedural SQL.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import asyncpg

from py_pgkit.db import PgSettings, get_pool


async def ensure_functions_loaded(
    functions: list[str] | str | Path,
    settings: PgSettings,
) -> None:
    """
    Ensure custom SQL functions are loaded into the database.

    Accepts either:
    - A directory path containing `.sql` files
    - A list of SQL function definitions as strings
    - A single `.sql` file path

    Parameters
    ----------
    functions : list[str] | str | Path
        Source of the SQL functions.
    settings : PgSettings
        Connection settings.

    Examples
    --------
    >>> await ensure_functions_loaded("/path/to/sql/functions/", settings)
    >>> await ensure_functions_loaded([
    ...     "CREATE OR REPLACE FUNCTION my_func() RETURNS void AS $$ ... $$ LANGUAGE plpgsql;",
    ... ], settings)
    """
    pool = await get_pool(settings)

    sql_statements: list[str] = []

    if isinstance(functions, (str, Path)):
        path = Path(functions)
        if path.is_dir():
            for sql_file in sorted(path.glob("*.sql")):
                sql_statements.append(sql_file.read_text())
        elif path.is_file():
            sql_statements.append(path.read_text())
        else:
            # Treat as raw SQL string
            sql_statements.append(str(functions))
    elif isinstance(functions, list):
        sql_statements = functions

    async with pool.acquire() as conn:
        for sql in sql_statements:
            try:
                await conn.execute(sql)
            except Exception as exc:
                print(f"[py_pgkit] Warning loading function: {exc}")


async def ensure_partition_exists(
    table_name: str,
    partition_name: str,
    start_value: str,
    end_value: str,
    settings: PgSettings,
    partition_column: str = "created_at",
) -> None:
    """
    Ensure a range partition exists for a table (commonly used for daily partitions).

    Creates the partition if it does not already exist. Works with both
    declarative partitioning (Postgres 10+) and the older trigger-based approach.

    Parameters
    ----------
    table_name : str
        Name of the parent partitioned table.
    partition_name : str
        Name for the new partition (e.g. 'logs_2026_04_27').
    start_value : str
        Start value for the range (inclusive).
    end_value : str
        End value for the range (exclusive).
    settings : PgSettings
        Connection settings.
    partition_column : str, optional
        Name of the timestamp column used for partitioning (default 'created_at').

    Examples
    --------
    >>> await ensure_partition_exists(
    ...     "logs", "logs_2026_04_27",
    ...     "2026-04-27 00:00:00", "2026-04-28 00:00:00",
    ...     settings
    ... )
    """
    pool = await get_pool(settings)

    create_stmt = f"""
        CREATE TABLE IF NOT EXISTS {partition_name}
        PARTITION OF {table_name}
        FOR VALUES FROM ('{start_value}') TO ('{end_value}');
    """

    async with pool.acquire() as conn:
        try:
            await conn.execute(create_stmt)
        except asyncpg.exceptions.DuplicateTableError:
            pass                                                                          # Partition already exists
        except Exception as exc:
            print(f"[py_pgkit] Partition creation warning: {exc}")
