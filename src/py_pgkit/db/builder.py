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
- Daily range partitioning (via `ensure_partition_exists`)

The builder now reuses the shared helpers from `db/methods/` to reduce
duplication and ensure consistent behaviour across the package.

The builder is **idempotent** — running it multiple times is safe and
fast (it only performs work that has not already been done).

All operations use the shared `PgPoolManager` so connection costs are
minimal even when the builder is called frequently.

Examples
--------
>>> from py_pgkit.db import DatabaseBuilder, PgSettings
>>> from pathlib import Path

>>> settings = PgSettings(database="analytics")

>>> builder = DatabaseBuilder(
...     settings=settings,
...     functions=Path("/app/sql/functions/"),
...     partition_strategy="daily"
... )

>>> await builder.build()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Type

import asyncpg
import networkx as nx
from sqlalchemy import MetaData, Table, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .methods.db_tools import ensure_functions_loaded, ensure_partition_exists
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
        Tables are created in dependency order (foreign keys) using
        NetworkX topological sort.
    create_tablespace : bool, optional
        Whether to create the tablespace if it does not exist
        (default True).
    create_database : bool, optional
        Whether to create the target database if it does not exist
        (default True).
    create_extensions : bool, optional
        Whether to create listed extensions (default True).
    create_tables : bool, optional
        Whether to create tables from `models` (default True).
    create_triggers_and_functions : bool, optional
        Whether to load custom SQL functions and triggers (default True).
    partition_strategy : str | None, optional
        'daily' for daily range partitioning on a timestamp column
        (advanced feature — see `add_daily_partition`).
    functions : list[str] | str | Path | None, optional
        SQL functions to load (directory, file, or list of strings).
        Passed to `ensure_functions_loaded`.

    Attributes
    ----------
    settings : PgSettings
        The settings used for this build.
    engine : AsyncEngine | None
        SQLAlchemy async engine (created lazily).

    Examples
    --------
    >>> import asyncio
    >>> from py_pgkit.db.settings import PgSettings
    >>> from py_pgkit.db.builder import DatabaseBuilder
    >>> from mymodels import Base  # your SQLAlchemy models
    >>> settings = PgSettings(database="analytics")
    >>> builder = DatabaseBuilder(
    ...     settings=settings,
    ...     models=[Base],
    ...     extensions=["uuid-ossp", "pg_trgm"],
    ... )
    >>> asyncio.run(builder.build())
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
        functions: list[str] | str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.models = models or []
        self.create_tablespace = create_tablespace
        self.create_database = create_database
        self.create_extensions = create_extensions
        self.create_tables = create_tables
        self.create_triggers_and_functions = create_triggers_and_functions
        self.partition_strategy = partition_strategy
        self.functions = functions                                                        # passed to ensure_functions_loaded
        self.engine: AsyncEngine | None = None
        self._pool: asyncpg.Pool | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        """Lazily obtain the shared connection pool."""
        if self._pool is None:
            self._pool = await get_pool(self.settings)
        return self._pool

    async def build(self) -> None:
        """
        Execute the full incremental build process.

        The order of operations is carefully chosen to satisfy
        PostgreSQL constraints:

        1. Tablespace (if requested)
        2. Database (if requested)
        3. Extensions (if requested)
        4. Tables (via SQLAlchemy metadata + topological sort)
        5. Triggers & functions (if requested)
        6. Partitioning setup (if requested)

        Each step is idempotent.
        """
        logger.info("Starting DatabaseBuilder for %s", self.settings.database)

        pool = await self._get_pool()

        if self.create_tablespace and self.settings.tablespace_name:
            await self._ensure_tablespace(pool)

        if self.create_database:
            await self._ensure_database(pool)

        if self.create_extensions and self.settings.extensions:
            await self._ensure_extensions(pool)

        if self.create_tables and self.models:
            await self._ensure_tables()

        if self.create_triggers_and_functions:
            await self._ensure_triggers_and_functions(pool)

        if self.partition_strategy == "daily":
            await self._setup_daily_partitioning(pool)

        logger.info("DatabaseBuilder completed successfully")

    # ------------------------------------------------------------------
    # Internal implementation methods (each is independently useful)
    # ------------------------------------------------------------------

    async def _ensure_tablespace(self, pool: asyncpg.Pool) -> None:
        """Create tablespace if it does not exist."""
        ts_name = self.settings.tablespace_name
        ts_path = self.settings.tablespace_path

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

    async def _ensure_database(self, pool: asyncpg.Pool) -> None:
        """Create the target database if it does not exist."""
        db_name = self.settings.database

        # We must connect to 'postgres' or 'template1' to create DBs
        admin_pool = await get_pool(
            PgSettings(
                host=self.settings.host,
                port=self.settings.port,
                database="postgres",
                user=self.settings.user,
                password=self.settings.password,
            )
        )

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

    async def _ensure_extensions(self, pool: asyncpg.Pool) -> None:
        """Create listed extensions if they do not exist."""
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

    async def _get_engine(self) -> AsyncEngine:
        """Lazily create SQLAlchemy async engine."""
        if self.engine is None:
            url = (
                f"postgresql+asyncpg://{self.settings.user}:"
                f"{self.settings.password or ''}@{self.settings.host}:"
                f"{self.settings.port}/{self.settings.database}"
            )
            self.engine = create_async_engine(url, echo=self.settings.echo)
        return self.engine

    async def _ensure_triggers_and_functions(self, pool: asyncpg.Pool) -> None:
        """
        Load custom SQL functions and triggers using the shared helper.

        If the builder was initialised with a `functions` parameter
        (path to .sql directory/file or list of SQL strings), those
        functions will be loaded. Otherwise this step is a no-op.
        """
        functions = getattr(self, "functions", None)
        if functions:
            await ensure_functions_loaded(functions, self.settings)
            logger.info("Custom functions loaded")

    async def _setup_daily_partitioning(self, pool: asyncpg.Pool) -> None:
        """
        Set up daily range partitioning using the shared helper.

        This delegates to `ensure_partition_exists` for each day in a
        sensible default range (today and tomorrow). For full control
        use `add_daily_partition` directly.
        """
        from datetime import date, timedelta

        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        await self.add_daily_partition("logs", today, tomorrow)

    async def add_daily_partition(
        self, table_name: str, start_date: str, end_date: str
    ) -> None:
        """
        Create a specific daily partition using the modern helper.

        Delegates to `ensure_partition_exists` from the methods module.
        """
        partition_name = f"{table_name}_{start_date.replace('-', '_')}"
        await ensure_partition_exists(
            table_name=table_name,
            partition_name=partition_name,
            start_value=start_date,
            end_value=end_date,
            settings=self.settings,
        )
        logger.info("Ensured partition %s", partition_name)

    async def with_partition_support(
        self,
        partition_backend: str = "pg_partman",
        auto_ensure_partitions: bool = True,
        partitioned_tables: Optional[list[str]] = None,
    ) -> "DatabaseBuilder":
        """
        Enable automatic partition management during database setup.

        When `partition_backend="pg_partman"` and the extension is installed,
        `pg_partman` will be used for fully automatic partition creation and
        maintenance. Otherwise the native `ensure_partition_exists` path is used.

        Parameters
        ----------
        partition_backend : {'pg_partman', 'native'}, default "pg_partman"
            Partition management backend to use.
        auto_ensure_partitions : bool, default True
            Whether to automatically ensure partitions exist for the listed tables.
        partitioned_tables : list of str, optional
            List of fully qualified table names that require partitioning.
            Defaults to `["responses"]`.

        Returns
        -------
        DatabaseBuilder
            The current builder instance (for method chaining).
        """
        if partitioned_tables is None:
            partitioned_tables = ["responses"]

        if partition_backend == "pg_partman":
            partman = PartmanManager(self.pool, self.logger)
            if await partman.is_installed():
                for table in partitioned_tables:
                    await partman.create_parent(table)
                    if auto_ensure_partitions:
                        await partman.ensure_partitions(table)
            else:
                self.logger.warning(
                    "pg_partman requested but not installed. "
                    "Falling back to native partitioning."
                )
                # --- Native fallback using existing py_pgkit function ---
                from py_pgkit.db import ensure_partition_exists

                for table in partitioned_tables:
                    await ensure_partition_exists(
                        pool=self.pool,
                        table=table,
                        partition_key="tstamp",
                        strategy="daily",
                        days_ahead=7,
                    )
        else:
            # Pure native path (when user explicitly chooses "native")
            from py_pgkit.db import ensure_partition_exists

            for table in partitioned_tables:
                await ensure_partition_exists(
                    pool=self.pool,
                    table=table,
                    partition_key="tstamp",
                    strategy="daily",
                    days_ahead=7,
                )

        return self
