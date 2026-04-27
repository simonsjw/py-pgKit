"""
py_pgkit.db.methods.query
=========================

Query execution utilities originally from `psqlhelpers.py` in infopypg.

Provides safe, convenient wrappers for running single queries and
multi-statement SQL scripts (e.g. migrations, setup scripts, or
complex procedural SQL).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from py_pgkit.db import PgSettings, get_pool


async def execute_query(
    query: str,
    settings: PgSettings,
    params: list[Any] | None = None,
    fetch: bool = True,
) -> list[dict[str, Any]] | str | None:
    """
    Execute a single SQL query safely with parameter binding.

    Parameters
    ----------
    query : str
        SQL query to execute.
    settings : PgSettings
        Connection settings.
    params : list[Any] | None, optional
        Parameters for the query.
    fetch : bool, optional
        If True (default), returns results as list of dicts.
        If False, returns the status message (e.g. "INSERT 0 5").

    Returns
    -------
    list[dict] | str | None
        Query results or status message.

    Examples
    --------
    >>> result = await execute_query(
    ...     "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id",
    ...     settings,
    ...     ["Alice", "alice@example.com"]
    ... )
    """
    pool = await get_pool(settings)

    async with pool.acquire() as conn:
        if fetch:
            rows = await conn.fetch(query, *(params or []))
            return [dict(row) for row in rows]
        else:
            result = await conn.execute(query, *(params or []))
            return result


async def run_multi_statement_sql_script(
    script: str,
    settings: PgSettings,
    stop_on_error: bool = True,
) -> list[Any]:
    """
    Execute a multi-statement SQL script with smart handling of SELECT vs DDL/DML.

    Features:
    - Strips SQL comments (`--` single-line and `/* */` block comments)
    - Automatically uses `fetch()` for SELECT/RETURNING queries (returns list of dicts)
    - Uses `execute()` for all other statements (returns status string)
    - Robust splitting on `;` while respecting basic SQL structure

    Parameters
    ----------
    script : str
        Full SQL script (can contain multiple statements separated by `;`).
    settings : PgSettings
        Connection settings.
    stop_on_error : bool, optional
        If True (default), stops on first error.
        If False, continues and collects errors.

    Returns
    -------
    list[Any]
        Mixed list containing either:
        - list[dict] for SELECT queries
        - str (status message) for DDL/DML statements
        - Error messages (if stop_on_error=False)

    Examples
    --------
    >>> with open("migrations/001_init.sql") as f:
    ...     script = f.read()
    >>> results = await run_multi_statement_sql_script(script, settings)
    """
    import re

    pool = await get_pool(settings)
    results: list[Any] = []

    # 1. Remove block comments /* ... */
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.DOTALL)
    # 2. Remove single-line comments --
    script = re.sub(r"--.*?$", "", script, flags=re.MULTILINE)

    # 3. Split on semicolons, strip whitespace, ignore empty
    raw_statements = [s.strip() for s in script.split(";") if s.strip()]

    async with pool.acquire() as conn:
        for stmt in raw_statements:
            if not stmt:
                continue

            try:
                # Heuristic: if statement starts with SELECT or contains RETURNING,
                # use fetch
                stmt_upper = stmt.upper().lstrip()
                is_select = stmt_upper.startswith("SELECT") or "RETURNING" in stmt_upper

                if is_select:
                    rows = await conn.fetch(stmt)
                    results.append([dict(row) for row in rows])
                else:
                    result = await conn.execute(stmt)
                    results.append(result)

            except Exception as exc:
                error_msg = f"ERROR in statement: {stmt[:100]}... -> {exc}"
                results.append(error_msg)
                if stop_on_error:
                    break

    return results


async def query_logs(
    settings: PgSettings,
    level: str | None = None,
    logger_name: str | None = None,
    limit: int = 100,
    order_by: str = "tstamp DESC",
    start_time: str | None = None,
    end_time: str | None = None,
) -> list[dict[str, Any]]:
    """
    Convenience wrapper to query the structured logs table.

    This was originally exposed in the `logger` package as `query_logs`.
    It provides a simple, safe way to retrieve recent log entries with
    optional filtering.

    Parameters
    ----------
    settings : PgSettings
        Connection settings (must point to the database containing the logs table).
    level : str | None, optional
        Filter by log level (e.g. "INFO", "ERROR").
    logger_name : str | None, optional
        Filter by logger name (supports partial match with LIKE).
    limit : int, optional
        Maximum number of rows to return (default 100).
    order_by : str, optional
        ORDER BY clause (default "tstamp DESC").
    start_time : str | None, optional
        Filter logs after this timestamp (ISO format recommended).
    end_time : str | None, optional
        Filter logs before this timestamp (ISO format recommended).

    Returns
    -------
    list[dict[str, Any]]
        List of log records as dictionaries.

    Examples
    --------
    >>> recent_errors = await query_logs(settings, level="ERROR", limit=50)
    >>> app_logs = await query_logs(settings, logger_name="myapp", start_time="2026-04-01")
    """
    pool = await get_pool(settings)

    conditions = []
    params: list[Any] = []

    if level:
        conditions.append("loglvl = $1")
        params.append(level.upper())
    if logger_name:
        conditions.append(f"logger LIKE ${len(params) + 1}")
        params.append(f"%{logger_name}%")
    if start_time:
        conditions.append(f"tstamp >= ${len(params) + 1}")
        params.append(start_time)
    if end_time:
        conditions.append(f"tstamp <= ${len(params) + 1}")
        params.append(end_time)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    query = f"""
        SELECT idx, tstamp, loglvl, logger, message, obj
        FROM logs
        WHERE {where_clause}
        ORDER BY {order_by}
        LIMIT {limit}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]
