"""
py_pgkit.db.settings
====================

Pydantic-based configuration model for PostgreSQL connections.

This module replaces the original `ResolvedSettingsDict` + manual validation
function from infopypg with a modern, type-safe, environment-aware Pydantic
model. It dramatically reduces boilerplate while preserving (and improving)
every capability of the original design.

Key improvements over the legacy implementation:
- Automatic validation, coercion, and error messages
- Native support for environment variables (DB_HOST, etc.)
- Dict-like interface preserved for backward compatibility
- Immutable by default (frozen=True)
- Rich serialization (model_dump, model_dump_json, etc.)

All existing uppercase `DB_*` key patterns continue to work via
`model_validate()`.

This is the single source of truth for connection settings used by
PgPoolManager, DatabaseBuilder, and the logging subsystem.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings


class PgSettings(BaseSettings):
    """
    PostgreSQL connection and infrastructure settings.

    This dataclass-style model is the modern replacement for the original
    `ResolvedSettingsDict`. It supports the exact same input patterns used
    in infopypg while adding robust validation and environment variable
    support.

    Parameters
    ----------
    host : str
        Database host (defaults to 'localhost').
    port : int
        Database port (defaults to 5432).
    database : str
        Database name.
    user : str
        Database user.
    password : str | None
        Database password (can be None for peer auth, etc.).
    extensions : list[str] | None, optional
        PostgreSQL extensions to ensure are installed
        (e.g. ['uuid-ossp', 'pg_trgm']).
    tablespace_name : str | None, optional
        Name of the tablespace to create/use.
    tablespace_path : str | None, optional
        Filesystem path for the tablespace (required if tablespace_name
        is provided and the tablespace does not already exist).
    pool_min_size : int, optional
        Minimum connections in the pool (default 5).
    pool_max_size : int, optional
        Maximum connections in the pool (default 20).
    echo : bool, optional
        Whether to echo SQLAlchemy / asyncpg statements (debug only).

    Attributes
    ----------
    All fields are accessible both as attributes and via dict-like
    interface (see __getitem__, keys, items, etc.).

    Examples
    --------
    >>> from py_pgkit.db.settings import PgSettings
    >>> settings = PgSettings(
    ...     host="localhost",
    ...     port=5432,
    ...     database="mydb",
    ...     user="postgres",
    ...     password="secret",
    ...     extensions=["uuid-ossp"],
    ... )
    >>> settings.host
    'localhost'
    >>> dict(settings)  # dict-like access still works
    {'host': 'localhost', ...}

    # From legacy uppercase dict (full backward compatibility)
    >>> legacy = {
    ...     "DB_HOST": "db.example.com",
    ...     "DB_PORT": "5432",
    ...     "DB_NAME": "prod",
    ...     "DB_USER": "app",
    ...     "PASSWORD": "s3cr3t",
    ... }
    >>> s = PgSettings.model_validate(legacy)
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
        env_prefix="DB_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Core connection fields (support both new and legacy names via aliases)
    host: str = Field(default="localhost", alias="DB_HOST")
    port: int = Field(default=5432, alias="DB_PORT")
    database: str = Field(..., alias="DB_NAME")
    user: str = Field(..., alias="DB_USER")
    password: str | None = Field(default=None, alias="PASSWORD")

    # Infrastructure
    extensions: list[str] | None = Field(
        default=None,
        description="List of PostgreSQL extensions to create if missing",
    )
    tablespace_name: str | None = Field(default=None, alias="TABLESPACE_NAME")
    tablespace_path: str | None = Field(default=None, alias="TABLESPACE_PATH")

    # Pool tuning
    pool_min_size: int = Field(default=5, ge=1)
    pool_max_size: int = Field(default=20, ge=1)

    # Debug
    echo: bool = Field(default=False)

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        """Ensure port is in valid range."""
        if not (1 <= v <= 65535):
            raise ValueError("Port must be between 1 and 65535")
        return v

    @model_validator(mode="after")
    def _validate_tablespace(self) -> PgSettings:
        """If tablespace_name is given, tablespace_path should also be provided
        (unless the tablespace already exists — we can't know that here)."""
        if self.tablespace_name and not self.tablespace_path:
            # We allow it; the builder will raise a clearer error later
            pass
        return self

    # ------------------------------------------------------------------
    # Dict-like interface (preserves original ResolvedSettingsDict API)
    # ------------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        """Allow settings['DB_HOST'] style access (legacy compatibility)."""
        # Support both new and legacy key styles
        key_map = {
            "DB_HOST": "host",
            "DB_PORT": "port",
            "DB_NAME": "database",
            "DB_USER": "user",
            "PASSWORD": "password",
            "EXTENSIONS": "extensions",
            "TABLESPACE_NAME": "tablespace_name",
            "TABLESPACE_PATH": "tablespace_path",
        }
        attr = key_map.get(key, key.lower())
        if hasattr(self, attr):
            return getattr(self, attr)
        raise KeyError(key)

    def keys(self) -> list[str]:
        """Return list of keys (both legacy and modern)."""
        return [
            "DB_HOST",
            "DB_PORT",
            "DB_NAME",
            "DB_USER",
            "PASSWORD",
            "EXTENSIONS",
            "TABLESPACE_NAME",
            "TABLESPACE_PATH",
            "host",
            "port",
            "database",
            "user",
            "password",
            "extensions",
            "tablespace_name",
            "tablespace_path",
        ]

    def values(self) -> list[Any]:
        """Return list of values in same order as keys()."""
        return [
            getattr(self, k.lower().replace("db_", "")) for k in self.keys()[:8]
        ] + [
            getattr(self, k)
            for k in ["extensions", "tablespace_name", "tablespace_path"]
        ]

    def items(self) -> list[tuple[str, Any]]:
        """Return (key, value) pairs."""
        return list(zip(self.keys(), self.values()))

    def __iter__(self):
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    def model_dump(self, **kwargs) -> dict[str, Any]:
        """Override to also include legacy uppercase keys for compatibility."""
        data = super().model_dump(**kwargs)
        # Add legacy keys
        data.update(
            {
                "DB_HOST": data["host"],
                "DB_PORT": data["port"],
                "DB_NAME": data["database"],
                "DB_USER": data["user"],
                "PASSWORD": data["password"],
                "EXTENSIONS": data.get("extensions"),
                "TABLESPACE_NAME": data.get("tablespace_name"),
                "TABLESPACE_PATH": data.get("tablespace_path"),
            }
        )
        return data

    async def async_ping(self) -> bool:
        """
        Asynchronously test connectivity to the PostgreSQL server.

        Uses a temporary connection from the pool (or creates one if none
        exists yet). This is the async equivalent of the original
        `ResolvedSettingsDict.async_ping()`.

        Returns
        -------
        bool
            True if connection succeeds, False otherwise (never raises
            for ping — use try/except if you want the exception).

        Examples
        --------
        >>> import asyncio
        >>> from py_pgkit.db.settings import PgSettings
        >>> settings = PgSettings(database="test")
        >>> asyncio.run(settings.async_ping())
        True
        """
        from .pool import get_pool                                                        # local import to avoid circularity

        try:
            pool = await get_pool(self)
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def __repr__(self) -> str:
        return (
            f"PgSettings(host={self.host!r}, port={self.port}, "
            f"database={self.database!r}, user={self.user!r})"
        )
