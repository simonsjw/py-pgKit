"""
py_pgkit.partitioning.partman
=============================

High-level async manager for the pg_partman PostgreSQL extension.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import asyncpg


@dataclass
class CoveredRange:
    """Represents the inclusive date range for which partitions have been ensured."""

    start: date
    end: date

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def extend_to(self, new_date: date, buffer_days: int = 7) -> None:
        if new_date < self.start:
            self.start = new_date
        new_end = new_date + timedelta(days=buffer_days)
        if new_end > self.end:
            self.end = new_end

    def __str__(self) -> str:
        return f"{self.start.isoformat()} → {self.end.isoformat()}"


class PartmanManager:
    """
    High-level async manager for pg_partman.

    Provides registration, initial partition creation, and efficient
    per-insert partition assurance via an in-memory CoveredRange cache.
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
        self.covered: CoveredRange | None = None

    async def is_installed(self) -> bool:
        try:
            result = await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_partman')"
            )
            return bool(result)
        except Exception as exc:
            self.logger.warning("Failed to check pg_partman: %s", exc)
            return False

    async def create_parent(
        self,
        parent_table: str,
        control_column: str = "tstamp",
        interval: str = "1 day",
        premake: int = 7,
        start_partition: str | None = None,
    ) -> bool:
        if not await self.is_installed():
            self.logger.warning("pg_partman not installed — skipping registration")
            return False

        try:
            await self.pool.execute(
                """
                SELECT partman.create_parent(
                    p_parent_table    := $1,
                    p_control         := $2,
                    p_type            := 'range',
                    p_interval        := $3,
                    p_premake         := $4,
                    p_start_partition := $5
                )
                """,
                parent_table,
                control_column,
                interval,
                premake,
                start_partition,
            )
            self.logger.info("pg_partman parent registered: %s", parent_table)
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
        premake: int | None = None,
        start_partition: date | None = None,
    ) -> bool:
        if start_partition is None:
            start_partition = date.today() - timedelta(days=1)
        premake = premake or self.default_premake

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
            start=start_partition,
            end=date.today() + timedelta(days=premake),
        )
        self.logger.info("PartmanManager ready. Covered: %s", self.covered)
        return True

    async def ensure_partition_for_date(
        self,
        target_date: date | None = None,
        parent_table: str = "public.responses",
    ) -> bool:
        if target_date is None:
            target_date = date.today()

        if self.covered and self.covered.contains(target_date):
            return True

        await self.pool.execute(f"SELECT partman.run_maintenance('{parent_table}')")

        if self.covered:
            self.covered.extend_to(target_date, self.default_buffer_days)
        else:
            self.covered = CoveredRange(
                start=target_date,
                end=target_date + timedelta(days=self.default_buffer_days),
            )
        return True

    async def ensure_partitions(
        self,
        parent_table: str,
        days_ahead: int = 7,
    ) -> None:
        if not await self.is_installed():
            return

        try:
            current_premake = await self.pool.fetchval(
                "SELECT premake FROM partman.part_config WHERE parent_table = $1",
                parent_table,
            )
            if days_ahead > (current_premake or 0):
                await self.pool.execute(
                    "UPDATE partman.part_config SET premake = $1 WHERE parent_table = $2",
                    days_ahead,
                    parent_table,
                )
            await self.pool.execute("SELECT partman.run_maintenance($1)", parent_table)
        except Exception as exc:
            self.logger.error(
                "pg_partman maintenance failed for %s: %s", parent_table, exc
            )
            raise


# =============================================================================
# Singleton / Factory Helpers (New)
# =============================================================================

_partman_manager: PartmanManager | None = None


def get_partman_manager() -> PartmanManager:
    """Return the cached PartmanManager instance."""
    if _partman_manager is None:
        raise RuntimeError(
            "PartmanManager has not been initialized. "
            "Call initialize_partman_manager() after database bootstrap."
        )
    return _partman_manager


async def initialize_partman_manager(
    pool: asyncpg.Pool,
    parent_table: str = "responses",
    control_column: str = "tstamp",
    premake: int = 14,
) -> PartmanManager:
    """Initialize and cache a PartmanManager instance (call once at startup)."""
    global _partman_manager
    if _partman_manager is not None:
        return _partman_manager

    manager = PartmanManager(pool)
    if not await manager.is_installed():
        raise RuntimeError("pg_partman extension is not installed in the database.")

    await manager.initialize(
        parent_table=parent_table,
        control_column=control_column,
        premake=premake,
    )
    _partman_manager = manager
    return manager
