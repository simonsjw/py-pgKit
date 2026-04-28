"""
Tests for py_pgkit.db.builder.DatabaseBuilder

Covers construction, parameter handling, and selected internal logic with mocks.
Full end-to-end build requires a real DB + models, so we test the orchestration points.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from py_pgkit.db.builder import DatabaseBuilder
from py_pgkit.db.settings import PgSettings


def test_database_builder_init(settings):
    builder = DatabaseBuilder(
        settings=settings,
        create_tablespace=False,
        partition_strategy="daily",
        functions="/tmp/sql/",
    )
    assert builder.settings is settings
    assert builder.create_tablespace is False
    assert builder.partition_strategy == "daily"
    assert builder.functions == "/tmp/sql/"


@pytest.mark.asyncio
async def test_database_builder_build_calls_expected_steps(settings):
    """Verify the build() method calls the right internal helpers based on flags."""
    # Need tablespace_name so the guard in build() passes
    ts_settings = PgSettings(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password=settings.password,
        tablespace_name="fast_ssd",
        tablespace_path="/tmp/ssd",
    )
    builder = DatabaseBuilder(
        settings=ts_settings,
        create_tablespace=True,
        create_database=True,
        create_extensions=True,
        create_tables=False,          # skip (no models)
        create_triggers_and_functions=False,
        partition_strategy=None,
    )

    with patch.object(builder, "_ensure_tablespace") as mock_ts, \
         patch.object(builder, "_ensure_database") as mock_db, \
         patch.object(builder, "_ensure_extensions") as mock_ext, \
         patch.object(builder, "_get_pool") as mock_get_pool:

        mock_get_pool.return_value = AsyncMock()

        await builder.build()

        mock_ts.assert_awaited_once()
        mock_db.assert_awaited_once()
        mock_ext.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_daily_partition_delegates(settings):
    builder = DatabaseBuilder(settings=settings)

    with patch("py_pgkit.db.builder.ensure_partition_exists") as mock_ensure:
        await builder.add_daily_partition("logs", "2026-04-28", "2026-04-29")
        mock_ensure.assert_awaited_once()
        args, _ = mock_ensure.call_args
        assert args[0] == "logs"
        assert "logs_2026_04_28" in args[1]  # will be updated after patch target fix