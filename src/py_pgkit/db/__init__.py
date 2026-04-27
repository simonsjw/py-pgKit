"""
py_pgkit.db
===========

Database layer for py-pgkit.

This package re-exports the core components that were previously
spread across infopypg:

- `PgSettings` — modern Pydantic settings (replaces ResolvedSettingsDict)
- `get_pool` / `PgPoolManager` — shared asyncpg connection pools
- `DatabaseBuilder` — full incremental infrastructure builder

All functionality from the original infopypg is preserved (and improved)
while dramatically reducing the amount of boilerplate required by users.
"""

from .builder import DatabaseBuilder
from .pool import close_all_pools, get_pool
from .settings import PgSettings

__all__ = [
    "PgSettings",
    "get_pool",
    "close_all_pools",
    "DatabaseBuilder",
]
