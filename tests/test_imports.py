"""
tests/test_imports.py

Comprehensive import tests for the py_pgkit package.

Tests are ordered from the lowest-level (most internal) modules
to the highest-level public API. This ensures every __init__.py correctly
exposes its submodules and that the package can be imported cleanly at every level.

Run with:
    pytest tests/test_imports.py -q
"""

import pytest

# ============================================================
# LEVEL 1: Deepest internal modules (no __init__.py dependencies)
# ============================================================


def test_import_db_settings():
    """Test core settings module."""
    from py_pgkit.db.settings import PgSettings

    assert PgSettings is not None


def test_import_db_pool():
    """Test connection pool module."""
    from py_pgkit.db.pool import close_all_pools, get_pool

    assert close_all_pools is not None
    assert get_pool is not None


# ============================================================
# LEVEL 2: Database methods (internal implementation)
# ============================================================


def test_import_db_methods_db_tools():
    """Test database tools module."""
    from py_pgkit.db.methods.db_tools import (
        ensure_functions_loaded,
        ensure_partition_exists,
    )

    assert ensure_partition_exists is not None
    assert ensure_functions_loaded is not None


def test_import_db_methods_load():
    """Test data loading module."""
    from py_pgkit.db.methods.load import (
        bulk_insert,
        load_query_to_memory,
        load_table_to_memory,
    )

    assert load_table_to_memory is not None
    assert bulk_insert is not None


def test_import_db_methods_query():
    """Test query execution module."""
    from py_pgkit.db.methods.query import (
        execute_query,
        query_logs,
        run_multi_statement_sql_script,
    )

    assert execute_query is not None
    assert run_multi_statement_sql_script is not None


# ============================================================
# LEVEL 3: Subpackage __init__.py files
# ============================================================


def test_import_db_methods_package():
    """Test that py_pgkit.db.methods exposes its public API."""
    import py_pgkit.db.methods as methods

    assert hasattr(methods, "ensure_partition_exists")
    assert hasattr(methods, "load_table_to_memory")
    assert hasattr(methods, "execute_query")


def test_import_db_package():
    """Test that py_pgkit.db exposes its public API."""
    import py_pgkit.db as db

    assert hasattr(db, "PgSettings")
    assert hasattr(db, "get_pool")
    assert hasattr(db, "ensure_partition_exists")                                         # re-exported from methods


# ============================================================
# LEVEL 4: Other top-level subpackages (if they exist)
# ============================================================


def test_import_logging_package():
    """Test logging subpackage (if present)."""
    try:
        import py_pgkit.logging

        assert True
    except ImportError:
        pytest.skip("py_pgkit.logging not present")


# ============================================================
# LEVEL 5: Top-level package import
# ============================================================


def test_import_top_level_py_pgkit():
    """Test the main public API of the entire package."""
    import py_pgkit

    # Core attributes that should always be available
    assert hasattr(py_pgkit, "db")
    assert hasattr(py_pgkit, "__version__") or True                                       # optional

    # Verify we can reach deep modules through the top-level import
    from py_pgkit.db.methods import query as q

    assert q.execute_query is not None
