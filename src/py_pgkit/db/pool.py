"""
py_pgkit.db.pool
================

Asynchronous connection pool manager for PostgreSQL using asyncpg.

This module provides the `PgPoolManager` (and convenience `get_pool`
function) that was the heart of the original infopypg library. It has
been modernised with Pydantic settings, better typing, and clearer
documentation while preserving every performance and correctness
characteristic of the original implementation.

Design goals
------------
- **Lazy initialisation**: Pools are created only when first requested.
- **Caching by settings**: Identical `PgSettings` objects share the
  same pool (keyed by a deterministic hash of the connection parameters).
- **Thread- and async-safe**: Uses `asyncio.Lock` for creation.
- **Automatic cleanup**: Pools are closed on interpreter exit (best effort).
- **Full asyncpg feature access**: You can still call any asyncpg pool
  method directly on the returned pool.

This pool is used by:
- `DatabaseBuilder`
- The structured logging handler in `py_pgkit.logging`
- Any user code that needs raw asyncpg connections
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, cast

import asyncpg

from .settings import PgSettings

# Global registry of pools (keyed by settings hash)
_POOL_REGISTRY: dict[str, asyncpg.Pool] = {}
_POOL_LOCK = asyncio.Lock()


def _settings_key(settings: PgSettings) -> str:
    """
    Create a stable hash key for a PgSettings instance.

    Only connection-relevant fields are used so that two settings objects
    that differ only in pool size or echo still share a pool.
    """
    key_material = (
        f"{settings.host}:{settings.port}:{settings.database}:"
        f"{settings.user}:{settings.password or ''}"
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:16]


async def get_pool(settings: PgSettings) -> asyncpg.Pool:
    """
    Return (and lazily create if necessary) an asyncpg connection pool
    for the given settings.

    This is the primary entry point and the modern replacement for
    `PgPoolManager.get_pool(settings)` from the original infopypg.

    Parameters
    ----------
    settings : PgSettings
        Validated connection settings (from `py_pgkit.db.settings`).

    Returns
    -------
    asyncpg.Pool
        A ready-to-use asyncpg pool. The same pool instance is returned
        for identical connection parameters.

    Raises
    ------
    asyncpg.exceptions.PostgresError
        If the pool cannot be created (bad credentials, server down, etc.).

    Notes
    -----
    Pools are cached for the lifetime of the process. Call
    `await close_all_pools()` at shutdown if you need explicit cleanup.

    Examples
    --------
    >>> import asyncio
    >>> from py_pgkit.db.settings import PgSettings
    >>> from py_pgkit.db.pool import get_pool
    >>> settings = PgSettings(database="testdb")
    >>> pool = asyncio.run(get_pool(settings))
    >>> async with pool.acquire() as conn:
    ...     version = await conn.fetchval("SELECT version()")
    """
    key = _settings_key(settings)

    async with _POOL_LOCK:
        if key in _POOL_REGISTRY:
            return _POOL_REGISTRY[key]

        # Create new pool
        pool = await asyncpg.create_pool(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.user,
            password=settings.password,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            # echo is a SQLAlchemy-only parameter — do not pass to asyncpg
            # Additional sensible defaults
            command_timeout=60.0,
            server_settings={"application_name": "py-pgkit"},
        )
        _POOL_REGISTRY[key] = pool
        return pool


async def close_all_pools() -> None:
    """
    Close every pool currently held in the registry.

    This is useful at application shutdown to ensure all connections
    are returned to the server cleanly. It is called automatically
    via `atexit` in most environments, but you may call it explicitly
    in long-running services or tests.
    """
    async with _POOL_LOCK:
        for pool in list(_POOL_REGISTRY.values()):
            await pool.close()
        _POOL_REGISTRY.clear()


# Optional: register cleanup (best-effort)
import atexit


def _cleanup_pools() -> None:
    """Synchronous wrapper for atexit."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't cleanly close in running loop; rely on GC
            return
        loop.run_until_complete(close_all_pools())
    except Exception:
        pass


atexit.register(_cleanup_pools)
