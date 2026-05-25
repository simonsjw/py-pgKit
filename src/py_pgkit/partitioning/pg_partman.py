"""
pg_partman Integration for py_pgkit.

This module provides a high-level, async-friendly interface to the
pg_partman PostgreSQL extension for automated time-based table partitioning
using native declarative partitioning.

The design follows modern best practices:

- Single source of truth via ``initialize()`` (registers table + creates
  initial partitions).
- Fast-path checks using an in-memory ``CoveredRange`` dataclass so most
  calls incur zero database overhead.
- Graceful degradation when pg_partman is not installed.
- Clear separation between one-time setup and per-insert partition
  assurance.

The primary class is :class:`PartmanManager`.

Notes
-----
This module is intended to be used by ``DatabaseBuilder`` and
``PersistenceManager``. It assumes the target table (e.g. ``public.responses``)
has already been created as a declarative partitioned table with a
``timestamptz`` or ``date`` column used as the partition key.

Examples
--------
>>> from py_pgkit.partman import PartmanManager
>>> pm = PartmanManager(pool)
>>> await pm.initialize(parent_table="public.responses", premake=14)
>>> await pm.ensure_partition_for_date()  # cheap fast-path check
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import asyncpg


@dataclass
class CoveredRange:
    """
    Represents the inclusive date range for which partitions have been ensured.

    This dataclass encapsulates the state of covered partitions and provides
    convenient methods for range checking and extension. It is stored as an
    instance attribute on :class:`PartmanManager` to enable fast in-memory
    checks before any database work.

    Attributes
    ----------
    start : datetime.date
        The earliest date for which a partition is known to exist.
    end : datetime.date
        The latest date for which a partition is known to exist (inclusive).

    Methods
    -------
    contains(d: date) -> bool
        Return True if the given date falls within the covered range.
    extend_to(new_date: date, buffer_days: int = 7) -> None
        Extend the range to include ``new_date`` plus an optional buffer.
    """

    start: date
    end: date

    def contains(self, d: date) -> bool:
        """Return whether the supplied date is within the covered range."""
        return self.start <= d <= self.end

    def extend_to(self, new_date: date, buffer_days: int = 7) -> None:
        """
        Extend the covered range to include ``new_date`` plus a buffer.

        Parameters
        ----------
        new_date : datetime.date
            The date that must now be covered.
        buffer_days : int, default 7
            Number of additional days to extend into the future.
        """
        if new_date < self.start:
            self.start = new_date
        new_end = new_date + timedelta(days=buffer_days)
        if new_end > self.end:
            self.end = new_end

    def __str__(self) -> str:
        return f"{self.start.isoformat()} → {self.end.isoformat()}"


class PartmanManager:
    """
    High-level async manager for the pg_partman PostgreSQL extension.

    This class provides a clean, Pythonic interface to `pg_partman` for
    automated daily range partitioning. It handles registration via
    ``create_parent()``, initial partition creation via ``run_maintenance()``,
    and efficient per-insert checks via an in-memory ``CoveredRange``.

    The manager is designed to be initialized once at application startup
    and then used via the lightweight ``ensure_partition_for_date()`` method
    before every write to a partitioned table.

    Parameters
    ----------
    pool : asyncpg.Pool
        An active asyncpg connection pool.
    logger : logging.Logger, optional
        Logger instance. If None, a module-level logger is created.
    default_premake : int, default 14
        Default number of future partitions to pre-create during initialization.
    default_buffer_days : int, default 7
        Default future buffer when extending coverage on-demand.

    Attributes
    ----------
    pool : asyncpg.Pool
        The connection pool used for all operations.
    logger : logging.Logger
        Structured logger.
    covered : CoveredRange or None
        The currently ensured date range (set after successful initialization).
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        logger: logging.Logger | None = None,
        default_premake: int = 14,
        default_buffer_days: int = 7,
    ) -> None:
        self.pool = pool
        self.logger = logger or logging.getLogger(__name__)
        self.default_premake = default_premake
        self.default_buffer_days = default_buffer_days
        self.covered: Optional[CoveredRange] = None

    async def is_installed(self) -> bool:
        """
        Check whether the pg_partman extension is installed in the database.

        Returns
        -------
        bool
            True if the extension is present and usable, False otherwise.
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
        Register a parent table with pg_partman.

        This method calls the `partman.create_parent()` stored procedure with
        parameters optimised for daily native declarative partitioning.

        Parameters
        ----------
        parent_table : str
            Fully qualified table name (e.g. ``'public.responses'``).
            Must be schema-qualified.
        control_column : str, default "tstamp"
            Name of the timestamptz column used as partition key.
        interval : str, default "1 day"
            Partition interval (e.g. ``'1 day'``, ``'1 month'``).
        premake : int, default 7
            Number of future partitions to pre-create.
        start_partition : str, optional
            Starting date in ``'YYYY-MM-DD'`` format. If None, pg_partman
            uses the current date.

        Returns
        -------
        bool
            True on success, False on failure.
        """
        if not await self.is_installed():
            self.logger.warning("pg_partman not installed — skipping registration")
            return False

        try:
            query = """
                SELECT partman.create_parent(
                    p_parent_table    := $1,
                    p_control         := $2,
                    p_type            := 'range',
                    p_interval        := $3,
                    p_premake         := $4,
                    p_start_partition := $5
                )
            """
            await self.pool.execute(
                query,
                parent_table,
                control_column,
                interval,
                premake,
                start_partition,
            )
            self.logger.info(
                "pg_partman parent registered successfully for %s", parent_table
            )
            return True
        except Exception as exc:
            self.logger.error(
                "Failed to register %s with pg_partman: %s", parent_table, exc
            )
            return False

    async def initialize(
        self,
        parent_table: str = "public.responses",
        control_column: str = "tstamp",
        interval: str = "1 day",
        premake: Optional[int] = None,
        start_partition: Optional[date] = None,
    ) -> bool:
        """
        One-time initialization of pg_partman for a partitioned table.

        This is the recommended entry point. It registers the table (if needed),
        runs maintenance to create initial partitions, and populates the
        in-memory ``covered`` range.

        Parameters
        ----------
        parent_table : str, default "public.responses"
            Fully qualified parent table name.
        control_column : str, default "tstamp"
            Partition key column.
        interval : str, default "1 day"
            Partition interval.
        premake : int, optional
            Number of future partitions to create. Defaults to
            ``self.default_premake``.
        start_partition : datetime.date, optional
            Earliest date to cover. Defaults to yesterday.

        Returns
        -------
        bool
            True if initialization succeeded.
        """
        if start_partition is None:
            start_partition = date.today() - timedelta(days=1)

        premake = premake or self.default_premake

        self.logger.info(
            "Initializing PartmanManager for %s (start=%s, premake=%d)",
            parent_table,
            start_partition,
            premake,
        )

        success = await self.create_parent(
            parent_table=parent_table,
            control_column=control_column,
            interval=interval,
            premake=premake,
            start_partition=start_partition.isoformat(),
        )

        if not success:
            return False

        await self.pool.execute(f"SELECT partman.run_maintenance('{parent_table}')")

        self.covered = CoveredRange(
            start=start_partition, end=date.today() + timedelta(days=premake)
        )

        self.logger.info("PartmanManager ready. Covered range: %s", self.covered)
        return True

    async def ensure_partition_for_date(
        self,
        target_date: Optional[date] = None,
        parent_table: str = "public.responses",
    ) -> bool:
        """
        Ensure a partition exists for the given date (main public method).

        This method implements a fast in-memory check. If the date is already
        covered, it returns immediately with zero database cost. Otherwise it
        extends coverage by running ``partman.run_maintenance()``.

        Parameters
        ----------
        target_date : datetime.date, optional
            Date to ensure. Defaults to today.
        parent_table : str, default "public.responses"
            Fully qualified parent table.

        Returns
        -------
        bool
            True if the partition is now guaranteed to exist.
        """
        if target_date is None:
            target_date = date.today()

        if self.covered and self.covered.contains(target_date):
            return True

        self.logger.debug(
            "Date %s outside covered range %s — extending partitions",
            target_date,
            self.covered,
        )

        await self.pool.execute(f"SELECT partman.run_maintenance('{parent_table}')")

        if self.covered:
            self.covered.extend_to(target_date, self.default_buffer_days)
        else:
            self.covered = CoveredRange(
                start=target_date,
                end=target_date + timedelta(days=self.default_buffer_days),
            )

        self.logger.info("Extended coverage to %s", self.covered)
        return True

    async def ensure_partitions(
        self,
        parent_table: str,
        days_ahead: int = 7,
    ) -> None:
        """
        Ensure that the required partitions exist for a date range.

        This method intelligently adjusts the `premake` setting if `days_ahead`
        is larger than the currently configured value, then runs
        `partman.run_maintenance()` to create any missing partitions.

        Parameters
        ----------
        parent_table : str
            Fully qualified name of the parent partitioned table
            (e.g. ``'public.responses'``).
        days_ahead : int, default 7
            Minimum number of future days that must have partitions.
            If this is greater than the current ``premake`` value,
            the ``premake`` will be temporarily increased.

        Notes
        -----
        This method is safe to call frequently. It only performs work
        when partitions are actually missing.
        """
        if not await self.is_installed():
            self.logger.debug("pg_partman not available — skipping")
            return

        try:
            # 1. Get current premake value from part_config
            current_premake = await self.pool.fetchval(
                """
                SELECT premake 
                FROM partman.part_config 
                WHERE parent_table = $1
                """,
                parent_table,
            )

            # 2. If user wants more future partitions than currently configured,
            #    temporarily increase premake
            if days_ahead > (current_premake or 0):
                self.logger.info(
                    "Increasing premake from %s to %s for %s",
                    current_premake,
                    days_ahead,
                    parent_table,
                )
                await self.pool.execute(
                    """
                    UPDATE partman.part_config 
                    SET premake = $1 
                    WHERE parent_table = $2
                    """,
                    days_ahead,
                    parent_table,
                )

            # 3. Run maintenance (this will now create the extra partitions)
            await self.pool.execute("SELECT partman.run_maintenance($1)", parent_table)

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
