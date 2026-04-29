"""
tests/test_imports.py

Import tests for py-pgkit, organised by the library/module being imported.

This gives a clear overview of the public API and ensures every exposed
method/class is importable both directly and via package __init__.py files.
"""

import pytest

# ============================================================
# py_pgkit.db.settings
# ============================================================


def test_import_from_db_settings():
    """Core configuration class."""
    from py_pgkit.db.settings import PgSettings

    assert PgSettings is not None


# ============================================================
# py_pgkit.db.pool
# ============================================================


def test_import_from_db_pool():
    """Connection pool management."""
    from py_pgkit.db.pool import close_all_pools, get_pool

    assert get_pool is not None
    assert close_all_pools is not None


# ============================================================
# py_pgkit.db.methods.db_tools
# ============================================================


def test_import_from_db_methods_db_tools():
    """Partitioning and custom function utilities."""
    from py_pgkit.db.methods.db_tools import (
        ensure_functions_loaded,
        ensure_partition_exists,
    )

    assert ensure_partition_exists is not None
    assert ensure_functions_loaded is not None


# ============================================================
# py_pgkit.db.methods.load
# ============================================================


def test_import_from_db_methods_load():
    """Data loading and high-performance bulk insert."""
    from py_pgkit.db.methods.load import (
        bulk_insert,
        load_query_to_memory,
        load_table_to_memory,
    )

    assert load_table_to_memory is not None
    assert load_query_to_memory is not None
    assert bulk_insert is not None


# ============================================================
# py_pgkit.db.methods.query
# ============================================================


def test_import_from_db_methods_query():
    """Query execution and multi-statement script runner."""
    from py_pgkit.db.methods.query import (
        execute_query,
        query_logs,
        run_multi_statement_sql_script,
    )

    assert execute_query is not None
    assert run_multi_statement_sql_script is not None
    assert query_logs is not None


# ============================================================
# py_pgkit.db.methods (subpackage)
# ============================================================


def test_import_db_methods_package():
    """Verify py_pgkit.db.methods re-exports its public API."""
    import py_pgkit.db.methods as methods

    assert hasattr(methods, "ensure_partition_exists")
    assert hasattr(methods, "load_table_to_memory")
    assert hasattr(methods, "bulk_insert")
    assert hasattr(methods, "execute_query")


# ============================================================
# py_pgkit.db (main subpackage)
# ============================================================


def test_import_db_package():
    """Verify py_pgkit.db re-exports the main public API."""
    import py_pgkit.db as db

    assert hasattr(db, "PgSettings")
    assert hasattr(db, "get_pool")
    assert hasattr(db, "close_all_pools")
    assert hasattr(db, "ensure_partition_exists")
    assert hasattr(db, "bulk_insert")
    assert hasattr(db, "execute_query")
    assert hasattr(db, "DatabaseBuilder")                                                 # if exposed at db level


# ============================================================
# py_pgkit.logging (optional)
# ============================================================


def test_import_logging_package():
    """Logging subpackage (mostly internal)."""
    try:
        import py_pgkit.logging

        assert True
    except ImportError:
        pytest.skip("py_pgkit.logging not present or has no public API")


# ============================================================
# Top-level: import py_pgkit
# ============================================================


def test_import_top_level_py_pgkit():
    """Full public API via the top-level package."""
    import py_pgkit

    assert hasattr(py_pgkit, "db")

    # Deep access still works
    from py_pgkit.db.methods import query as q

    assert q.execute_query is not None

    from py_pgkit.db import PgSettings, bulk_insert, get_pool

    assert PgSettings is not None
    assert get_pool is not None
    assert bulk_insert is not None
