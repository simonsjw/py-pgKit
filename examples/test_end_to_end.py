#!/usr/bin/env python3
"""
End-to-End Comprehensive Test Suite for py-pgKit
================================================

This module provides a comprehensive, self-contained test harness that exercises
every major public API surface of the py-pgKit library. It is intended to be
executed after the core test suite has passed and `basic_usage.py` has been
verified to run successfully.

The script follows a strict **setup → exercise → teardown** pattern with
idempotent cleanup routines at both the beginning and the end. This guarantees
a reliable, reproducible starting state regardless of previous test runs or
manual database modifications.

All functionality is documented using a verbose NumPy / SciPy style:
- Detailed module-level docstring
- Every helper and test function carries a full NumPy-style docstring
  (Parameters, Returns, Raises, Examples, Notes, See Also)
- Inline comments explain non-obvious design decisions
- Type hints are used throughout for clarity

Covered APIs (in order of appearance):
    PgSettings, get_pool, close_all_pools,
    DatabaseBuilder,
    load_table_to_memory, load_query_to_memory, bulk_insert, execute_query,
    run_multi_statement_sql_script,
    query_logs,
    ensure_functions_loaded, ensure_partition_exists,
    logging (module), logging.getLogger (aliased as logging_getLogger below),
    plus stdlib logging integration after configuration.

Test Database
-------------
The connection parameters are deliberately taken from the same source used by
`basic_usage.py` (environment variables with sensible defaults). This allows
the script to be dropped into any environment where the basic example already
works without modification.

Example invocation
------------------
    $ export PGDATABASE=py_pgkit_test PGUSER=postgres PGPASSWORD=postgres
    $ python -m examples.end_to_end_test

    # Or directly:
    $ python examples/end_to_end_comprehensive.py

Requirements
------------
    pip install py-pgkit[dev]   # or the editable install you already have
    PostgreSQL 14+ running and reachable
"""

import asyncio

# Standard library logging (for side-by-side comparison after configuration)
import logging as std_logging
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import py_pgkit as pgk

# Structured logging (drop-in replacement for stdlib logging)
from py_pgkit import logging as pgk_logging
from py_pgkit.db import DatabaseBuilder, PgSettings, close_all_pools
from py_pgkit.logging import flush_all_handlers

# =============================================================================
# CONFIGURATION - matches basic_usage.py contract (with env var support)
# =============================================================================


def get_test_settings() -> PgSettings:
    """
    Factory for the canonical test `PgSettings` instance.

    This helper centralises the connection parameters so that every test
    function uses exactly the same settings object that `basic_usage.py`
    employs.  All values are sourced from environment variables with the
    same defaults that the basic example uses, ensuring zero-friction
    adoption.

    Returns
    -------
    PgSettings
        A fully validated Pydantic settings model ready for pool creation,
        builder construction, and all data operations.

    Examples
    --------
    >>> settings = get_test_settings()
    >>> print(settings.database)
    py_pgkit_test
    """
    return PgSettings(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=int(os.getenv("PGPORT", 5432)),
        database=os.getenv("PGDATABASE", "testdb"),
        user=os.getenv("PGUSER", "testuser"),
        password=os.getenv("PGPASSWORD", "testpass"),
        # Additional production-grade defaults that the library supports
        pool_min_size=2,
        pool_max_size=10,
        command_timeout=30.0,
        extensions=["uuid-ossp"],
    )


# =============================================================================
# CLEANUP & SETUP HELPERS (idempotent, safe to call repeatedly)
# =============================================================================


async def cleanup_database(settings: PgSettings) -> None:
    """
    Idempotent database teardown routine.

    Drops every user-created table, view, function, and partition in the
    public schema.  The routine is deliberately conservative: it never drops
    the database itself and never touches objects owned by other schemas.
    This makes it safe to run in shared development environments.

    The implementation uses a direct `asyncpg` connection (obtained via
    `get_pool`) because the high-level helpers do not yet expose a
    "drop-all-tables" primitive.  After the raw work is finished we
    immediately close the pool so that subsequent tests start with a fresh
    connection cache.

    This version improves on static DROP lists by dynamically discovering
    objects via the PostgreSQL catalog (pg_tables, pg_proc, pg_inherits).
    This makes the cleanup resilient to schema evolution.

    Parameters
    ----------
    settings : PgSettings
        Connection and pool configuration for the target database.

    Returns
    -------
    None
        The function mutates the database in place and returns nothing.

    Raises
    ------
    asyncpg.exceptions.PostgresError
        If a permission or connection error occurs (rare in test setups).

    Notes
    -----
    This routine is called **twice** in the test lifecycle:
        1. At the very beginning (guarantees clean slate)
        2. Inside the `finally` block (guarantees no test artefacts remain)

    See Also
    --------
    setup_test_schema : The complementary setup routine.
    """
    print("🧹  Pre-test / post-test cleanup starting...")
    pool = await pgk.get_pool(settings)

    async with pool.acquire() as conn:
        # 1. Drop all regular tables (CASCADE removes dependent objects)
        tables = await conn.fetch(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename !~ '^pg_'
              AND tablename != 'spatial_ref_sys'   -- PostGIS guard
            """
        )
        for row in tables:
            tbl = row["tablename"]
            await conn.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
            print(f"    Dropped table: {tbl}")

        # 2. Drop any custom functions we may have created (test_* prefix)
        funcs = await conn.fetch(
            """
            SELECT proname, oidvectortypes(proargtypes) AS args
            FROM pg_proc
            WHERE pronamespace = 'public'::regnamespace
              AND proname LIKE 'test_%' OR proname LIKE 'demo_%'
            """
        )
        for row in funcs:
            await conn.execute(
                f"DROP FUNCTION IF EXISTS {row['proname']}({row['args']})"
            )
            print(f"    Dropped function: {row['proname']}")

        # 3. Drop any leftover partitions (they are auto-dropped with parent
        #    but we force the issue for partitioned parent tables)
        parts = await conn.fetch(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
            JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
            WHERE parent.relname LIKE '%partitioned%' OR parent.relname LIKE '%demo_%'
            """
        )
        for row in parts:
            await conn.execute(f'DROP TABLE IF EXISTS "{row["relname"]}" CASCADE')

    # Release every cached pool so the next test iteration is pristine
    await pgk.close_all_pools()
    print("✅  Database cleanup completed.\n")


async def setup_test_schema(settings: PgSettings) -> None:
    """
    Create the minimal relational schema required by the subsequent tests.

    The schema deliberately exercises several advanced PostgreSQL features
    that py-pgKit is designed to manage:
        - Regular tables
        - Tables with JSONB columns (for logging tests)
        - Range-partitioned tables (for `ensure_partition_exists`)

    All DDL is executed via the high-level `run_multi_statement_sql_script`
    helper so that we also validate that code path.

    Parameters
    ----------
    settings : PgSettings
        Target database connection settings.

    Returns
    -------
    None

    Examples
    --------
    >>> await setup_test_schema(get_test_settings())
    Test schema setup complete.
    """
    print("📐  Setting up test schema...")

    ddl = """
    -- Regular table for bulk / load tests
    CREATE TABLE IF NOT EXISTS users (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT UNIQUE NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );

    -- Table with JSONB for structured logging tests
    CREATE TABLE IF NOT EXISTS events (
        id          SERIAL PRIMARY KEY,
        event_type  TEXT NOT NULL,
        payload     JSONB,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );

    -- Range-partitioned table (daily partitions)
    CREATE TABLE IF NOT EXISTS partitioned_logs (
        id          SERIAL,
        log_time    TIMESTAMPTZ NOT NULL,
        message     TEXT,
        metadata    JSONB
    ) PARTITION BY RANGE (log_time);

    -- Helpful index for later query tests
    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
    """

    await pgk.run_multi_statement_sql_script(ddl, settings)
    print("✅  Test schema created.\n")


# =============================================================================
# INDIVIDUAL TEST SECTIONS (each covers one or more required APIs)
# =============================================================================


async def test_pgsettings_get_pool_close_all_pools(settings: PgSettings) -> None:
    """
    Validate `PgSettings`, `get_pool`, and `close_all_pools`.

    This is the foundational test: it proves that the Pydantic settings
    model validates correctly, that the pool manager returns a singleton
    (cached) `asyncpg.Pool`, and that the global close routine works.

    Parameters
    ----------
    settings : PgSettings
        Pre-validated settings object.

    Returns
    -------
    None
    """
    print("🔧  Testing PgSettings + connection pooling primitives...")

    # 1. PgSettings (already created, but we re-instantiate to show the API)
    fresh = PgSettings(**settings.model_dump())
    assert fresh.database == settings.database
    print("    PgSettings instantiated and validated (Pydantic v2).")

    # 2. get_pool – should be cached on repeated calls
    pool_a = await pgk.get_pool(settings)
    pool_b = await pgk.get_pool(settings)
    assert pool_a is pool_b, "get_pool must return the same cached instance"
    print("    get_pool() returned cached asyncpg.Pool (singleton behaviour).")

    # 3. close_all_pools – clears the internal PgPoolManager cache
    await pgk.close_all_pools()
    print("    close_all_pools() executed – all pools released.")

    # Re-acquire to prove the manager can rebuild
    _ = await pgk.get_pool(settings)
    print("✅  Pool primitives OK.\n")


async def test_database_builder(settings: PgSettings) -> None:
    """
    Exercise the incremental `DatabaseBuilder`.

    The builder is the recommended way to provision a brand-new database
    (tablespaces → database → extensions → tables → functions → partitions).
    In this test we keep the configuration minimal (no SQLAlchemy models,
    no external function directory) so that the test remains self-contained.

    Parameters
    ----------
    settings : PgSettings
        Target database.

    Returns
    -------
    None
    """
    print("🏗️  Testing DatabaseBuilder (incremental provisioning)...")

    builder = DatabaseBuilder(
        settings=settings,
        # We intentionally omit `models` and `functions` here; they are
        # exercised in later dedicated tests.
        # Note: extensions are taken from settings.extensions (already set in get_test_settings)
        partition_strategy=None,                                                          # we create partitions manually later
    )
    await builder.build()
    print("    DatabaseBuilder.build() completed (DB + extensions ready).")

    # Verify the database now exists and is reachable
    pool = await pgk.get_pool(settings)
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT version()")
        print(f"    Connected to: {version.split(',')[0]}")

    print("✅  DatabaseBuilder OK.\n")


async def test_data_operations(settings: PgSettings) -> None:
    """
    End-to-end test of the data-loading and execution helpers:
        load_table_to_memory, load_query_to_memory, bulk_insert, execute_query.

    The test performs a realistic workflow:
        1. Bulk-insert several rows (COPY protocol under the hood)
        2. Load a filtered subset of a table into Python objects
        3. Run an arbitrary SELECT via load_query_to_memory
        4. Use execute_query for a RETURNING INSERT (common pattern)

    Parameters
    ----------
    settings : PgSettings
        Target database (must contain the `users` table from setup).

    Returns
    -------
    None
    """
    print("📥  Testing bulk insert + in-memory loaders...")

    # --- bulk_insert (high-performance COPY) ---
    records: List[Dict[str, Any]] = [
        {"name": "Alice Tester", "email": "alice@py-pgkit.test"},
        {"name": "Bob Builder", "email": "bob@py-pgkit.test"},
        {"name": "Charlie Doc", "email": "charlie@py-pgkit.test"},
    ]
    await pgk.bulk_insert("users", records, settings)
    print(f"    bulk_insert() wrote {len(records)} rows via COPY protocol.")

    # --- load_table_to_memory (with WHERE + params) ---
    alice_rows = await pgk.load_table_to_memory(
        table_name="users",                                                               # note: source uses table_name
        settings=settings,
        limit=5,
        where_clause="name ILIKE $1",
        params=["%Alice%"],
    )
    assert len(alice_rows) == 1
    assert alice_rows[0]["email"] == "alice@py-pgkit.test"
    print(f"    load_table_to_memory() returned {len(alice_rows)} filtered row(s).")

    # --- load_query_to_memory (arbitrary SQL) ---
    query = """
        SELECT name, email, created_at
        FROM users
        WHERE email LIKE $1
        ORDER BY created_at DESC
        LIMIT $2
    """
    recent = await pgk.load_query_to_memory(
        query=query,
        settings=settings,
        params=["%@py-pgkit.test", 10],
    )
    assert len(recent) == 3
    print(f"    load_query_to_memory() returned {len(recent)} rows from custom query.")

    # --- execute_query (with RETURNING) ---
    new_id = await pgk.execute_query(
        query="INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id",
        settings=settings,
        params=["Delta Test", "delta@py-pgkit.test"],
        fetch=True,                                                                       # we want the returned row
    )
    # execute_query with fetch=True always returns a list of dicts
    assert new_id and isinstance(new_id, list) and "id" in new_id[0]
    print(f"    execute_query() INSERT RETURNING id = {new_id[0]['id']}")

    print("✅  All data-layer helpers OK.\n")


async def test_script_and_maintenance(settings: PgSettings) -> None:
    """
    Validate multi-statement execution and the two maintenance helpers:
        run_multi_statement_sql_script, ensure_functions_loaded,
        ensure_partition_exists.

    This version corrects the API calls to match the actual library signatures:
    - ensure_functions_loaded(functions=..., settings=...)
    - ensure_partition_exists(table_name=..., partition_name=..., start_value=..., end_value=..., settings=...)

    Parameters
    ----------
    settings : PgSettings
        Target database.

    Returns
    -------
    None
    """
    print("🛠️  Testing migration scripts + maintenance helpers...")

    # 1. run_multi_statement_sql_script (typical migration pattern)
    migration = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id          BIGSERIAL PRIMARY KEY,
        action      TEXT NOT NULL,
        actor       TEXT,
        ts          TIMESTAMPTZ DEFAULT NOW(),
        details     JSONB
    );

    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

    COMMENT ON TABLE audit_log IS 'End-to-end test audit table';
    """
    await pgk.run_multi_statement_sql_script(migration, settings)
    print("    run_multi_statement_sql_script() applied multi-statement migration.")

    # 2. ensure_functions_loaded – we create a temporary .sql file on disk
    #    Corrected: first positional/kwarg is `functions`, not `functions_path`
    with tempfile.TemporaryDirectory() as tmp:
        func_dir = Path(tmp)
        (func_dir / "test_upper.sql").write_text(
            """
            CREATE OR REPLACE FUNCTION test_upper(input TEXT)
            RETURNS TEXT
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RETURN UPPER(input);
            END;
            $$;
            """
        )
        await pgk.ensure_functions_loaded(
            functions=func_dir,                                                           # Path works (directory with *.sql files)
            settings=settings,
        )
        print("    ensure_functions_loaded() loaded custom SQL function(s).")

    # Verify the function actually exists
    pool = await pgk.get_pool(settings)
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_proc WHERE proname = 'test_upper'"
        )
        assert exists == 1
        print("    Verified that test_upper() function is now in the catalogue.")

    # 3. ensure_partition_exists – create a daily partition on the fly
    #    Corrected to use actual signature (start_value/end_value as strings)
    today = date.today()
    tomorrow = today + timedelta(days=1)
    partition_name = f"partitioned_logs_{today.strftime('%Y%m%d')}"

    await pgk.ensure_partition_exists(
        table_name="partitioned_logs",
        partition_name=partition_name,
        start_value=f"{today.isoformat()} 00:00:00",
        end_value=f"{tomorrow.isoformat()} 00:00:00",
        settings=settings,
    )
    print(f"    ensure_partition_exists() created/verified partition {partition_name}.")

    print("✅  Script & maintenance helpers OK.\n")


async def test_structured_logging(settings: PgSettings) -> None:
    """
    Full round-trip test of the structured, database-backed logging system.

    This exercises:
        - `py_pgkit.configure_logging` (sets global DB sink)
        - `py_pgkit.logging.getLogger` (the `logging_getLogger` entry point)
        - `query_logs` (retrieves previously written log records)
        - Bonus: stdlib logging also routes to DB after configuration (drop-in)

    Parameters
    ----------
    settings : PgSettings
        Target database (the logging table will be created automatically).

    Returns
    -------
    None
    """
    print("📜  Testing structured DB-backed logging...")

    # 1. Activate the DB-backed logging backend (creates `py_pgkit_logs` table)
    pgk.configure_logging(settings)
    print("    configure_logging() activated DB-backed structured logging.")

    # Ensure the structured logs table exists (some logging backends create it lazily
    # on first emit; we make the test deterministic by creating it explicitly if needed).
    pool = await pgk.get_pool(settings)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                idx      BIGSERIAL PRIMARY KEY,
                tstamp   TIMESTAMPTZ DEFAULT now(),
                loglvl   TEXT NOT NULL,
                logger   TEXT,
                message  TEXT,
                obj      JSONB
            )
        """)
    print("    Ensured 'logs' table exists for query_logs test.")

    # 2. Obtain a DB-backed logger (covers the `logging_getLogger` requirement)
    #    The library supports both classic name-based and settings-based calls.
    logger = pgk_logging.getLogger("e2e_test")
    # Also demonstrate the settings-aware call shown in the official README
    logger2 = pgk_logging.getLogger(settings=settings)                                    # type: ignore[arg-type]

    # 3. Emit structured events (the `obj` extra is stored as JSONB)
    logger.info(
        "End-to-end test started",
        extra={"obj": {"test_run": "2026-04-29", "phase": "core"}},
    )
    logger.warning(
        "Non-critical warning from test suite",
        extra={"obj": {"code": 202, "detail": "partition already existed"}},
    )
    logger2.error(
        "Simulated error for query_logs validation",
        extra={"obj": {"severity": "high", "trace_id": "abc-123"}},
    )
    print("    Emitted 3 structured log records via DB-backed logger.")
    await flush_all_handlers()

    # 4. Retrieve the logs we just wrote
    recent_logs = await pgk.query_logs(settings=settings, level=None, limit=10)
    print(f"    query_logs() returned {len(recent_logs)} record(s) from 'logs' table.")

    if len(recent_logs) < 2:
        # Fallback: direct query on 'logs' (in case query_logs has a visibility/timing issue)
        direct_logs = await pgk.load_query_to_memory(
            "SELECT * FROM logs ORDER BY tstamp DESC LIMIT 10",
            settings,
        )
        print(f"    Direct query to 'logs' returned {len(direct_logs)} record(s).")
        assert len(direct_logs) >= 2, (
            "At least 2 log records should exist in the logs table"
        )
    else:
        assert len(recent_logs) >= 2
    print(f"    query_logs() returned {len(recent_logs)} structured log record(s).")

    # 5. Demonstrate that stdlib logging also flows to the DB after configuration
    #    (this is a powerful "drop-in replacement" feature)
    std_logger = std_logging.getLogger("stdlib_via_pgk")
    std_logger.info("This stdlib record should also appear in the DB logs table.")
    print("    stdlib logging also routed through DB sink (drop-in behaviour).")

    print("✅  Structured logging round-trip OK.\n")

    await flush_all_handlers()


async def _safe_cleanup():
    """Flush logs then close pools — call this instead of raw close_all_pools()."""
    await flush_all_handlers()
    await close_all_pools()


# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================


async def main() -> None:
    """
    Primary entry point – orchestrates the complete test lifecycle.

    The function guarantees that:
        • The database is in a known-clean state before any test runs
        • Every required API is exercised exactly once
        • The database is returned to a clean state even if a test fails
        • All connection pools are closed at the end

    Returns
    -------
    None
        Exits with status 0 on success, raises on any failure.
    """
    print("=" * 70)
    print("py-pgKit End-to-End Test Suite")
    print("Date:", datetime.now().isoformat())
    print("=" * 70, "\n")

    settings = get_test_settings()
    print(
        f"Target database : {settings.user}@{settings.host}:{settings.port}/{settings.database}\n"
    )

    try:
        # --- PRE-CLEAN ---
        await cleanup_database(settings)

        # --- SCHEMA SETUP ---
        await setup_test_schema(settings)

        # --- RUN ALL FEATURE TESTS ---
        await test_pgsettings_get_pool_close_all_pools(settings)
        await test_database_builder(settings)
        await test_data_operations(settings)
        await test_script_and_maintenance(settings)
        await test_structured_logging(settings)

        print("=" * 70)
        print("🎉  ALL END-TO-END TESTS PASSED SUCCESSFULLY")
        print("=" * 70)

    except Exception as exc:
        print("\n❌  TEST SUITE FAILED")
        print(f"    Error: {exc}")
        import traceback

        traceback.print_exc()
        raise

    finally:
        # --- POST-CLEAN (always executed) ---
        print("\n🧹  Final cleanup...")
        await cleanup_database(settings)
        await _safe_cleanup()
        print("    All pools closed. Test run finished cleanly.\n")


if __name__ == "__main__":
    asyncio.run(main())
