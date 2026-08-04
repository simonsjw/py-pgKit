"""
py_pgkit.db.builder
===================

Incremental PostgreSQL database builder.

This module contains ``DatabaseBuilder``, the most powerful component
originally developed in infopypg. It can create:

- Tablespaces (with path)
- Databases
- Extensions (uuid-ossp, pg_trgm, vector, pg_partman, …)
- Tables (from SQLAlchemy ``Base`` metadata, with dependency ordering via NetworkX)
- Triggers and functions (via ``ensure_functions_loaded``)
- Partition management via ``PartmanManager`` (preferred over legacy native helpers)

The builder is **idempotent** — running it multiple times is safe and fast.

Privilege separation
--------------------
When ``PgSettings.bootstrap_user`` / ``bootstrap_password`` are supplied,
administrative steps (``CREATE DATABASE``, ``CREATE TABLESPACE``,
``CREATE EXTENSION``, and schema privilege grants) are performed under that
privileged identity. Table creation, triggers, functions and the runtime
pool continue to use the ordinary ``user`` / ``password``.  This lets
bootstrap jobs elevate only for the steps that require it while ordinary
application roles stay least-privilege.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Type

import asyncpg
import networkx as nx
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..partitioning.pg_partman import PartmanManager
from .methods.db_tools import ensure_functions_loaded
from .pool import get_pool
from .settings import PgSettings

logger = logging.getLogger(__name__)


class DatabaseBuilder:
    """Incremental database infrastructure builder.

    Parameters
    ----------
    settings : PgSettings
        Connection and infrastructure settings.  When
        ``settings.bootstrap_user`` is set, that identity is used for
        ``CREATE DATABASE``, ``CREATE TABLESPACE``, ``CREATE EXTENSION``
        and granting the ordinary role access to ``public`` / ``partman``;
        the ordinary ``user`` / ``password`` are used for everything else.
    admin_db : str
        Name of the maintenance database used for ``CREATE DATABASE`` /
        ``CREATE TABLESPACE`` (default ``"postgres"``).
    models : list of declarative model types or None, optional
        SQLAlchemy declarative models whose tables should be created.
    create_tablespace, create_database, create_extensions, create_tables,
    create_triggers_and_functions : bool, optional
        Flags controlling which steps are executed.
    functions : list of str / Path, str, Path or None, optional
        SQL functions / triggers to load (directory, file, or list).

    Notes
    -----
    Privilege model
        - No bootstrap credentials → identical behaviour to earlier releases
          (the ordinary role performs every step).
        - Bootstrap credentials present → privileged steps run as
          ``bootstrap_user``; table DDL and the runtime pool stay on the
          ordinary role.  After ``CREATE DATABASE``, the ordinary role is
          set as ``OWNER`` of schema ``public``.  After ``pg_partman`` is
          installed, the ordinary role is granted full use of schema
          ``partman`` so ``partman.create_parent`` / ``run_maintenance``
          work without further elevation.

    See Also
    --------
    PgSettings : connection settings, including the optional bootstrap pair.
    """

    def __init__(
        self,
        settings: PgSettings,
        admin_db: str = "postgres",
        models: list[Type[Any]] | None = None,
        create_tablespace: bool = True,
        create_database: bool = True,
        create_extensions: bool = True,
        create_tables: bool = True,
        create_triggers_and_functions: bool = True,
        functions: list[str] | str | Path | list[Path] | None = None,
    ) -> None:
        self.settings = settings
        self.admin_db = admin_db
        self.models = models or []
        self.create_tablespace = create_tablespace
        self.create_database = create_database
        self.create_extensions = create_extensions
        self.create_tables = create_tables
        self.create_triggers_and_functions = create_triggers_and_functions
        self.functions = functions
        self.engine: AsyncEngine | None = None
        self._pool: asyncpg.Pool | None = None
        self._admin_pool: asyncpg.Pool | None = None

    async def build(self) -> None:
        """Run the full incremental build sequence.

        Order of operations
        -------------------
        1. Create tablespace (if requested) — admin / bootstrap connection.
        2. Create database (if requested) — admin / bootstrap connection,
           with ``OWNER`` set to the ordinary runtime role.
        3. Ensure the ordinary role can create objects in schema ``public``
           (PostgreSQL 15+ compatibility).
        4. Connect to the target database.
        5. Create extensions — bootstrap identity when supplied, otherwise
           the ordinary role.
        6. Grant the ordinary role access to schema ``partman`` when
           ``pg_partman`` is present.
        7. Create tables, triggers and functions — ordinary role.
        """
        logger.info("Starting DatabaseBuilder for %s", self.settings.database)

        # Phase 1 — must use a connection that is *not* inside the target DB
        await self._get_admin_pool()

        if self.create_tablespace and self.settings.tablespace_name:
            await self._ensure_tablespace()

        if self.create_database:
            await self._ensure_database()

        # Ensure ordinary role can DDL in public (needed on PG 15+ even when
        # the database already existed from a prior bootstrap).
        await self._ensure_public_schema_privileges()

        # Phase 2 — target database
        await self._get_pool()

        if self.create_extensions and self.settings.extensions:
            await self._ensure_extensions()

        # partman schema is owned by whoever created the extension; grant the
        # ordinary role access so create_parent / run_maintenance work.
        await self._ensure_partman_schema_privileges()

        if self.create_tables and self.models:
            await self._ensure_tables()

        if self.create_triggers_and_functions:
            await self._ensure_triggers_and_functions()

        logger.info("DatabaseBuilder completed successfully")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_pool(self) -> asyncpg.Pool:
        """Return (and cache) a pool connected to the target database.

        Always uses the ordinary runtime credentials (``settings.user`` /
        ``settings.password``).
        """
        if self._pool is None:
            self._pool = await get_pool(self.settings)
        return self._pool

    async def _get_admin_pool(self) -> asyncpg.Pool:
        """Return (and cache) a pool connected to the maintenance database.

        Tablespaces and databases can only be created from a connection that
        is not already inside the target database.

        When ``settings.bootstrap_user`` is set the pool is opened with that
        privileged identity; otherwise the ordinary credentials are used
        (identical to earlier releases).
        """
        if self._admin_pool is None:
            if self.settings.bootstrap_user is not None:
                admin_settings = self.settings.model_copy(
                    update={
                        "database": self.admin_db,
                        "user": self.settings.bootstrap_user,
                        "password": self.settings.bootstrap_password,
                    }
                )
            else:
                admin_settings = self.settings.model_copy(
                    update={"database": self.admin_db}
                )
            self._admin_pool = await get_pool(admin_settings)
        return self._admin_pool

    async def _get_engine(self) -> AsyncEngine:
        """Return (and cache) a SQLAlchemy async engine for the target DB.

        Always uses the ordinary runtime credentials.
        """
        if self.engine is None:
            url = (
                f"postgresql+asyncpg://{self.settings.user}:"
                f"{self.settings.password or ''}@{self.settings.host}:"
                f"{self.settings.port}/{self.settings.database}"
            )
            self.engine = create_async_engine(url, echo=self.settings.echo)
        return self.engine

    async def _bootstrap_target_pool(self) -> asyncpg.Pool | None:
        """Pool connected to the target DB as the bootstrap user, or None."""
        if self.settings.bootstrap_user is None:
            return None
        priv_settings = self.settings.model_copy(
            update={
                "user": self.settings.bootstrap_user,
                "password": self.settings.bootstrap_password,
            }
        )
        return await get_pool(priv_settings)

    async def _ensure_tablespace(self) -> None:
        """Create the tablespace if it does not already exist.

        Runs under the admin / bootstrap identity.
        """
        ts_name = self.settings.tablespace_name
        ts_path = self.settings.tablespace_path

        if not ts_name:
            return

        admin_pool = await self._get_admin_pool()

        async with admin_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_tablespace WHERE spcname = $1", ts_name
            )
            if exists:
                logger.debug("Tablespace %s already exists", ts_name)
                return

            if not ts_path:
                raise ValueError(
                    f"tablespace_path must be provided when creating "
                    f"tablespace '{ts_name}'"
                )

            await conn.execute(
                f"CREATE TABLESPACE {ts_name} LOCATION '{ts_path}'"
            )
            logger.info("Created tablespace %s at %s", ts_name, ts_path)

    async def _ensure_database(self) -> None:
        """Create the target database if it does not already exist.

        Runs under the admin / bootstrap identity.  When creating a new
        database the ordinary runtime role (``settings.user``) is set as
        ``OWNER`` so it can create objects under PostgreSQL 15+ default
        privilege rules.
        """
        db_name = self.settings.database
        owner = self.settings.user
        admin_pool = await self._get_admin_pool()

        async with admin_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db_name
            )
            if exists:
                logger.debug("Database %s already exists", db_name)
                return

            ts_clause = ""
            if self.settings.tablespace_name:
                ts_clause = f" TABLESPACE {self.settings.tablespace_name}"

            # OWNER ensures the runtime role owns public and can CREATE.
            await conn.execute(
                f'CREATE DATABASE "{db_name}" OWNER "{owner}"{ts_clause}'
            )
            logger.info("Created database %s owned by %s", db_name, owner)

    async def _ensure_public_schema_privileges(self) -> None:
        """Grant the ordinary role ownership of schema public.

        Required on PostgreSQL 15+ where ``CREATE`` is revoked from
        ``PUBLIC`` by default.  When the database was created earlier by a
        superuser without transferring ownership, table DDL as the ordinary
        role fails with ``permission denied for schema public``.

        Runs only when bootstrap credentials are available.  Idempotent.
        """
        pool = await self._bootstrap_target_pool()
        if pool is None:
            return

        owner = self.settings.user
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    f'ALTER SCHEMA public OWNER TO "{owner}"'
                )
                logger.info(
                    "Set schema public owner to %s in database %s",
                    owner,
                    self.settings.database,
                )
            except Exception as alter_exc:
                logger.debug(
                    "ALTER SCHEMA public OWNER TO %s failed (%s); "
                    "falling back to GRANT",
                    owner,
                    alter_exc,
                )
                await conn.execute(
                    f'GRANT USAGE, CREATE ON SCHEMA public TO "{owner}"'
                )
                logger.info(
                    "Granted USAGE, CREATE on schema public to %s "
                    "in database %s",
                    owner,
                    self.settings.database,
                )

    async def _ensure_partman_schema_privileges(self) -> None:
        """Grant the ordinary role full use of schema partman.

        ``pg_partman`` is installed into schema ``partman`` under the
        bootstrap identity.  ``partman.create_parent`` is SECURITY INVOKER:
        the caller can enter the function with USAGE+EXECUTE, but its
        internal ``EXECUTE`` creates objects inside schema ``partman`` and
        therefore needs ``CREATE`` on that schema.  ``GRANT ALL ON SCHEMA``
        covers both USAGE and CREATE.

        Idempotent.  No-op when bootstrap credentials are absent or the
        schema is not present.
        """
        pool = await self._bootstrap_target_pool()
        if pool is None:
            return

        owner = self.settings.user
        async with pool.acquire() as conn:
            schema_exists = await conn.fetchval(
                "SELECT 1 FROM pg_namespace WHERE nspname = 'partman'"
            )
            if not schema_exists:
                return

            # ALL on schema = USAGE + CREATE (required for internal EXECUTE)
            await conn.execute(
                f'GRANT ALL ON SCHEMA partman TO "{owner}"'
            )
            await conn.execute(
                f'GRANT ALL ON ALL TABLES IN SCHEMA partman TO "{owner}"'
            )
            await conn.execute(
                f'GRANT ALL ON ALL SEQUENCES IN SCHEMA partman TO "{owner}"'
            )
            await conn.execute(
                f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO "{owner}"'
            )
            # pg_partman 5.x exposes some entry points as procedures
            try:
                await conn.execute(
                    f'GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman '
                    f'TO "{owner}"'
                )
            except Exception as proc_exc:
                logger.debug(
                    "GRANT EXECUTE ON ALL PROCEDURES in partman skipped: %s",
                    proc_exc,
                )
            try:
                await conn.execute(
                    f'GRANT USAGE ON ALL TYPES IN SCHEMA partman TO "{owner}"'
                )
            except Exception as type_exc:
                logger.debug(
                    "GRANT USAGE ON ALL TYPES in partman skipped: %s",
                    type_exc,
                )
            # Future objects created by maintenance as the bootstrap role
            await conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE '
                f'"{self.settings.bootstrap_user}" IN SCHEMA partman '
                f'GRANT ALL ON TABLES TO "{owner}"'
            )
            await conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE '
                f'"{self.settings.bootstrap_user}" IN SCHEMA partman '
                f'GRANT ALL ON SEQUENCES TO "{owner}"'
            )
            await conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE '
                f'"{self.settings.bootstrap_user}" IN SCHEMA partman '
                f'GRANT EXECUTE ON FUNCTIONS TO "{owner}"'
            )
            logger.info(
                "Granted full partman schema privileges to %s in database %s",
                owner,
                self.settings.database,
            )

    async def _ensure_extensions(self) -> None:
        """Create listed extensions if they do not exist.

        When bootstrap credentials are present they are used to connect to
        the *target* database for the ``CREATE EXTENSION`` statements
        (extensions live per-database).  Otherwise the ordinary runtime
        pool is used, preserving historical behaviour.

        ``pg_partman`` receives special handling (explicit ``partman``
        schema) because that is the most reliable installation path.
        """
        if self.settings.bootstrap_user is not None:
            # Privileged identity, still pointing at the target database.
            ext_settings = self.settings.model_copy(
                update={
                    "user": self.settings.bootstrap_user,
                    "password": self.settings.bootstrap_password,
                }
            )
            pool = await get_pool(ext_settings)
            logger.debug(
                "Creating extensions as bootstrap user %s",
                self.settings.bootstrap_user,
            )
        else:
            pool = await self._get_pool()

        async with pool.acquire() as conn:
            for ext in self.settings.extensions or []:
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_extension WHERE extname = $1", ext
                )
                if exists:
                    continue

                try:
                    if ext == "pg_partman":
                        # Explicit schema is the most reliable installation path
                        await conn.execute(
                            "CREATE SCHEMA IF NOT EXISTS partman"
                        )
                        await conn.execute(
                            'CREATE EXTENSION IF NOT EXISTS "pg_partman" '
                            "SCHEMA partman"
                        )
                    else:
                        sql = "CREATE EXTENSION IF NOT EXISTS \"" + ext + "\""
                        await conn.execute(sql)
                    logger.info("Created extension %s", ext)
                except Exception as exc:
                    logger.error("Failed to create extension %s: %s", ext, exc)
                    raise

    async def _ensure_tables(self) -> None:
        """Create tables from SQLAlchemy models in dependency order.

        Uses NetworkX to build a directed graph of foreign-key dependencies
        and then performs a topological sort.  Self-referential foreign keys
        are ignored for ordering purposes (PostgreSQL allows them once the
        table exists).  Always runs under the ordinary runtime credentials.
        """
        if not self.models:
            return

        all_tables: list[Table] = []
        for model in self.models:
            if hasattr(model, "metadata"):
                all_tables.extend(model.metadata.sorted_tables)

        if not all_tables:
            return

        graph = nx.DiGraph()
        for table in all_tables:
            graph.add_node(table.name)
            for fk in table.foreign_keys:
                parent = fk.column.table.name
                # Self-referential FKs are valid DDL but create cycles in the
                # dependency graph; skip them for topological ordering.
                if parent != table.name:
                    graph.add_edge(parent, table.name)

        try:
            ordered_names = list(nx.topological_sort(graph))
        except nx.NetworkXUnfeasible:
            logger.warning("Circular dependency detected in table graph!")
            ordered_names = [t.name for t in all_tables]

        engine = await self._get_engine()
        async with engine.begin() as conn:
            for name in ordered_names:
                table = next(t for t in all_tables if t.name == name)
                await conn.run_sync(table.create, checkfirst=True)
                logger.debug("Ensured table %s", name)

    async def _ensure_triggers_and_functions(self) -> None:
        """Load custom SQL functions / triggers if any were supplied.

        Always runs under the ordinary runtime credentials.
        """
        if not self.functions:
            return

        pool = await self._get_pool()
        await ensure_functions_loaded(self.functions, pool)
        logger.info("Custom functions and triggers loaded")

    # ------------------------------------------------------------------
    # Partitioning Support
    # ------------------------------------------------------------------

    async def with_partition_support(
        self,
        partitioned_tables: list[str] | None = None,
        premake: int = 14,
    ) -> "DatabaseBuilder":
        """Configure pg_partman-backed partition management for the given tables.

        This is the recommended way to enable automatic partition creation
        and maintenance using ``PartmanManager``.

        Parameters
        ----------
        partitioned_tables : list of str or None, optional
            Parent tables to register with pg_partman.  Defaults to
            ``["responses"]``.
        premake : int, optional
            Number of partitions to pre-create (default 14).

        Returns
        -------
        DatabaseBuilder
            ``self``, for fluent chaining.
        """
        if partitioned_tables is None:
            partitioned_tables = ["responses"]

        pool = await self._get_pool()
        partman = PartmanManager(pool, logger=logger)

        if not await partman.is_installed():
            logger.warning(
                "pg_partman extension is not installed. "
                "Partition management will not be configured."
            )
            return self

        for table in partitioned_tables:
            success = await partman.create_parent(
                parent_table=table,
                premake=premake,
            )
            if success:
                await partman.ensure_partitions(table, days_ahead=premake)
                logger.info("pg_partman configured for table: %s", table)

        return self
