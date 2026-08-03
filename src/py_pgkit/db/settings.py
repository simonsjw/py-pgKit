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
- Optional bootstrap (privileged) credentials for CREATE DATABASE /
  CREATE EXTENSION while ordinary runtime credentials remain least-privilege

All existing uppercase `DB_*` key patterns continue to work via
`model_validate()`.

This is the single source of truth for connection settings used by
PgPoolManager, DatabaseBuilder, and the logging subsystem.
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings


class PgSettings(BaseSettings):
    """PostgreSQL connection and infrastructure settings.

    This dataclass-style model is the modern replacement for the original
    `ResolvedSettingsDict`. It supports the exact same input patterns used
    in infopypg while adding robust validation and environment variable
    support.

    Parameters
    ----------
    host : str
        Database host (defaults to ``'localhost'``).
    port : int
        Database port (defaults to 5432).
    database : str
        Database name.
    user : str
        Ordinary (runtime) database role.  Used for table creation, triggers,
        and day-to-day connections.
    password : str or None
        Password for ``user`` (may be ``None`` for peer authentication).
    bootstrap_user : str or None, optional
        Privileged role used **only** for administrative steps
        (``CREATE DATABASE``, ``CREATE TABLESPACE``, ``CREATE EXTENSION``).
        When supplied, ``DatabaseBuilder`` connects with this identity for
        those steps while continuing to use ``user`` / ``password`` for
        everything else.  Must be paired with ``bootstrap_password``.
    bootstrap_password : str or None, optional
        Password for ``bootstrap_user``.  Must be supplied together with
        ``bootstrap_user`` (both present or both omitted).
    extensions : list of str or None, optional
        PostgreSQL extensions to ensure are installed
        (e.g. ``['uuid-ossp', 'pg_trgm']``).
    tablespace_name : str or None, optional
        Name of the tablespace to create/use.
    tablespace_path : str or None, optional
        Filesystem path for the tablespace (required if ``tablespace_name``
        is provided and the tablespace does not already exist).
    pool_min_size : int, optional
        Minimum connections in the pool (default 5).
    pool_max_size : int, optional
        Maximum connections in the pool (default 20).
    echo : bool, optional
        Whether to echo SQLAlchemy / asyncpg statements (debug only).

    Notes
    -----
    Privilege separation
        The recommended pattern is to keep ``user`` as a least-privilege
        application role and supply ``bootstrap_user`` (typically a
        superuser or a role with ``CREATEDB`` + extension privileges) only
        for bootstrap / migration jobs.  Ordinary runtime processes should
        never receive the bootstrap credentials.

    Attributes
    ----------
    All fields are accessible both as attributes and via the dict-like
    interface (see ``__getitem__``, ``keys``, ``items``, etc.).

    Examples
    --------
    >>> from py_pgkit.db.settings import PgSettings
    >>> settings = PgSettings(
    ...     host="localhost",
    ...     port=5432,
    ...     database="mydb",
    ...     user="appuser",
    ...     password="secret",
    ...     extensions=["uuid-ossp"],
    ... )
    >>> settings.host
    'localhost'

    With optional bootstrap credentials (privileged steps only):

    >>> settings = PgSettings(
    ...     database="mydb",
    ...     user="appuser",
    ...     password="app-secret",
    ...     bootstrap_user="postgres",
    ...     bootstrap_password="super-secret",
    ... )
    >>> settings.bootstrap_user
    'postgres'

    From a legacy uppercase dict (full backward compatibility):

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

    # Optional privileged identity for administrative steps only
    bootstrap_user: str | None = Field(
        default=None,
        description=(
            "Privileged role used solely for CREATE DATABASE / CREATE "
            "TABLESPACE / CREATE EXTENSION. Ordinary runtime work continues "
            "to use ``user`` / ``password``."
        ),
    )
    bootstrap_password: str | None = Field(
        default=None,
        description=(
            "Password for ``bootstrap_user``. Must be supplied together with "
            "``bootstrap_user`` (both present or both omitted)."
        ),
    )

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
    def _validate_tablespace_and_bootstrap(self) -> PgSettings:
        """Cross-field validation for tablespace and bootstrap credentials.

        - tablespace_path is only required when the tablespace must be created
          (the builder raises a clearer error later if it is missing at that
          moment).
        - bootstrap_user and bootstrap_password must both be present or both
          omitted so callers cannot accidentally supply a half-configured
          privileged identity.
        """
        if self.tablespace_name and not self.tablespace_path:
            # Allowed here; DatabaseBuilder will raise a clearer error if
            # the tablespace does not already exist.
            pass

        has_bootstrap_user = self.bootstrap_user is not None
        has_bootstrap_password = self.bootstrap_password is not None
        if has_bootstrap_user != has_bootstrap_password:
            raise ValueError(
                "bootstrap_user and bootstrap_password must both be provided "
                "or both omitted"
            )
        return self

    # ------------------------------------------------------------------
    # Dict-like interface (preserves original ResolvedSettingsDict API)
    # ------------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        """Allow settings['DB_HOST'] style access (legacy compatibility)."""
        key_map = {
            "DB_HOST": "host",
            "DB_PORT": "port",
            "DB_NAME": "database",
            "DB_USER": "user",
            "PASSWORD": "password",
            "EXTENSIONS": "extensions",
            "TABLESPACE_NAME": "tablespace_name",
            "TABLESPACE_PATH": "tablespace_path",
            "BOOTSTRAP_USER": "bootstrap_user",
            "BOOTSTRAP_PASSWORD": "bootstrap_password",
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
            "BOOTSTRAP_USER",
            "BOOTSTRAP_PASSWORD",
            "EXTENSIONS",
            "TABLESPACE_NAME",
            "TABLESPACE_PATH",
            "host",
            "port",
            "database",
            "user",
            "password",
            "bootstrap_user",
            "bootstrap_password",
            "extensions",
            "tablespace_name",
            "tablespace_path",
        ]

    def values(self) -> list[Any]:
        """Return list of values in the same order as keys()."""
        modern = [
            self.host,
            self.port,
            self.database,
            self.user,
            self.password,
            self.bootstrap_user,
            self.bootstrap_password,
            self.extensions,
            self.tablespace_name,
            self.tablespace_path,
        ]
        # Legacy keys come first in keys(); mirror the same values.
        return modern + modern

    def items(self) -> list[tuple[str, Any]]:
        """Return (key, value) pairs."""
        return list(zip(self.keys(), self.values(), strict=True))

    def __iter__(self):
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    def model_dump(self, **kwargs) -> dict[str, Any]:
        """Override to also include legacy uppercase keys for compatibility."""
        data = super().model_dump(**kwargs)
        data.update(
            {
                "DB_HOST": data["host"],
                "DB_PORT": data["port"],
                "DB_NAME": data["database"],
                "DB_USER": data["user"],
                "PASSWORD": data["password"],
                "BOOTSTRAP_USER": data.get("bootstrap_user"),
                "BOOTSTRAP_PASSWORD": data.get("bootstrap_password"),
                "EXTENSIONS": data.get("extensions"),
                "TABLESPACE_NAME": data.get("tablespace_name"),
                "TABLESPACE_PATH": data.get("tablespace_path"),
            }
        )
        return data

    async def async_ping(self) -> bool:
        """Asynchronously test connectivity to the PostgreSQL server.

        Uses a temporary connection from the pool (or creates one if none
        exists yet). This is the async equivalent of the original
        ``ResolvedSettingsDict.async_ping()``.

        Returns
        -------
        bool
            True if connection succeeds, False otherwise (never raises
            for ping — callers that need the exception should open a
            connection themselves).

        Examples
        --------
        >>> import asyncio
        >>> from py_pgkit.db.settings import PgSettings
        >>> settings = PgSettings(database="test", user="postgres")
        >>> asyncio.run(settings.async_ping())
        True
        """
        from .pool import get_pool  # local import to avoid circularity

        try:
            pool = await get_pool(self)
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def __repr__(self) -> str:
        bootstrap = (
            f", bootstrap_user={self.bootstrap_user!r}"
            if self.bootstrap_user is not None
            else ""
        )
        return (
            f"PgSettings(host={self.host!r}, port={self.port}, "
            f"database={self.database!r}, user={self.user!r}"
            f"{bootstrap})"
        )
