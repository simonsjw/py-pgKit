"""
Basic usage example for py-pgkit.

Run with: python examples/basic_usage.py
(requires a running PostgreSQL with the given credentials)
"""

import asyncio

import py_pgkit as pgk
from py_pgkit.db import PgSettings


async def main():
    settings = PgSettings(
        host="localhost",
        port=5432,
        database="testdb",
        user="postgres",
        password="postgres",
        extensions=["uuid-ossp"],
    )

    print("=== Configuring logging ===")
    pgk.configure_logging(settings)

    logger = pgk.logging.getLogger(__name__)
    logger.info("py-pgkit example started", extra={"obj": {"version": "0.1.0"}})

    print("\n=== Testing pool ===")
    pool = await pgk.db.get_pool(settings)
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT version()")
        print(f"PostgreSQL version: {version[:50]}...")

    print("\n=== Testing DatabaseBuilder (idempotent) ===")
    builder = pgk.db.DatabaseBuilder(settings, create_tables=False)
    await builder.build()
    print("Builder completed (no tables defined in this example)")

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
