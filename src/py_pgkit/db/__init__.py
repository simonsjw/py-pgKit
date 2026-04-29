"""
py_pgkit.db
===========

Database layer for py-pgkit.

This package re-exports the core component:

- `get_pool`   — shared asyncpg connection pools
- `DatabaseBuilder` — full incremental infrastructure builder


Examples
--------
>>> import py_pgkit as pgk
>>> from py_pgkit.db.settings import PgSettings

>>> settings = PgSettings(database="analytics")

>>> # Load data
>>> users = await pgk.load_table_to_memory("users", settings, limit=500)
>>> results = await pgk.load_query_to_memory(
...    "SELECT * FROM orders WHERE status = $1", settings, ["shipped"])

>>> # Execute queries
>>> await pgk.execute_query(
...    "UPDATE users SET last_login = now() WHERE id = $1", settings, [42], fetch=False)

>>> # Run migration script
>>> with open("migrations/002_add_indexes.sql") as f:
>>>     await pgk.run_multi_statement_sql_script(f.read(), settings)

>>> # Ensure functions & partitions
>>> await pgk.ensure_functions_loaded("/app/sql/functions/", settings)
>>> await pgk.ensure_partition_exists("logs", "logs_2026_04_28", "2026-04-28",
...    "2026-04-29", settings)
"""

from .builder import DatabaseBuilder
from .methods.db_tools import ensure_functions_loaded, ensure_partition_exists

# methods module:
from .methods.load import bulk_insert, load_query_to_memory, load_table_to_memory
from .methods.query import execute_query, query_logs, run_multi_statement_sql_script
from .pool import close_all_pools, get_pool
from .settings import PgSettings

__all__ = [
    "bulk_insert",
    "query_logs",
    "PgSettings",
    "get_pool",
    "close_all_pools",
    "DatabaseBuilder",
    "load_table_to_memory",
    "load_query_to_memory",
    "execute_query",
    "run_multi_statement_sql_script",
    "ensure_functions_loaded",
    "ensure_partition_exists",
]
