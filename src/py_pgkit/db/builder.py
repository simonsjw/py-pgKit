"""
py_pgkit.db.builder
===================

Incremental PostgreSQL database builder.

This module contains `DatabaseBuilder`, the most powerful component
originally developed in infopypg. It can create:

- Tablespaces (with path)
- Databases
- Extensions (uuid-ossp, pg_trgm, etc.)
- Tables (from SQLAlchemy `Base` metadata, with dependency ordering via NetworkX)
- Triggers and functions (via `ensure_functions_loaded`)
- Partition management via `PartmanManager` (preferred over legacy native helpers)

The builder is **idempotent** — running it multiple times is safe and fast.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Type

import asyncpg
import networkx as nx
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# New recommended import path
from ..partitioning.partman import PartmanManager
from .methods.db_tools import ensure_functions_loaded
from .pool import get_pool
from .settings import PgSettings

logger = logging.getLogger(__name__)


class DatabaseBuilder:
    """
    Incremental database infrastructure builder.

    Parameters
    ----------
    settings : PgSettings
        Connection and infrastructure settings.
    models : list[Type[DeclarativeBase]] | None, optional
        SQLAlchemy declarative models whose tables should be created.
    create_tablespace, create_database, create_extensions, create_tables,
    create_triggers_and_functions : bool, optional
        Flags to control which steps are executed.
    partition_strategy : str | None, optional
        Legacy option. Prefer using `with_partition_support()` instead.
    functions : list[str] | str | Path | list[Path] | None, optional
        SQL functions/triggers to load (directory, file, or list).
    """

    def __init__(
        self,
        settings: PgSettings,
        models: list[Type[Any]] | None = None,
        create_tablespace: bool = True,
        create_database: bool = True,
        create_extensions: bool = True,
        create_tables: bool = True,
        create_triggers_and_functions: bool = True,
        partition_strategy: str | None = None,
        functions: list[str] | str | Path | list[Path] | None = None,
    ) -> None:
        self.settings = settings
        self.models = models or []
        self.create_tablespace = create_tablespace
        self.create_database = create_database
        self.create_extensions = create_extensions
        self.create_tables = create_tables
        self.create_triggers_and_functions = create_triggers_and_functions
        self.partition_strategy = partition_strategy
        self.functions = functions
        self.engine: AsyncEngine | None = None
        self._pool: asyncpg.Pool | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await get_pool(self.settings)
        return self._pool

    async def build(self) -> None:
        logger.info("Starting DatabaseBuilder for %s", self.settings.database)

        pool = await self._get_pool()

        if self.create_tablespace and self.settings.tablespace_name:
            await self._ensure_tablespace(pool)

        if self.create_database:
            await self._ensure_database()

        if self.create_extensions and self.settings.extensions:
            await self._ensure_extensions(pool)

        if self.create_tables and self.models:
            await self._ensure_tables()

        if self.create_triggers_and_functions:
            await self._ensure_triggers_and_functions(pool)

        if self.partition_strategy == "daily":
            logger.warning(
                "partition_strategy='daily' is deprecated. "
                "Use with_partition_support() after build() instead."
            )

        logger.info("DatabaseBuilder completed successfully")

    # ------------------------------------------------------------------
    # Internal helpers (unchanged from your latest version)
    # ------------------------------------------------------------------

    async def _get_engine(self) -> AsyncEngine:
        if self.engine is None:
            url = (
                f"postgresql+asyncpg://{self.settings.user}:"
                f"{self.settings.password or ''}@{self.settings.host}:"
                f"{self.settings.port}/{self.settings.database}"
            )
            self.engine = create_async_engine(url, echo=self.settings.echo)
        return self.engine

    async def _ensure_tablespace(self) -> None:
        """Create tablespace if it does not exist."""
        ts_name = self.settings.tablespace_name
        ts_path = self.settings.tablespace_path

        pool: asyncpg.Pool = await self._get_pool()

        if not ts_name:
            return

        async with pool.acquire() as conn:
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

            await conn.execute(f"CREATE TABLESPACE {ts_name} LOCATION '{ts_path}'")
            logger.info("Created tablespace %s at %s", ts_name, ts_path)

    async def _ensure_database(self) -> None:
        """Create the target database if it does not exist."""
        db_name = self.settings.database

        # We must connect to 'postgres' or 'template1' to create DBs
        admin_pool = await self._get_admin_pool()

        async with admin_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db_name
            )
            if exists:
                logger.debug("Database %s already exists", db_name)
                return

            # Create with correct tablespace if specified
            ts_clause = ""
            if self.settings.tablespace_name:
                ts_clause = f" TABLESPACE {self.settings.tablespace_name}"

            await conn.execute(f'CREATE DATABASE "{db_name}"{ts_clause}')
            logger.info("Created database %s", db_name)

    async def _ensure_extensions(self) -> None:
        """Create listed extensions if they do not exist."""

        pool: asyncpg.Pool = await self._get_pool()

        async with pool.acquire() as conn:
            for ext in self.settings.extensions or []:
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_extension WHERE extname = $1", ext
                )
                if exists:
                    continue
                await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
                logger.info("Created extension %s", ext)

    async def _ensure_tables(self) -> None:
        """
        Create tables from SQLAlchemy models in dependency order.

        Uses NetworkX to build a directed graph of foreign-key
        dependencies and then performs a topological sort.
        """
        if not self.models:
            return

        # Collect all tables from all Base classes
        all_tables: list[Table] = []
        for model in self.models:
            if hasattr(model, "metadata"):
                all_tables.extend(model.metadata.sorted_tables)

        if not all_tables:
            return

        # Build dependency graph
        graph = nx.DiGraph()
        for table in all_tables:
            graph.add_node(table.name)
            for fk in table.foreign_keys:
                graph.add_edge(fk.column.table.name, table.name)

        # Topological sort (NetworkX)
        try:
            ordered_names = list(nx.topological_sort(graph))
        except nx.NetworkXUnfeasible:
            logger.warning("Circular dependency detected in table graph!")
            ordered_names = [t.name for t in all_tables]

        # Create in order
        engine = await self._get_engine()
        async with engine.begin() as conn:
            for name in ordered_names:
                table = next(t for t in all_tables if t.name == name)
                await conn.run_sync(table.create, checkfirst=True)
                logger.debug("Ensured table %s", name)

    async def _ensure_triggers_and_functions(self, pool: asyncpg.Pool) -> None:
        functions = getattr(self, "functions", None)
        if functions:
            await ensure_functions_loaded(functions, self.settings)
            logger.info("Custom functions and triggers loaded")

    # ------------------------------------------------------------------
    # Partitioning Support
    # ------------------------------------------------------------------

    async def with_partition_support(
        self,
        partitioned_tables: list[str] | None = None,
        premake: int = 14,
    ) -> "DatabaseBuilder":
        """
        Configure pg_partman-backed partition management for the given tables.

        This is the recommended way to enable automatic partition creation
        and maintenance using `PartmanManager`.

        Parameters
        ----------
        partitioned_tables : list of str, optional
            Fully qualified table names. Defaults to ["responses"].
        premake : int, default 14
            Number of future partitions to pre-create.

        Returns
        -------
        DatabaseBuilder
            Self for method chaining.
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
