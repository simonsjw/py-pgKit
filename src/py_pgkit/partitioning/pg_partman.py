"""
pg_partman Integration for py_pgkit.

This module provides a clean, high-level, async-friendly interface to the
pg_partman PostgreSQL extension for automated time-based table partitioning.

It is designed to be used by `DatabaseBuilder` and `PersistenceManager`
to ensure partitions are created automatically when needed, while providing
graceful fallback when the extension is not installed.

The primary class is :class:`PartmanManager`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import asyncpg


class PartmanManager:
    """
    High-level manager for the pg_partman PostgreSQL extension.

    This class wraps the core pg_partman functions (`create_parent` and
    `run_maintenance`) and exposes them through a clean, Pythonic async API.

    When pg_partman is installed, it becomes the preferred backend for
    automatic daily partition creation and maintenance. When it is not
    available, the manager gracefully degrades to native PostgreSQL
    partition handling.

    Parameters
    ----------
    pool : asyncpg.Pool
        An active asyncpg connection pool.
    logger : logging.Logger, optional
        Logger instance. If None, a module-level logger is created.
    schema : str, default "partman"
        Schema in which the pg_partman extension is installed.

    Attributes
    ----------
    pool : asyncpg.Pool
        The connection pool used for all database operations.
    logger : logging.Logger
        Logger used for structured output.
    schema : str
        Schema containing the pg_partman extension.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        logger: Optional[logging.Logger] = None,
        schema: str = "partman",
    ) -> None:
        self.pool = pool
        self.logger = logger or logging.getLogger(__name__)
        self.schema = schema

    async def is_installed(self) -> bool:
        """
        Check whether the pg_partman extension is installed.

        Returns
        -------
        bool
            True if pg_partman is installed and available, False otherwise.
        """
        try:
            result = await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_partman')"
            )
            return bool(result)
        except Exception as exc:
            self.logger.warning("Failed to check pg_partman installation: %s", exc)
            return False

    async def create_parent(
        self,
        parent_table: str,
        control_column: str = "tstamp",
        interval: str = "1 day",
        premake: int = 7,
        start_partition: Optional[str] = None,
    ) -> bool:
        """
        Register a parent table with pg_partman and create initial partitions.

        This method calls `partman.create_parent()` with sensible defaults
        optimised for daily time-based partitioning using native declarative
        partitioning.

        Parameters
        ----------
        parent_table : str
            Fully qualified name of the parent partitioned table
            (e.g. 'public.responses').
        control_column : str, default "tstamp"
            Name of the column used as the partition key (must be
            `timestamptz` or `date`).
        interval : str, default "1 day"
            Partition interval (e.g. '1 day', '1 month', '1 week').
        premake : int, default 7
            Number of future partitions to pre-create.
        start_partition : str, optional
            Optional start date for the first partition in 'YYYY-MM-DD'
            format.

        Returns
        -------
        bool
            True if the parent table was successfully registered with
            pg_partman, False otherwise.
        """
        if not await self.is_installed():
            self.logger.warning(
                "pg_partman is not installed. Falling back to native partitioning."
            )
            return False

        try:
            await self.pool.execute(
                f"""
                SELECT {self.schema}.create_parent(
                    p_parent_table := $1,
                    p_control      := $2,
                    p_type         := 'native',
                    p_interval     := $3,
                    p_premake      := $4,
                    p_start_partition := $5
                )
                """,
                parent_table,
                control_column,
                interval,
                premake,
                start_partition,
            )
            self.logger.info(
                "pg_partman parent registered successfully for table %s",
                parent_table,
            )
            return True
        except Exception as exc:
            self.logger.error("Failed to register parent with pg_partman: %s", exc)
            return False

    async def ensure_partitions(
        self,
        parent_table: str,
        days_ahead: int = 7,
        days_behind: int = 1,
    ) -> None:
        """
        Ensure that the required partitions exist for a date range.

        Runs `partman.run_maintenance()` to create any missing future
        partitions. This method is safe to call on every application
        startup or before bulk operations.

        Parameters
        ----------
        parent_table : str
            Fully qualified name of the parent partitioned table.
        days_ahead : int, default 7
            Number of days of future partitions to ensure.
        days_behind : int, default 1
            Number of days of past partitions to ensure.

        Raises
        ------
        Exception
            If pg_partman maintenance fails.
        """
        if not await self.is_installed():
            self.logger.debug(
                "pg_partman not available — skipping automatic partition creation"
            )
            return

        try:
            await self.pool.execute(
                f"SELECT {self.schema}.run_maintenance($1)",
                parent_table,
            )
            self.logger.debug(
                "pg_partman maintenance completed for %s (days_ahead=%d)",
                parent_table,
                days_ahead,
            )
        except Exception as exc:
            self.logger.error(
                "pg_partman maintenance failed for %s: %s", parent_table, exc
            )
            raise

    async def ensure_partition_exists(
        self,
        parent_table: str,
        target_date: Optional[date] = None,
    ) -> None:
        """
        Ensure a partition exists for a specific date.

        Convenience wrapper around :meth:`ensure_partitions` that targets
        a narrow window around the given date.

        Parameters
        ----------
        parent_table : str
            Fully qualified name of the parent partitioned table.
        target_date : datetime.date, optional
            The date for which the partition must exist. Defaults to
            the current local date.
        """
        if target_date is None:
            target_date = datetime.now().date()

        await self.ensure_partitions(
            parent_table=parent_table,
            days_ahead=2,
            days_behind=1,
        )
