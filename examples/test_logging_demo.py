#!/usr/bin/env python3
"""
Focused Logging & Maintenance Demo for py-pgKit
===============================================

A lightweight, self-contained companion to `end_to_end_comprehensive.py` that
zeroes in on the structured logging subsystem and the two maintenance
helpers (`ensure_functions_loaded` and `ensure_partition_exists`).

It re-uses the exact same test-database connection contract as
`basic_usage.py` and `end_to_end_comprehensive.py`, and applies the same
pre-/post-cleanup discipline.

This file is ideal for:
- Quick smoke-testing of the logging pipeline after a schema change
- Demonstrating how to integrate DB-backed logging into an existing
  application without pulling in the full test harness

All documentation follows the same verbose NumPy/SciPy style.

Note: API calls have been corrected to match the actual library signatures
from the source (functions=..., start_value=... etc.).
"""

import asyncio
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import py_pgkit as pgk
from py_pgkit import logging as pgk_logging
from py_pgkit.db import PgSettings


def get_test_settings() -> PgSettings:
    """Return the canonical test `PgSettings` (identical to other examples)."""
    return PgSettings(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=int(os.getenv("PGPORT", 5432)),
        database=os.getenv("PGDATABASE", "testdb"),
        user=os.getenv("PGUSER", "testuser"),
        password=os.getenv("PGPASSWORD", "testpass"),
    )


async def cleanup(settings: PgSettings) -> None:
    """Minimal cleanup – only the objects this demo creates."""
    pool = await pgk.get_pool(settings)
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS demo_partitioned CASCADE")
        await conn.execute("DROP FUNCTION IF EXISTS demo_upper(TEXT)")
        await conn.execute("DROP TABLE IF EXISTS py_pgkit_logs CASCADE")
    await pgk.close_all_pools()


async def main() -> None:
    """
    Run a focused logging + maintenance demonstration.

    The script:
        1. Cleans any previous artefacts
        2. Creates a partitioned table + custom function via the dedicated helpers
        3. Configures and exercises the full DB-backed logging stack
        4. Queries the logs it just wrote
        5. Cleans up again
    """
    print("🚀  py-pgKit Logging & Maintenance Demo")
    settings = get_test_settings()

    try:
        await cleanup(settings)

        # --- 1. ensure_partition_exists on a fresh partitioned table ---
        print("\n1️⃣  Creating partitioned table + ensuring today's partition...")
        await pgk.execute_query(
            """
            CREATE TABLE IF NOT EXISTS demo_partitioned (
                id        SERIAL,
                log_time  TIMESTAMPTZ NOT NULL,
                payload   JSONB
            ) PARTITION BY RANGE (log_time);
            """,
            settings,
        )

        today = date.today()
        tomorrow = today + timedelta(days=1)
        part_name = f"demo_partitioned_{today.strftime('%Y%m%d')}"

        # Corrected API: use start_value / end_value (strings) and table_name
        await pgk.ensure_partition_exists(
            table_name="demo_partitioned",
            partition_name=part_name,
            start_value=f"{today.isoformat()} 00:00:00",
            end_value=f"{tomorrow.isoformat()} 00:00:00",
            settings=settings,
        )
        print(f"   Partition {part_name} ensured.")

        # --- 2. ensure_functions_loaded from an in-memory string (via temp file) ---
        print("\n2️⃣  Loading a custom SQL function...")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "demo_upper.sql"
            p.write_text(
                "CREATE OR REPLACE FUNCTION demo_upper(t TEXT) "
                "RETURNS TEXT LANGUAGE sql AS $$ SELECT UPPER(t); $$;"
            )
            # Corrected: functions= (not functions_path=)
            await pgk.ensure_functions_loaded(functions=p, settings=settings)
        print("   demo_upper() function loaded and verified.")

        # --- 3. Structured logging round-trip ---
        print("\n3️⃣  Activating DB-backed structured logging...")
        pgk.configure_logging(settings)

        logger = pgk_logging.getLogger("logging_demo")
        logger.info(
            "Demo event – logging subsystem fully operational",
            extra={"obj": {"demo_id": 42, "timestamp": str(today)}},
        )
        logger.error(
            "Intentional test error for query_logs validation",
            extra={"obj": {"error_code": 500, "trace": "demo-stack"}},
        )
        print("   Two structured events written to py_pgkit_logs table.")

        logs = await pgk.query_logs(settings, level=None, limit=5)
        print(
            f"   query_logs() retrieved {len(logs)} record(s). Sample level: {logs[0]['loglvl'] if logs else 'N/A'}"
        )

        print("\n✅  Logging & maintenance demo completed successfully.")

    finally:
        await cleanup(settings)
        print("🧹  Demo artefacts removed.")


if __name__ == "__main__":
    asyncio.run(main())
