"""
py_pgkit.db.methods
===================

Convenience methods and helpers restored from the original `infopypg`
(`loaddb.py` and `psqlhelpers.py`).

This subpackage provides high-level, async-friendly utilities for:

- **Data Loading** (`load.py`)
  - `load_table_to_memory` — fetch entire tables or filtered subsets into Python
  - `load_query_to_memory` — execute arbitrary SELECT queries and return results as list of dicts

- **Query Execution** (`query.py`)
  - `execute_query` — safe single-query execution with optional result fetching
  - `run_multi_statement_sql_script` — run migration/setup scripts containing multiple statements

- **Database Tools** (`db_tools.py`)
  - `ensure_functions_loaded` — load custom SQL functions from directories, files, or strings
  - `ensure_partition_exists` — create range partitions (especially useful for daily time-series tables)

All functions use the shared `PgPoolManager` for maximum efficiency and are
fully compatible with the `DatabaseBuilder` class.

Import Examples
---------------

>>> from py_pgkit.db.methods import (
...     load_table_to_memory,
...     load_query_to_memory,
...     execute_query,
...     run_multi_statement_sql_script,
...     ensure_functions_loaded,
...     ensure_partition_exists,
... )

>>> # Or via the parent package
>>> from py_pgkit.db import methods
>>> result = await methods.load_table_to_memory("users", settings)

>>> # Option 1 – Direct import
>>> from py_pgkit.db.methods import load_table_to_memory, ensure_partition_exists

>>> # Option 2 – Via parent package
>>> from py_pgkit.db import methods
>>> result = await methods.load_query_to_memory("SELECT * FROM users", settings)

>>> # Option 3 – Top-level (already re-exported)
>>> import py_pgkit as pgk
>>> await pgk.ensure_functions_loaded("/app/sql/functions/", settings)


Design Philosophy
-----------------
These helpers were extracted into their own module so they can be used
independently of `DatabaseBuilder`, while still being reused internally
by the builder for consistency (see `builder.py`).

All functions follow the same documentation and coding standards as the
rest of `py_pgkit`.
"""

from .db_tools import ensure_functions_loaded, ensure_partition_exists
from .load import load_query_to_memory, load_table_to_memory
from .query import execute_query, run_multi_statement_sql_script

__all__ = [
    "load_table_to_memory",
    "load_query_to_memory",
    "execute_query",
    "run_multi_statement_sql_script",
    "ensure_functions_loaded",
    "ensure_partition_exists",
]
